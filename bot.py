# AntiZalipBot — Webhook-версия (Render Web Service Free)
# UX: чистый чат, без ForceReply, нудж один раз, пользовательские сообщения не удаляем
# ИИ-коуч: лимиты на пользователя + общий лимит по $/сутки
# Фон: nightly digest
# Healthcheck + Webhook endpoint

import os
import asyncio
import logging
import random
from datetime import datetime, timedelta, date, timezone
from zoneinfo import ZoneInfo

import aiosqlite
from dotenv import load_dotenv
from aiohttp import web

from aiogram import Bot, Dispatcher, F, types
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ChatAction
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

# ---------- OpenAI ----------
try:
    from openai import AsyncOpenAI
except Exception:
    AsyncOpenAI = None

# ---------- Config / ENV ----------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN отсутствует. Добавь в .env или Render → Environment.")

OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL")  # опционально
MODEL_NAME      = os.getenv("MODEL_NAME", "gpt-4o-mini")

# для webhook
BASE_URL       = os.getenv("BASE_URL")  # например: https://antizalipbot.onrender.com
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "super_secret_path")

# дайджест
DIGEST_TZ   = os.getenv("DIGEST_TZ", "Europe/Moscow")
DIGEST_HOUR = int(os.getenv("DIGEST_HOUR", "22"))

# лимиты ИИ
MAX_AI_CALLS_PER_DAY = int(os.getenv("MAX_AI_CALLS_PER_DAY", "30"))
MAX_DAILY_SPEND      = float(os.getenv("MAX_DAILY_SPEND", "1.0"))  # $ в сутки

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("AntiZalipBot")

# ---------- Aiogram ----------
session = AiohttpSession()
bot = Bot(TOKEN, session=session)
dp = Dispatcher()

# ---------- OpenAI Client ----------
oa_client = None
if OPENAI_API_KEY and AsyncOpenAI:
    try:
        oa_client = AsyncOpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL)
        log.info("✅ OpenAI client initialized")
    except Exception as e:
        log.warning(f"OpenAI init failed: {e}")

# ---------- DB ----------
DB_PATH = os.path.join(BASE_DIR, "bot.db")

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            created TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_digest_date TEXT,
            seen_nudge INTEGER DEFAULT 0
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            event TEXT,
            value REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        # миграция на случай старой схемы
        try:
            await db.execute("ALTER TABLE users ADD COLUMN seen_nudge INTEGER DEFAULT 0")
        except Exception:
            pass
        await db.commit()

async def ensure_user(uid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (uid,))
        await db.commit()

