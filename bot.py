# bot.py — AntiZalipBot (финал)
# Фичи:
# - Понятные тексты (Залипатор), меню и действия
# - Битва с Залипатором (челленджи, победы/сдачи, «минуты свободы»)
# - Таймеры (5/15/30/свой), /stop, учёт «timer_done»
# - Свободный чат (кнопка + нудж + ForceReply), ИИ-коуч
# - Nightly-дайджест (ежевечерняя сводка с ИИ-комментом)
# - Лимиты: персональный по ИИ-вызовам/сутки и глобальный лимит по $ в сутки
# - Healthcheck web-сервер (для Render Free)
#
# ENV:
#   TELEGRAM_TOKEN=...
#   OPENAI_API_KEY=...            # опционально; если нет — фоллбек-фразы
#   OPENAI_BASE_URL=...           # опционально (совместимый провайдер)
#   MODEL_NAME=gpt-4o-mini
#   PYTHON_VERSION=3.12.5         # на Render
#   DIGEST_TZ=Europe/Moscow
#   DIGEST_HOUR=22
#   MAX_AI_CALLS_PER_DAY=30
#   MAX_DAILY_SPEND=1.0

import os
import asyncio
import logging
import random
from datetime import datetime, timedelta, date, timezone
from zoneinfo import ZoneInfo

import aiosqlite
from dotenv import load_dotenv
from aiohttp import web

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.enums import ChatAction
from aiogram.types import ForceReply
from aiogram.client.session.aiohttp import AiohttpSession

# ИИ-клиент
try:
    from openai import AsyncOpenAI
except Exception:
    AsyncOpenAI = None

# ---------- Конфиг ----------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN не найден. Добавь в .env или Environment.")

OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL")
MODEL_NAME      = os.getenv("MODEL_NAME", "gpt-4o-mini")

DIGEST_TZ   = os.getenv("DIGEST_TZ", "Europe/Moscow")
DIGEST_HOUR = int(os.getenv("DIGEST_HOUR", "22"))

MAX_AI_CALLS_PER_DAY = int(os.getenv("MAX_AI_CALLS_PER_DAY", "30"))
MAX_DAILY_SPEND      = float(os.getenv("MAX_DAILY_SPEND", "1.0"))

# ---------- Логи ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("AntiZalipBot")

# ---------- Aiogram ----------
session = AiohttpSession()
bot = Bot(TOKEN, session=session)
dp = Dispatcher()

# ---------- OpenAI ----------
oa_client = None
if OPENAI_API_KEY and AsyncOpenAI:
    try:
        oa_client = AsyncOpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL)
        log.info("✅ OpenAI client initialized")
    except Exception as e:
        log.warning(f"OpenAI init failed: {e}")

# ---------- БД ----------
DB_PATH = os.path.join(BASE_DIR, "bot.db")

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            created TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_digest_date TEXT
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            event TEXT,
            value REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        await db.commit()

async def ensure_user(uid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (uid,))
        await db.commit()

async def log_event(uid: int, event: str, value: float | None = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO events(user_id,event,value) VALUES(?,?,?)", (uid, event, value))
        await db.commit()

# ---------- Статистика ----------
async def fetch_stats(uid: int) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        # победы/сдачи/минуты свободы (по битвам)
        cur = await db.execute("SELECT COUNT(*) FROM events WHERE user_id=? AND event='battle_win'", (uid,))
        wins, = await cur.fetchone()
        cur = await db.execute("SELECT COUNT(*) FROM events WHERE user_id=? AND event='battle_lose'", (uid,))
        loses, = await cur.fetchone()
        cur = await db.execute("SELECT COALESCE(SUM(value),0) FROM events WHERE user_id=? AND event='battle_win'", (uid,))
        reclaimed, = await cur.fetchone()
        # сегодня по таймерам
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
        "today_min": int(today_min or 0)
    }

async def free_chat_count_today(uid: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT COUNT(*) FROM events
            WHERE user_id=? AND event='free_chat' AND substr(created_at,1,10)=date('now')
        """, (uid,))
        n, = await cur.fetchone()
    return int(n or 0)

# ---------- Лимиты ИИ ----------
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

async def mark_ai_call(uid: int):
    await log_event(uid, "ai_call", 1)

async def mark_ai_block(uid: int):
    await log_event(uid, "ai_block", 1)

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

# ---------- ИИ генерация ----------
SYSTEM_COACH = "Ты жёсткий, но заботливый тренер внимательности. Отвечай кратко, по делу, дружелюбно."

async def ai_generate(prompt: str, temperature: float = 0.8, max_tokens: int = 200) -> str | None:
    if not oa_client:
        # фоллбек, если ИИ недоступен
        return random.choice([
            "Сделай паузу на 60 секунд, расправь плечи и выбери одно простое действие.",
            "Залипатор жрёт минуты — верни себе 2 минуты внимания прямо сейчас.",
            "Встань, глоток воды, 10 глубоких вдохов — и вперёд к одному маленькому шагу."
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
    # персональный лимит
    if not await can_use_ai(uid):
        await mark_ai_block(uid)
        return random.choice([
            "Сегодня ты уже много общался с тренером 🤖. Оффлайн-режим: 60 сек дыхания и одно простое действие.",
            "Пауза ИИ на сегодня. Сделай 2-минутный микро-шаг и зафиксируй победу.",
            "Без ИИ: вода, дыхание, один крошечный шаг — и ты снова в фокусе."
        ])
    # глобальный лимит $ (очень грубая оценка стоимости по max_tokens)
    est_cost = (max_tokens / 1000.0) * 0.0006  # ~ $0.0006 за 1 токен ответа
    if not await can_spend(est_cost):
        await mark_ai_block(uid)
        return "Сегодня общий лимит расходов бота достигнут 💸. Завтра продолжим в ИИ-режиме. Пока сделай офлайн-мини-шаг!"

    # генерация
    text = await ai_generate(prompt, temperature=temperature, max_tokens=max_tokens)
    await mark_ai_call(uid)
    await add_spend(est_cost)
    return text or random.choice([
        "Сделай паузу, расправь плечи, подыши. Потом одно простое действие.",
        "Хватит кормить Залипатора временем. Встань, глоток воды — и делай один шаг."
    ])

# ---------- Кнопки ----------
def main_menu_kb() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="🛑 Я залип", callback_data="battle:start")],
        [types.InlineKeyboardButton(text="💬 Написать тренеру", callback_data="ask")],
        [types.InlineKeyboardButton(text="⏳ Поставить таймер", callback_data="menu:timer")],
        [types.InlineKeyboardButton(text="🛌 Режим сна (30 мин)", callback_data="sleep:30")],
        [
            types.InlineKeyboardButton(text="📊 Статистика", callback_data="menu:stats"),
            types.InlineKeyboardButton(text="ℹ️ Справка", callback_data="menu:help"),
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

# ---------- Тексты ----------
WELCOME_TEXT = (
    "Привет! Я AntiZalipBot 👋\n"
    "Помогу вырваться из лап Залипатора и вернуть кайф от жизни.\n\n"
    "Можно жать на кнопки или просто писать своими словами, что происходит. "
    "Я отвечу как жёсткий, но заботливый тренер.\n\n"
    "Выбери действие ниже:"
)
HELP_TEXT = (
    "Я помогаю перестать залипать и вернуть контроль над вниманием.\n\n"
    "Что умею:\n"
    "• 🛑 «Я залип» — вызову на битву с Залипатором\n"
    "• 💬 Свободный чат — можно писать своими словами, как тренеру\n"
    "• ⏳ Таймер 5/15/30 или свой\n"
    "• 📊 /stats — твои победы, минуты свободы и переписки с тренером\n\n"
    "Примеры: «я залип в рилсах», «нет сил начать», «сорвался и неловко», «хочу план на вечер»."
)

# ---------- Состояния ----------
class TimerStates(StatesGroup):
    waiting_minutes = State()

class AskStates(StatesGroup):
    waiting_input = State()

# ---------- Таймеры ----------
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
        await bot.send_message(chat_id, f"⏰ {minutes} минут вышло! Что дальше?",
                               reply_markup=types.InlineKeyboardMarkup(
                                   inline_keyboard=[
                                       [types.InlineKeyboardButton(text="🔁 Ещё столько же", callback_data="timer:again")],
                                       [types.InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:root")],
                                   ]
                               ))
    except asyncio.CancelledError:
        pass

async def start_timer(chat_id: int, uid: int, minutes: int):
    await cancel_user_timer(uid)
    until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    timer_meta[uid] = (until, minutes)
    task = asyncio.create_task(schedule_timer(chat_id, uid, minutes))
    active_timers[uid] = task

# ---------- Нудж про свободный чат ----------
async def maybe_show_free_chat_nudge(uid: int, chat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT 1 FROM events WHERE user_id=? AND event='free_chat' LIMIT 1", (uid,))
        row = await cur.fetchone()
    if row:
        return
    await bot.send_message(
        chat_id,
        "💬 Можно писать своими словами.\nПримеры:\n• «я залип в рилсах»\n• «нет сил начать»\n• «выгорел»",
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[[types.InlineKeyboardButton(text="💬 Написать тренеру", callback_data="ask")]]
        )
    )

# ---------- Команды ----------
@dp.message(Command("start"))
async def cmd_start(msg: types.Message):
    await ensure_user(msg.from_user.id)
    await log_event(msg.from_user.id, "start")
    await msg.answer(WELCOME_TEXT, reply_markup=main_menu_kb())
    await maybe_show_free_chat_nudge(msg.from_user.id, msg.chat.id)

@dp.message(Command("menu"))
async def cmd_menu(msg: types.Message):
    await msg.answer("Главное меню:", reply_markup=main_menu_kb())
    await maybe_show_free_chat_nudge(msg.from_user.id, msg.chat.id)

@dp.message(Command("help"))
async def cmd_help(msg: types.Message):
    await msg.answer(HELP_TEXT, reply_markup=main_menu_kb())

@dp.message(Command("stop"))
async def cmd_stop(msg: types.Message):
    await cancel_user_timer(msg.from_user.id)
    await msg.answer("⏹ Таймер остановлен.", reply_markup=main_menu_kb())

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
    await msg.answer(text, reply_markup=main_menu_kb())

@dp.message(Command("ai_status"))
async def ai_status(msg: types.Message):
    ok = bool(oa_client and OPENAI_API_KEY)
    used = await ai_calls_today(msg.from_user.id)
    left = max(0, MAX_AI_CALLS_PER_DAY - used)
    spent = await total_spend_today()
    await msg.answer(
        f"🤖 AI: {'ON ✅' if ok else 'OFF ❌'}\n"
        f"Model: {MODEL_NAME}\nBase: {OPENAI_BASE_URL or 'default'}\n"
        f"Персональный лимит: {used}/{MAX_AI_CALLS_PER_DAY} (осталось {left})\n"
        f"Глобальный расход: ${spent:.4f}/{MAX_DAILY_SPEND:.2f}"
    )

@dp.message(Command("adm_digest_now"))
async def adm_digest_now(msg: types.Message):
    await _send_digest_to_user(msg.from_user.id)
    await msg.answer("✅ Отправил тестовый дайджест.")

# ---------- Меню (callback) ----------
@dp.callback_query(F.data == "menu:root")
async def cb_menu_root(call: types.CallbackQuery):
    await call.message.edit_text(WELCOME_TEXT, reply_markup=main_menu_kb())
    await call.answer()

@dp.callback_query(F.data == "menu:help")
async def cb_menu_help(call: types.CallbackQuery):
    await call.message.edit_text(HELP_TEXT, reply_markup=main_menu_kb())
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
    await call.message.edit_text(text, reply_markup=main_menu_kb())
    await call.answer()

# ---------- Таймеры (callback) ----------
@dp.callback_query(F.data == "menu:timer")
async def cb_menu_timer(call: types.CallbackQuery):
    await call.message.edit_text("Выбери длительность таймера:", reply_markup=timers_kb())
    await call.answer()

@dp.callback_query(F.data.startswith("timer:"))
async def cb_timer(call: types.CallbackQuery, state: FSMContext):
    uid = call.from_user.id
    chat_id = call.message.chat.id
    data = call.data

    if data == "timer:custom":
        await state.set_state(TimerStates.waiting_minutes)
        await call.message.edit_text(
            "Введи число минут (1–180):",
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:timer")]
            ])
        )
        await call.answer()
        return

    if data == "timer:again":
        minutes = timer_meta.get(uid, (None, 15))[1]
    else:
        minutes = int(data.split(":")[1])

    await start_timer(chat_id, uid, minutes)
    await call.message.edit_text(
        f"✅ Таймер включён на {minutes} мин.",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="⏹ Остановить", callback_data="timer:stop")],
            [types.InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:root")],
        ])
    )
    await call.answer("Поехали!")

@dp.callback_query(F.data == "timer:stop")
async def cb_timer_stop(call: types.CallbackQuery):
    await cancel_user_timer(call.from_user.id)
    await call.message.edit_text("⏹ Таймер остановлен.", reply_markup=main_menu_kb())
    await call.answer()

@dp.message(TimerStates.waiting_minutes, F.text)
async def custom_minutes_input(msg: types.Message, state: FSMContext):
    text = (msg.text or "").strip()
    if not text.isdigit():
        await msg.reply("Нужно целое число 1–180. Попробуй снова или /menu.")
        return
    n = int(text)
    if not (1 <= n <= 180):
        await msg.reply("Допустимый диапазон: 1–180 минут. Попробуй снова.")
        return
    await state.clear()
    await start_timer(msg.chat.id, msg.from_user.id, n)
    await msg.reply(
        f"✅ Таймер включён на {n} мин.",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="⏹ Остановить", callback_data="timer:stop")],
            [types.InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:root")],
        ])
    )

# ---------- Битва с Залипатором ----------
@dp.callback_query(F.data == "battle:start")
async def cb_battle(call: types.CallbackQuery):
    uid = call.from_user.id
    await ensure_user(uid)
    challenge = random.choice([
        "Сделай 10 приседаний 💪",
        "Встань, выдохни, налей воды 💧",
        "Запиши одну задачу на 2 минуты ✍️",
        "Протри стол/экран за 1 минуту 🧽",
        "Сделай 20 глубоких вдохов и расправь плечи 🫁",
    ])
    text = f"⚔️ Залипатор тянет тебя в рилсы!\n\n🎯 Челлендж: {challenge}\nСправишься?"
    await call.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="✅ Сделал", callback_data="battle:win")],
            [types.InlineKeyboardButton(text="❌ Сдался", callback_data="battle:lose")],
        ])
    )
    await call.answer()

@dp.callback_query(F.data == "battle:win")
async def cb_battle_win(call: types.CallbackQuery):
    uid = call.from_user.id
    # считаем, что отобрано 5 минут свободы
    await log_event(uid, "battle_win", 5)
    s = await fetch_stats(uid)
    await call.message.edit_text(
        f"🏆 Победа! Ты отобрал у Залипатора 5 минут свободы.\n"
        f"Всего побед: {s['wins']} | Сдач: {s['loses']}\n"
        f"⏳ Минуты свободы: {s['reclaimed']}",
        reply_markup=main_menu_kb()
    )
    await call.answer()

@dp.callback_query(F.data == "battle:lose")
async def cb_battle_lose(call: types.CallbackQuery):
    uid = call.from_user.id
    await log_event(uid, "battle_lose", 0)
    s = await fetch_stats(uid)
    await call.message.edit_text(
        f"😈 Залипатор празднует маленькую победу…\n"
        f"Побед: {s['wins']} | Сдач: {s['loses']}\n\n"
        f"Но ты всегда можешь отыграться — жми «🛑 Я залип» снова.",
        reply_markup=main_menu_kb()
    )
    await call.answer()

# ---------- Свободный чат ----------
@dp.callback_query(F.data == "ask")
async def cb_ask(call: types.CallbackQuery, state: FSMContext):
    await state.set_state(AskStates.waiting_input)
    await call.message.answer(
        "Окей, напиши в ответ одним-двумя предложениями: что происходит и чем помочь.",
        reply_markup=ForceReply(selective=True, input_field_placeholder="Опиши ситуацию…")
    )
    await call.answer()

@dp.message(AskStates.waiting_input, F.text)
async def ask_input(msg: types.Message, state: FSMContext):
    await state.clear()
    uid = msg.from_user.id
    await ensure_user(uid)
    await log_event(uid, "free_chat")
    try:
        await bot.send_chat_action(msg.chat.id, ChatAction.TYPING)
    except:
        pass
    prompt = (
        f"Пользователь просит совет: «{msg.text}».\n"
        "Ответь как жёсткий, но заботливый тренер: 2–4 короткие фразы, один микро-шаг ≤2 минуты и поддержка."
    )
    reply = await ai_reply(uid, prompt, 0.9, 400)
    await msg.answer(reply, reply_markup=main_menu_kb())

@dp.message(F.text & ~F.text.startswith("/"))
async def free_chat(msg: types.Message):
    uid = msg.from_user.id
    await ensure_user(uid)
    await log_event(uid, "free_chat")
    try:
        await bot.send_chat_action(msg.chat.id, ChatAction.TYPING)
    except:
        pass
    prompt = (
        f"Пользователь: «{msg.text}».\n"
        "Ответь как тренер внимательности: по делу, 2–3 фразы, конкретный микро-шаг."
    )
    reply = await ai_reply(uid, prompt, 0.8, 400)
    await msg.answer(reply, reply_markup=main_menu_kb())

# ---------- Режим сна ----------
@dp.callback_query(F.data == "sleep:30")
async def cb_sleep(call: types.CallbackQuery):
    uid = call.from_user.id
    await start_timer(call.message.chat.id, uid, 30)
    await call.message.edit_text(
        "🌙 Режим сна: через 30 минут — отбой.\n"
        "До этого:\n• тёплый душ\n• убавь экранный свет\n• лёгкий текст без новостей",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="⏹ Остановить", callback_data="timer:stop")],
            [types.InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:root")],
        ])
    )
    await call.answer()

# ---------- Ночной дайджест ----------
async def _send_digest_to_user(uid: int):
    s = await fetch_stats(uid)
    chats = await free_chat_count_today(uid)
    prompt = (
        f"Сводка дня: победы={s['wins']}, сдачи={s['loses']}, свобода={s['reclaimed']} мин, "
        f"таймеров сегодня={s['today_cnt']} ({s['today_min']} мин), чатов={chats}. "
        "Сделай 2–3 фразы: тёплая похвала + один конкретный совет на завтра."
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

# ---------- Healthcheck ----------
async def _health(_req): return web.Response(text="OK")
async def start_web_server():
    app = web.Application()
    app.router.add_get("/", _health)
    app.router.add_get("/health", _health)
    runner = web.AppRunner(app); await runner.setup()
    port = int(os.getenv("PORT", "10000"))
    site = web.TCPSite(runner, "0.0.0.0", port); await site.start()
    log.info(f"🌐 Healthcheck started on {port}")

# ---------- main ----------
async def main():
    await init_db()
    asyncio.create_task(start_web_server())
    asyncio.create_task(nightly_digest_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())