async def set_seen_nudge(uid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET seen_nudge=1 WHERE user_id=?", (uid,))
        await db.commit()

async def get_seen_nudge(uid: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COALESCE(seen_nudge,0) FROM users WHERE user_id=?", (uid,))
        row = await cur.fetchone()
        return bool(row and row[0])

async def log_event(uid: int, event: str, value: float | None = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO events(user_id,event,value) VALUES(?,?,?)", (uid, event, value))
        await db.commit()

# ---------- Stats ----------
async def fetch_stats(uid: int) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM events WHERE user_id=? AND event='battle_win'", (uid,))
        wins, = await cur.fetchone()
        cur = await db.execute("SELECT COUNT(*) FROM events WHERE user_id=? AND event='battle_lose'", (uid,))
        loses, = await cur.fetchone()
        cur = await db.execute("SELECT COALESCE(SUM(value),0) FROM events WHERE user_id=? AND event='battle_win'", (uid,))
        reclaimed, = await cur.fetchone()
        cur = await db.execute("""
            SELECT COUNT(*), COALESCE(SUM(value),0)
            FROM events
            WHERE user_id=? AND event='timer_done' AND substr(created_at,1,10)=date('now')
        """, (uid,))
        today_cnt, today_min = await cur.fetchone()
    return {
        "wins": int(wins or 0),
        "loses": int(loses or 0),
        "reclaimed": int(reclaimed or 0),
        "today_cnt": int(today_cnt or 0),
        "today_min": int(today_min or 0),
    }

async def free_chat_count_today(uid: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT COUNT(*) FROM events
            WHERE user_id=? AND event='free_chat' AND substr(created_at,1,10)=date('now')
        """, (uid,))
        n, = await cur.fetchone()
    return int(n or 0)

# ---------- AI limits ----------
async def ai_calls_today(uid: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT COUNT(*) FROM events
            WHERE user_id=? AND event='ai_call' AND substr(created_at,1,10)=date('now')
        """, (uid,))
        n, = await cur.fetchone()
    return int(n or 0)

async def can_use_ai(uid: int) -> bool:
    return (await ai_calls_today(uid)) < MAX_AI_CALLS_PER_DAY

async def mark_ai_call(uid: int):  await log_event(uid, "ai_call", 1)
async def mark_ai_block(uid: int): await log_event(uid, "ai_block", 1)

async def total_spend_today() -> float:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT COALESCE(SUM(value),0) FROM events
            WHERE event='ai_usd' AND substr(created_at,1,10)=date('now')
        """)
        val, = await cur.fetchone()
    return float(val or 0.0)

async def add_spend(amount: float):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO events(user_id,event,value) VALUES(?,?,?)", (0, 'ai_usd', float(amount)))
        await db.commit()

async def can_spend(amount: float) -> bool:
    return (await total_spend_today()) + float(amount) <= MAX_DAILY_SPEND

# ---------- AI generation ----------
SYSTEM_COACH = (
    "Ты жёсткий, но уважительный тренер по фокусу. "
    "Отвечай коротко (2–4 фразы), по делу, с одним понятным микро-шагом ≤2 минут. "
    "Не используй 'туплю/выгорел/депрессия'. Больше про действия и фокус."
)

async def ai_generate(prompt: str, temperature: float = 0.8, max_tokens: int = 200) -> str | None:
    if not oa_client:
        return random.choice([
            "Сделай паузу на 30 секунд, расправь плечи, вдохни глубоко. Выбери одно дело и начни с простого шага.",
            "Отложи телефон на стол, сделай 5 вдохов. Дальше — 2 минуты на самом простом действии.",
            "Закрой лишнюю вкладку и выдели 120 секунд на одно короткое действие.",
        ])
    try:
        resp = await oa_client.chat.completions.create(
            model=MODEL_NAME,
            temperature=temperature,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": SYSTEM_COACH},
                {"role": "user", "content": prompt},
            ],
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        log.warning(f"AI error: {e}")
        return None

async def ai_reply(uid: int, prompt: str, temperature=0.8, max_tokens=200) -> str:
    if not await can_use_ai(uid):
        await mark_ai_block(uid)
        return "Сегодня ты уже много общался с тренером 🤖. Без ИИ: 30 сек пауза, 5 вдохов, одно простое действие."
    est_cost = (max_tokens / 1000.0) * 0.0006  # грубая оценка
    if not await can_spend(est_cost):
        await mark_ai_block(uid)
        return "Сегодня общий лимит расходов достигнут 💸. Завтра вернём ИИ. Сейчас — короткий шаг на 2 минуты."
    text = await ai_generate(prompt, temperature=temperature, max_tokens=max_tokens)
    await mark_ai_call(uid)
    await add_spend(est_cost)
    return text or "Сделай короткую паузу, расправь плечи и выдели 2 минуты на одно действие."

# ---------- Chat cleanliness ----------
LAST_BOT_MSG: dict[tuple[int, int], int] = {}  # (chat_id, user_id) -> last bot msg id

def is_private(obj) -> bool:
    chat = obj.chat if isinstance(obj, types.Message) else obj.message.chat
    return chat.type == "private"

async def send_clean(chat_id: int, user_id: int, text: str,
                     reply_markup: types.InlineKeyboardMarkup | None = None,
                     parse_mode: str | None = None):
    key = (chat_id, user_id)
    mid = LAST_BOT_MSG.get(key)
    if mid:
        try:
            await bot.delete_message(chat_id, mid)
        except Exception:
            pass
    m = await bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode=parse_mode)
    LAST_BOT_MSG[key] = m.message_id
    return m

async def delete_after(chat_id: int, message_id: int, seconds: int = 20):
    try:
        await asyncio.sleep(seconds)
        await bot.delete_message(chat_id, message_id)
    except Exception:
        pass

# ---------- Keyboards ----------
def main_menu_kb() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="🛑 Я залип", callback_data="battle:start")],
        [
            types.InlineKeyboardButton(text="⏳ Таймеры", callback_data="menu:timer"),
            types.InlineKeyboardButton(text="💬 Тренеру", callback_data="ask"),
        ],
        [
            types.InlineKeyboardButton(text="📊 Статистика", callback_data="menu:stats"),
            types.InlineKeyboardButton(text="ℹ️ Справка",   callback_data="menu:help"),
        ],
    ])

def timers_kb() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [
            types.InlineKeyboardButton(text="5 мин", callback_data="timer:5"),
            types.InlineKeyboardButton(text="15 мин", callback_data="timer:15"),
            types.InlineKeyboardButton(text="30 мин", callback_data="timer:30"),
        ],
        [types.InlineKeyboardButton(text="Свой…", callback_data="timer:custom")],
        [types.InlineKeyboardButton(text="⬅️ Главное меню", callback_data="menu:root")],
    ])

# ---------- Texts ----------
WELCOME_TEXT = (
    "Привет! Я AntiZalipBot 👋\n"
    "Я помогаю остановить прокрастинацию и вернуть фокус.\n\n"
    "Можно жать на кнопки или писать своими словами — отвечу как тренер.\n"
    "Выбери действие ниже:"
)
HELP_TEXT = (
    "Что я умею:\n"
    "• 🛑 «Я залип» — короткий челлендж, чтобы вернуться в действие\n"
    "• 💬 Свободный чат — опиши ситуацию, получи план на 2–4 фразы\n"
    "• ⏳ Таймер 5/15/30 или свой\n"
    "• 📊 /stats — победы, минуты свободы, переписки с тренером\n\n"
    "Примеры фраз:\n"
    "• «Откладываю задачу»\n"
    "• «Не могу собраться начать»\n"
    "• «Уже час занимаюсь не тем»\n"
    "• «Залип в телефон»\n"
    "• «Отвлёкся и потерял фокус»"
)

# ---------- States ----------
class TimerStates(StatesGroup):
    waiting_minutes = State()

class AskStates(StatesGroup):
    waiting_input = State()

# ---------- Timers ----------
active_timers: dict[int, asyncio.Task] = {}
timer_meta: dict[int, tuple[datetime, int]] = {}  # uid -> (until_utc, minutes)

async def cancel_user_timer(uid: int):
    t = active_timers.pop(uid, None)
    timer_meta.pop(uid, None)
    if t and not t.done():
        t.cancel()

async def schedule_timer(chat_id: int, uid: int, minutes: int):
    try:
        await asyncio.sleep(minutes * 60)
        await log_event(uid, "timer_done", minutes)
        active_timers.pop(uid, None)
        timer_meta.pop(uid, None)
        await bot.send_message(
            chat_id,
            f"⏰ {minutes} минут вышло! Что дальше?",
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="🔁 Ещё столько же", callback_data="timer:again")],
                [types.InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:root")],
            ])
        )
    except asyncio.CancelledError:
        pass

async def start_timer(chat_id: int, uid: int, minutes: int):
    await cancel_user_timer(uid)
    until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    timer_meta[uid] = (until, minutes)
    task = asyncio.create_task(schedule_timer(chat_id, uid, minutes))
    active_timers[uid] = task

# ---------- Nudge (show once) ----------
async def maybe_show_free_chat_nudge(uid: int, chat_id: int):
    if await get_seen_nudge(uid):
        return
    m = await bot.send_message(
        chat_id,
        "💬 Можно писать своими словами. Примеры:\n"
        "• «Откладываю задачу»\n"
        "• «Не могу собраться начать»\n"
        "• «Уже час занимаюсь не тем»",
    )
    asyncio.create_task(delete_after(chat_id, m.message_id, 20))
    await set_seen_nudge(uid)

# ---------- Commands ----------
@dp.message(Command("start"))
async def cmd_start(msg: types.Message):
    await ensure_user(msg.from_user.id)
    await log_event(msg.from_user.id, "start")
    await send_clean(msg.chat.id, msg.from_user.id, WELCOME_TEXT, reply_markup=main_menu_kb())
    await maybe_show_free_chat_nudge(msg.from_user.id, msg.chat.id)

@dp.message(Command("menu"))
async def cmd_menu(msg: types.Message):
    await send_clean(msg.chat.id, msg.from_user.id, "Главное меню:", reply_markup=main_menu_kb())

@dp.message(Command("help"))
async def cmd_help(msg: types.Message):
    await send_clean(msg.chat.id, msg.from_user.id, HELP_TEXT, reply_markup=main_menu_kb())

@dp.message(Command("stop"))
async def cmd_stop(msg: types.Message):
    await cancel_user_timer(msg.from_user.id)
    await send_clean(msg.chat.id, msg.from_user.id, "⏹ Таймер остановлен.", reply_markup=main_menu_kb())

@dp.message(Command("stats"))
async def cmd_stats(msg: types.Message):
    s = await fetch_stats(msg.from_user.id)
    chats = await free_chat_count_today(msg.from_user.id)
    text = (
        "📊 Статистика:\n"
        f"🏆 Победы над Залипатором: {s['wins']}\n"
        f"🙈 Сдачи Залипатору: {s['loses']}\n"
        f"⏳ Минуты свободы: {s['reclaimed']}\n"
        f"⏰ Таймеров сегодня: {s['today_cnt']} (мин: {s['today_min']})\n"
        f"💬 Сообщений тренеру сегодня: {chats}"
    )
    await send_clean(msg.chat.id, msg.from_user.id, text, reply_markup=main_menu_kb())

@dp.message(Command("ai_status"))
async def ai_status(msg: types.Message):
    ok = bool(oa_client and OPENAI_API_KEY)
    used = await ai_calls_today(msg.from_user.id)
    left = max(0, MAX_AI_CALLS_PER_DAY - used)
    spent = await total_spend_today()
    text = (
        f"🤖 AI: {'ON ✅' if ok else 'OFF ❌'}\n"
        f"Model: {MODEL_NAME}\nBase: {OPENAI_BASE_URL or 'default'}\n"
        f"Персональный лимит: {used}/{MAX_AI_CALLS_PER_DAY} (осталось {left})\n"
        f"Глобальный расход: ${spent:.4f}/{MAX_DAILY_SPEND:.2f}"
    )
    await send_clean(msg.chat.id, msg.from_user.id, text, reply_markup=main_menu_kb())

@dp.message(Command("adm_digest_now"))
async def adm_digest_now(msg: types.Message):
    await _send_digest_to_user(msg.from_user.id)
    await send_clean(msg.chat.id, msg.from_user.id, "✅ Отправил тестовый дайджест.", reply_markup=main_menu_kb())

# ---------- Menu callbacks ----------
@dp.callback_query(F.data == "menu:root")
async def cb_menu_root(call: types.CallbackQuery):
    try:
        await call.message.edit_text(WELCOME_TEXT, reply_markup=main_menu_kb())
    except Exception:
        await send_clean(call.message.chat.id, call.from_user.id, WELCOME_TEXT, reply_markup=main_menu_kb())
    finally:
        await call.answer()

@dp.callback_query(F.data == "menu:help")
async def cb_menu_help(call: types.CallbackQuery):
    try:
        await call.message.edit_text(HELP_TEXT, reply_markup=main_menu_kb())
    except Exception:
        await send_clean(call.message.chat.id, call.from_user.id, HELP_TEXT, reply_markup=main_menu_kb())
    finally:
        await call.answer()

@dp.callback_query(F.data == "menu:stats")
async def cb_menu_stats(call: types.CallbackQuery):
    s = await fetch_stats(call.from_user.id)
    chats = await free_chat_count_today(call.from_user.id)
    text = (
        "📊 Статистика:\n"
        f"🏆 Победы над Залипатором: {s['wins']}\n"
        f"🙈 Сдачи Залипатору: {s['loses']}\n"
        f"⏳ Минуты свободы: {s['reclaimed']}\n"
        f"⏰ Таймеров сегодня: {s['today_cnt']} (мин: {s['today_min']})\n"
        f"💬 Сообщений тренеру сегодня: {chats}"
    )
    try:
        await call.message.edit_text(text, reply_markup=main_menu_kb())
    except Exception:
        await send_clean(call.message.chat.id, call.from_user.id, text, reply_markup=main_menu_kb())
    finally:
        await call.answer()

# ---------- Timers ----------
@dp.callback_query(F.data == "menu:timer")
async def cb_menu_timer(call: types.CallbackQuery):
    try:
        await call.message.edit_text("Выбери длительность таймера:", reply_markup=timers_kb())
    except Exception:
        await send_clean(call.message.chat.id, call.from_user.id, "Выбери длительность таймера:", reply_markup=timers_kb())
    await call.answer()

@dp.callback_query(F.data.startswith("timer:"))
async def cb_timer(call: types.CallbackQuery, state: FSMContext):
    uid = call.from_user.id
    chat_id = call.message.chat.id
    data = call.data

    if data == "timer:custom":
        await state.set_state(TimerStates.waiting_minutes)
        try:
            await call.message.edit_text(
                "Введи число минут (1–180):",
                reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                    [types.InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:timer")]
                ])
            )
        except Exception:
            await send_clean(chat_id, uid, "Введи число минут (1–180):",
                             reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                                 [types.InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:timer")]
                             ]))
        await call.answer()
        return

    if data == "timer:again":
        minutes = timer_meta.get(uid, (None, 15))[1]
    else:
        minutes = int(data.split(":")[1])

    await start_timer(chat_id, uid, minutes)
    try:
        await call.message.edit_text(
            f"✅ Таймер включён на {minutes} мин.",
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="⏹ Остановить", callback_data="timer:stop")],
                [types.InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:root")],
            ])
        )
    except Exception:
        await send_clean(chat_id, uid, f"✅ Таймер включён на {minutes} мин.",
                         reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                             [types.InlineKeyboardButton(text="⏹ Остановить", callback_data="timer:stop")],
                             [types.InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:root")],
                         ]))
    await call.answer()

@dp.callback_query(F.data == "timer:stop")
async def cb_timer_stop(call: types.CallbackQuery):
    await cancel_user_timer(call.from_user.id)
    try:
        await call.message.edit_text("⛔️ Таймер остановлен.", reply_markup=main_menu_kb())
    except Exception:
        await send_clean(call.message.chat.id, call.from_user.id, "⛔️ Таймер остановлен.", reply_markup=main_menu_kb())
    await call.answer()

@dp.message(TimerStates.waiting_minutes, F.text)
async def custom_minutes_input(msg: types.Message, state: FSMContext):
    text = (msg.text or "").strip()
    if not text.isdigit():
        await send_clean(msg.chat.id, msg.from_user.id, "Нужно целое число 1–180. Попробуй снова или /menu.",
                         reply_markup=main_menu_kb())
        return
    n = int(text)
    if not (1 <= n <= 180):
        await send_clean(msg.chat.id, msg.from_user.id, "Допустимый диапазон: 1–180 минут. Попробуй снова.",
                         reply_markup=main_menu_kb())
        return
    await state.clear()
    await start_timer(msg.chat.id, msg.from_user.id, n)
    await send_clean(
        msg.chat.id, msg.from_user.id, f"✅ Таймер включён на {n} мин.",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="⏹ Остановить", callback_data="timer:stop")],
            [types.InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:root")],
        ])
    )

# ---------- Battle ----------
@dp.callback_query(F.data == "battle:start")
async def cb_battle(call: types.CallbackQuery):
    uid = call.from_user.id
    await ensure_user(uid)
    challenge = random.choice([
        "Встань и сделай 10 шагов 🚶",
        "Выпей стакан воды 💧",
        "Закрой лишнюю вкладку/окно 🗂️",
        "Сделай 5 приседаний 💪",
        "Потянись и сделай 5 глубоких вдохов 🌬️",
    ])
    text = f"⚔️ Залипатор тянет!\n\n🎯 Челлендж: {challenge}\nСправишься?"
    try:
        await call.message.edit_text(
            text,
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="✅ Сделал", callback_data="battle:win")],
                [types.InlineKeyboardButton(text="❌ Сдался", callback_data="battle:lose")],
            ])
        )
    except Exception:
        await send_clean(call.message.chat.id, uid, text,
                         reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                             [types.InlineKeyboardButton(text="✅ Сделал", callback_data="battle:win")],
                             [types.InlineKeyboardButton(text="❌ Сдался", callback_data="battle:lose")],
                         ]))
    await call.answer()

@dp.callback_query(F.data == "battle:win")
async def cb_battle_win(call: types.CallbackQuery):
    uid = call.from_user.id
    await log_event(uid, "battle_win", 5)
    s = await fetch_stats(uid)
    await call.message.edit_text(
        f"🏆 Победа! Вернул 5 минут фокуса.\n"
        f"Побед: {s['wins']} | Сдач: {s['loses']}",
        reply_markup=main_menu_kb()
    )
    await call.answer()

@dp.callback_query(F.data == "battle:lose")
async def cb_battle_lose(call: types.CallbackQuery):
    uid = call.from_user.id
    await log_event(uid, "battle_lose", 0)
    s = await fetch_stats(uid)
    await call.message.edit_text(
        f"😈 Залипатор взял маленькую победу…\n"
        f"Побед: {s['wins']} | Сдач: {s['loses']}\n\n"
        "Жми «🛑 Я залип» снова, чтобы отыграться.",
        reply_markup=main_menu_kb()
    )
    await call.answer()

# ---------- Free chat ----------
@dp.callback_query(F.data == "ask")
async def cb_ask(call: types.CallbackQuery, state: FSMContext):
    await state.set_state(AskStates.waiting_input)
    # Без ForceReply — просто мягкая подсказка + placeholder
    text = "Опиши в 1–2 предложениях: что происходит и чем помочь."
    try:
        await call.message.edit_text(text, reply_markup=main_menu_kb())
        m = await bot.send_message(call.message.chat.id, "Жду твоё сообщение…",
                                   input_field_placeholder="Опиши ситуацию…")
        asyncio.create_task(delete_after(call.message.chat.id, m.message_id, 15))
    except Exception:
        await send_clean(call.message.chat.id, call.from_user.id, text, reply_markup=main_menu_kb())
    await call.answer()

@dp.message(AskStates.waiting_input, F.text)
async def ask_input(msg: types.Message, state: FSMContext):
    await state.clear()
    uid = msg.from_user.id
    await ensure_user(uid)
    await log_event(uid, "free_chat")
    try: await bot.send_chat_action(msg.chat.id, ChatAction.TYPING)
    except: pass
    prompt = (
        f"Пользователь просит совет: «{msg.text}».\n"
        "Ответь как тренер по фокусу: 2–4 фразы, конкретный микро-шаг ≤2 минут. "
        "Без психологии, только действие и фокус."
    )
    reply = await ai_reply(uid, prompt, 0.9, 400)
    await send_clean(msg.chat.id, uid, reply, reply_markup=main_menu_kb())

@dp.message(F.text & ~F.text.startswith("/"))
async def free_chat(msg: types.Message):
    uid = msg.from_user.id
    await ensure_user(uid)
    await log_event(uid, "free_chat")
    try: await bot.send_chat_action(msg.chat.id, ChatAction.TYPING)
    except: pass
    prompt = (
        f"Пользователь: «{msg.text}».\n"
        "Ответь кратко как коуч по фокусу: 2–3 фразы, один понятный микро-шаг ≤2 минут."
    )
    reply = await ai_reply(uid, prompt, 0.8, 400)
    await send_clean(msg.chat.id, uid, reply, reply_markup=main_menu_kb())

# ---------- Sleep preset (пример) ----------
@dp.callback_query(F.data == "sleep:30")
async def cb_sleep(call: types.CallbackQuery):
    uid = call.from_user.id
    await start_timer(call.message.chat.id, uid, 30)
    try:
        await call.message.edit_text(
            "🌙 Режим сна: через 30 минут — отбой.\n"
            "До этого: тёплый душ, убавь яркость экрана, лёгкий текст без новостей.",
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="⏹ Остановить", callback_data="timer:stop")],
                [types.InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:root")],
            ])
        )
    except Exception:
        await send_clean(call.message.chat.id, uid,
                         "🌙 Режим сна: через 30 минут — отбой.\n"
                         "До этого: тёплый душ, убавь яркость экрана, лёгкий текст без новостей.",
                         reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                             [types.InlineKeyboardButton(text="⏹ Остановить", callback_data="timer:stop")],
                             [types.InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:root")],
                         ]))
    await call.answer()

# ---------- Nightly digest ----------
async def _send_digest_to_user(uid: int):
    s = await fetch_stats(uid)
    chats = await free_chat_count_today(uid)
    prompt = (
        f"Сводка дня: победы={s['wins']}, сдачи={s['loses']}, свобода={s['reclaimed']} мин, "
        f"таймеров сегодня={s['today_cnt']} ({s['today_min']} мин), чатов={chats}. "
        "Сделай 2–3 фразы: тёплая похвала + один конкретный совет на завтра (короткий)."
    )
    ai = await ai_reply(uid, prompt, 0.8, 280)
    text = (
        "🌙 Вечерний итог\n"
        f"🏆 Победы/сдачи: {s['wins']} / {s['loses']}\n"
        f"⏳ Минуты свободы: {s['reclaimed']}\n"
        f"⏰ Таймеров: {s['today_cnt']} ({s['today_min']} мин)\n"
        f"💬 Сообщений тренеру: {chats}\n\n"
        f"{ai}"
    )
    try:
        await bot.send_message(uid, text, reply_markup=main_menu_kb())
    except Exception as e:
        log.warning(f"digest send fail {uid}: {e}")

async def nightly_digest_loop():
    tz = ZoneInfo(DIGEST_TZ)
    while True:
        try:
            now = datetime.now(tz)
            today = date.today().isoformat()
            if now.hour == DIGEST_HOUR:
                async with aiosqlite.connect(DB_PATH) as db:
                    cur = await db.execute("SELECT user_id, COALESCE(last_digest_date,'') FROM users")
                    rows = await cur.fetchall()
                for uid, last_day in rows:
                    if last_day == today:
                        continue
                    await _send_digest_to_user(uid)
                    async with aiosqlite.connect(DB_PATH) as db:
                        await db.execute("UPDATE users SET last_digest_date=? WHERE user_id=?", (today, uid))
                        await db.commit()
                await asyncio.sleep(65 * 60)
            else:
                await asyncio.sleep(5 * 60)
        except Exception as e:
            log.warning(f"digest loop err: {e}")
            await asyncio.sleep(60)

# ---------- Healthcheck + Webhook ----------
async def _health(_req): return web.Response(text="OK")

from aiogram.types import Update

async def webhook_handler(request: web.Request):
    try:
        data = await request.json()
    except Exception:
        return web.Response(status=400, text="bad json")
    try:
        update = Update.model_validate(data)
        await dp.feed_update(bot, update)
    except Exception as e:
        log.exception(f"webhook error: {e}")
    return web.Response(text="ok")

async def set_webhook():
    if not BASE_URL:
        log.warning("BASE_URL не задан — webhook не настраивается.")
        return
    url = f"{BASE_URL}/webhook/{WEBHOOK_SECRET}"
    await bot.set_webhook(url, drop_pending_updates=True)
    log.info(f"🔔 Webhook set: {url}")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", _health)
    app.router.add_get("/health", _health)
    app.router.add_post(f"/webhook/{WEBHOOK_SECRET}", webhook_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "10000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info(f"🌐 Web server started on :{port}")
    await set_webhook()

# ---------- main (keep-alive loop) ----------
async def main():
    await init_db()

    # стартуем веб-сервер с healthcheck и webhook-роутом
    asyncio.create_task(start_web_server())

    # ночной дайджест в фоне
    asyncio.create_task(nightly_digest_loop())

    # держим процесс живым, даже если фоновые таски упадут
    try:
        while True:
            await asyncio.sleep(3600)  # раз в час просто «живём»
    except asyncio.CancelledError:
        pass  # корректное завершение по сигналу

if __name__ == "__main__":
    asyncio.run(main())
