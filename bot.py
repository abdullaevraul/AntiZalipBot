# AntiZalipBot — Render webhook + Supabase + PostHog
# Python 3.12, aiogram 3.5
# UX: онбординг, "Начать сейчас" (переименовано), таймеры, пост-таймер опрос,
# причины "почему не получилось", помощник (ИИ), обратная связь, чистка чата,
# сторож вебхука.

import os
import asyncio
import logging
import random
from datetime import datetime, timedelta, date, timezone
from zoneinfo import ZoneInfo

import asyncpg
import httpx
from dotenv import load_dotenv
from aiohttp import web

from aiogram import Bot, Dispatcher, F, types
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ChatAction
from aiogram.filters import Command, Text
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Update

# ---------- Optional OpenAI ----------
try:
    from openai import AsyncOpenAI
except Exception:
    AsyncOpenAI = None

# ---------- ENV ----------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN is missing")

BASE_URL       = os.getenv("BASE_URL")  # https://antizalipbot.onrender.com
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "antizalip_secret")

OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL")
MODEL_NAME      = os.getenv("MODEL_NAME", "gpt-4o-mini")

DATABASE_URL = os.getenv("DATABASE_URL")
POSTHOG_API_KEY = os.getenv("POSTHOG_API_KEY")
POSTHOG_HOST    = os.getenv("POSTHOG_HOST", "https://app.posthog.com")

DIGEST_TZ   = os.getenv("DIGEST_TZ", "Europe/Moscow")
DIGEST_HOUR = int(os.getenv("DIGEST_HOUR", "22"))

MAX_AI_CALLS_PER_DAY = int(os.getenv("MAX_AI_CALLS_PER_DAY", "30"))
MAX_DAILY_SPEND      = float(os.getenv("MAX_DAILY_SPEND", "1.0"))

ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()}

# ---------- Logging ----------
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

# ---------- DB ----------
DB_POOL: asyncpg.Pool | None = None

async def get_pool() -> asyncpg.Pool:
    global DB_POOL
    if DB_POOL is None:
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL is missing")
        DB_POOL = await asyncpg.create_pool(DATABASE_URL, max_size=10)
    return DB_POOL

async def init_db():
    pool = await get_pool()
    async with pool.acquire() as con:
        await con.execute("""
        CREATE TABLE IF NOT EXISTS users(
          user_id BIGINT PRIMARY KEY,
          created_at TIMESTAMPTZ DEFAULT now(),
          seen_nudge BOOLEAN DEFAULT FALSE,
          last_digest_date DATE,
          personal_context TEXT
        )""")
        await con.execute("""
        CREATE TABLE IF NOT EXISTS events(
          id BIGSERIAL PRIMARY KEY,
          user_id BIGINT NOT NULL,
          event TEXT NOT NULL,
          value NUMERIC,
          created_at TIMESTAMPTZ DEFAULT now()
        )""")
        await con.execute("""
        CREATE TABLE IF NOT EXISTS messages(
          id BIGSERIAL PRIMARY KEY,
          user_id BIGINT NOT NULL,
          kind TEXT NOT NULL,
          text TEXT NOT NULL,
          created_at TIMESTAMPTZ DEFAULT now()
        )""")
        await con.execute("CREATE INDEX IF NOT EXISTS idx_events_user_created ON events(user_id, created_at DESC)")
        await con.execute("CREATE INDEX IF NOT EXISTS idx_events_event_created ON events(event, created_at DESC)")
        await con.execute("CREATE INDEX IF NOT EXISTS idx_messages_user_created ON messages(user_id, created_at DESC)")

async def ensure_user(uid: int):
    pool = await get_pool()
    async with pool.acquire() as con:
        await con.execute("INSERT INTO users(user_id) VALUES($1) ON CONFLICT (user_id) DO NOTHING", uid)

async def set_personal_context(uid: int, context: str | None):
    pool = await get_pool()
    async with pool.acquire() as con:
        await con.execute("UPDATE users SET personal_context=$1 WHERE user_id=$2", context, uid)

async def get_personal_context(uid: int) -> str | None:
    pool = await get_pool()
    async with pool.acquire() as con:
        return await con.fetchval("SELECT personal_context FROM users WHERE user_id=$1", uid)

async def log_event(uid: int, event: str, value: float | None = None):
    pool = await get_pool()
    async with pool.acquire() as con:
        await con.execute("INSERT INTO events(user_id,event,value) VALUES($1,$2,$3)", uid, event, value)

async def store_message(uid: int, kind: str, text: str):
    pool = await get_pool()
    async with pool.acquire() as con:
        await con.execute("INSERT INTO messages(user_id,kind,text) VALUES($1,$2,$3)", uid, kind, text)

async def fetch_stats(uid: int) -> dict:
    pool = await get_pool()
    async with pool.acquire() as con:
        wins  = await con.fetchval("SELECT COUNT(*) FROM events WHERE user_id=$1 AND event='posttimer_win'", uid) or 0
        loses = await con.fetchval("SELECT COUNT(*) FROM events WHERE user_id=$1 AND event='posttimer_fail'", uid) or 0
        total_focus = await con.fetchval("SELECT COALESCE(SUM(value),0) FROM events WHERE user_id=$1 AND event='timer_done'", uid) or 0
        today = await con.fetchrow("""
            SELECT COUNT(*) AS cnt, COALESCE(SUM(value),0) AS minutes
            FROM events
            WHERE user_id=$1 AND event='timer_done' AND created_at::date=CURRENT_DATE
        """, uid)
    return {
        "wins": int(wins), "loses": int(loses), "reclaimed": int(total_focus),
        "today_cnt": int(today["cnt"] or 0), "today_min": int(today["minutes"] or 0)
    }

# ---------- Analytics (PostHog) ----------
async def track(uid: int, event: str, props: dict | None = None):
    if not POSTHOG_API_KEY:
        return
    payload = {"api_key": POSTHOG_API_KEY, "event": event, "distinct_id": str(uid), "properties": props or {}}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(f"{POSTHOG_HOST}/capture/", json=payload)
    except Exception:
        pass

# ---------- AI ----------
SYSTEM_COACH = (
    "Ты помогаешь бороться с прокрастинацией. Коротко, по делу, 2–3 фразы. "
    "Дай один конкретный шаг на 5–15 минут. Без психотерапии и оценок."
)

async def ai_generate(prompt: str, temperature: float = 0.8, max_tokens: int = 220) -> str | None:
    if not oa_client:
        return random.choice([
            "Выбери одну простую часть задачи и удели ей 10–15 минут. Начни с малого — остальное подтянется.",
            "Убери лишнее с глаз, поставь таймер на 15 минут и займись одной вещью. Потом решишь, что дальше.",
            "Сделай быстрый старт: 5 минут на самый лёгкий шаг. Главное — переключиться в действие.",
        ])
    try:
        resp = await oa_client.chat.completions.create(
            model=MODEL_NAME, temperature=temperature, max_tokens=max_tokens,
            messages=[{"role":"system","content":SYSTEM_COACH},{"role":"user","content":prompt}],
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        log.warning(f"AI error: {e}")
        return None

async def ai_calls_today(uid: int) -> int:
    pool = await get_pool()
    async with pool.acquire() as con:
        n = await con.fetchval("""
            SELECT COUNT(*) FROM events
            WHERE user_id=$1 AND event='ai_call' AND created_at::date=CURRENT_DATE
        """, uid)
    return int(n or 0)

async def total_spend_today() -> float:
    pool = await get_pool()
    async with pool.acquire() as con:
        val = await con.fetchval("""
            SELECT COALESCE(SUM(value),0) FROM events
            WHERE event='ai_usd' AND created_at::date=CURRENT_DATE
        """)
    return float(val or 0.0)

async def add_spend(amount: float):
    pool = await get_pool()
    async with pool.acquire() as con:
        await con.execute("INSERT INTO events(user_id,event,value) VALUES(0,'ai_usd',$1)", float(amount))

async def can_use_ai(uid: int) -> bool:
    return (await ai_calls_today(uid)) < MAX_AI_CALLS_PER_DAY

async def can_spend(amount: float) -> bool:
    return (await total_spend_today()) + float(amount) <= MAX_DAILY_SPEND

async def ai_reply(uid: int, prompt: str, temperature=0.8, max_tokens=220) -> str:
    est_cost = (max_tokens / 1000.0) * 0.0006
    if not await can_use_ai(uid) or not await can_spend(est_cost):
        await log_event(uid, "ai_block", 1)
        return (await ai_generate(prompt, temperature, max_tokens)) or "Начни с 10 минут на самом простом шаге."
    text = await ai_generate(prompt, temperature, max_tokens)
    await log_event(uid, "ai_call", 1)
    await add_spend(est_cost)
    return text or "Сделай маленький шаг 5–10 минут — главное начать."

# ---------- Chat cleanliness ----------
LAST_BOT_MSG: dict[tuple[int, int], tuple[int, str | None]] = {}
TIMER_MSG_ID: dict[int, int] = {}
ACTIVE_TIMERS: dict[int, asyncio.Task] = {}
TIMER_META: dict[int, dict] = {}

async def send_clean(chat_id: int, user_id: int, text: str,
                     reply_markup: types.InlineKeyboardMarkup | None = None,
                     parse_mode: str | None = None,
                     tag: str | None = None,
                     preserve_tags: set[str] | None = None):
    key = (chat_id, user_id)
    prev = LAST_BOT_MSG.get(key)
    if prev:
        prev_mid, prev_tag = prev
        if not (preserve_tags and prev_tag in preserve_tags):
            try:
                await bot.delete_message(chat_id, prev_mid)
            except Exception:
                pass
    m = await bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode=parse_mode)
    LAST_BOT_MSG[key] = (m.message_id, tag)
    return m

async def update_last_tag(chat_id: int, user_id: int, tag: str | None):
    key = (chat_id, user_id)
    if key in LAST_BOT_MSG:
        mid, _ = LAST_BOT_MSG[key]
        LAST_BOT_MSG[key] = (mid, tag)

async def safe_delete(chat_id: int, message_id: int):
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:
        pass

# ---------- Keyboards ----------
def menu_kb() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="🚀 Начать сейчас", callback_data="startnow:start")],
        [
            types.InlineKeyboardButton(text="⏳ Таймер", callback_data="menu:timer"),
            types.InlineKeyboardButton(text="💬 Помощник", callback_data="ask"),
        ],
        [
            types.InlineKeyboardButton(text="📊 Статистика", callback_data="menu:stats"),
            types.InlineKeyboardButton(text="💡 Обратная связь", callback_data="menu:feedback"),
        ],
        [types.InlineKeyboardButton(text="ℹ️ Польза и функции", callback_data="menu:help")],
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

def posttimer_kb(minutes: int) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [
            types.InlineKeyboardButton(text="✅ Да",  callback_data=f"posttimer:win:{minutes}"),
            types.InlineKeyboardButton(text="❌ Не удалось", callback_data=f"posttimer:fail:{minutes}"),
        ],
        [types.InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:root")],
    ])

def reasons_kb() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="📱 Уведомления/соцсети", callback_data="reason:notify")],
        [types.InlineKeyboardButton(text="🧱 Сложно/непонятно",     callback_data="reason:hard")],
        [types.InlineKeyboardButton(text="😵 Устал, нет энергии",   callback_data="reason:tired")],
        [types.InlineKeyboardButton(text="🚨 Срочные дела",         callback_data="reason:urgent")],
        [types.InlineKeyboardButton(text="🤔 Другое",               callback_data="reason:other")],
    ])

def onboarding_kb() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [
            types.InlineKeyboardButton(text="🏁 Не могу начать", callback_data="ob:start"),
            types.InlineKeyboardButton(text="📱 Отвлекаюсь", callback_data="ob:distraction"),
        ],
        [
            types.InlineKeyboardButton(text="😵 Перегруз", callback_data="ob:overload"),
            types.InlineKeyboardButton(text="☕ Нужен перерыв", callback_data="ob:break"),
        ],
        [types.InlineKeyboardButton(text="✍️ Напишу сам", callback_data="ask")],
    ])

# ---------- Texts ----------
ONBOARDING_TEXT = (
    "Привет 👋 Я AntiZalip. Помогаю перестать откладывать и вернуться к делу.\n\n"
    "Что мешает прямо сейчас?"
)
HELP_TEXT = (
    "Польза и функции:\n"
    "• 🚀 Начать сейчас — быстрый вход в действие\n"
    "• ⏳ Таймер — договорись с собой на 5/15/30 мин\n"
    "• 💬 Помощник — короткий план и один шаг\n"
    "• 📊 Статистика — твои минуты фокуса и победы\n\n"
    "Пиши своими словами в любой момент."
)
WELCOME_TEXT = "Главное меню. Выбирай действие ниже или напиши своими словами."

# ---------- States ----------
class TimerStates(StatesGroup):
    waiting_minutes = State()

class AskStates(StatesGroup):
    waiting_input = State()

class FeedbackStates(StatesGroup):
    waiting_text = State()

# ---------- "Начать сейчас" фразы ----------
UNIVERSAL_KICKS = [
    "Определи один простой шаг и сделай его 5 минут.",
    "Убери лишнее с глаз. Эти 15 минут — только одно дело.",
    "Назови вслух, что сделаешь за 30 минут — и начни.",
    "Сделай первую часть: один звонок, один абзац или одна полка — 10–15 минут.",
    "Если тяжело — уменьши задачу вдвое и начни на 5 минут.",
    "Сделай быстрый старт: настрой рабочее место и включись на 15 минут.",
    "Закрой всё лишнее. 5 минут — только один шаг к цели.",
]

CONTEXT_HINTS = {
    "start": [
        "Начнём с малого: выбери простой кусочек и удели ему 5 минут.",
        "Поставь 15 минут и сделай первый понятный шаг.",
    ],
    "distraction": [
        "Убери телефон с глаз и выключи звук на 15 минут. Одно дело — один отрезок.",
        "Закрой лишние окна и дай себе 5 минут на простой шаг.",
    ],
    "overload": [
        "Разбей задачу на куски. Возьми один — самый понятный — на 15 минут.",
        "Запиши 3 пункта плана, затем 10 минут на первый пункт.",
    ],
    "break": [
        "Пауза: вода, движение, 5 глубоких вдохов. Затем 5 минут на лёгкую часть.",
        "Сделай короткий перезапуск и включись на 10 минут.",
    ],
}

# ---------- Timers ----------
async def cancel_user_timer(uid: int):
    task = ACTIVE_TIMERS.pop(uid, None)
    if task and not task.done():
        task.cancel()
        await log_event(uid, "timer_cancel")
    mid = TIMER_MSG_ID.pop(uid, None)
    if mid:
        meta = TIMER_META.get(uid)
        if meta:
            await safe_delete(meta["chat_id"], mid)

async def timer_worker(uid: int, chat_id: int, minutes: int):
    try:
        await asyncio.sleep(minutes * 60)
        await log_event(uid, "timer_done", minutes)
        started_mid = TIMER_MSG_ID.pop(uid, None)
        if started_mid:
            await safe_delete(chat_id, started_mid)
        await bot.send_message(
            chat_id,
            f"⏰ {minutes} минут вышло!\nПолучилось сфокусироваться?",
            reply_markup=posttimer_kb(minutes)
        )
        await track(uid, "timer_done", {"min": minutes})
    except asyncio.CancelledError:
        pass

async def start_timer(chat_id: int, uid: int, minutes: int):
    await cancel_user_timer(uid)
    TIMER_META[uid] = {"chat_id": chat_id, "minutes": minutes}
    task = asyncio.create_task(timer_worker(uid, chat_id, minutes))
    ACTIVE_TIMERS[uid] = task
    await log_event(uid, "timer_start", minutes)
    await track(uid, "timer_start", {"min": minutes})
    m = await bot.send_message(
        chat_id,
        f"✅ Таймер на {minutes} мин запущен.",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="⏹ Остановить", callback_data="timer:stop")],
            [types.InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:root")],
        ])
    )
    TIMER_MSG_ID[uid] = m.message_id

# ---------- Admin utils ----------
def is_admin(uid: int) -> bool: return uid in ADMIN_IDS
def fmt_usd(x: float) -> str: return f"${x:.4f}"

# ---------- Commands ----------
@dp.message(Command("start"))
async def cmd_start(msg: types.Message):
    uid = msg.from_user.id
    await ensure_user(uid)
    try:
        await bot.delete_message(msg.chat.id, msg.message_id)
    except Exception:
        pass
    await log_event(uid, "start")
    await track(uid, "start", {})
    await send_clean(msg.chat.id, uid, ONBOARDING_TEXT, reply_markup=onboarding_kb(), tag="onboarding")
    await track(uid, "onboarding_view", {})

@dp.message(Command("menu"))
async def cmd_menu(msg: types.Message):
    await send_clean(msg.chat.id, msg.from_user.id, WELCOME_TEXT, reply_markup=menu_kb(), tag="menu")

@dp.message(Command("help"))
async def cmd_help(msg: types.Message):
    await send_clean(msg.chat.id, msg.from_user.id, HELP_TEXT, reply_markup=menu_kb(), tag="help")

@dp.message(Command("stats"))
async def cmd_stats(msg: types.Message):
    s = await fetch_stats(msg.from_user.id)
    text = (
        "📊 Твоя статистика:\n"
        f"🏆 Победы: {s['wins']} · ❌ Неудачи: {s['loses']}\n"
        f"⏱ Минут фокуса (всего): {s['reclaimed']}\n"
        f"⏳ Таймеров сегодня: {s['today_cnt']} (мин: {s['today_min']})"
    )
    await send_clean(msg.chat.id, msg.from_user.id, text, reply_markup=menu_kb(), tag="stats")

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
        f"Глобальный расход: {fmt_usd(spent)}/{fmt_usd(MAX_DAILY_SPEND)}"
    )
    await send_clean(msg.chat.id, msg.from_user.id, text, reply_markup=menu_kb(), tag="ai")

# ---------- Onboarding (фикc: Text(startswith="ob:")) ----------
@dp.callback_query(Text(startswith="ob:"))
async def cb_onboarding(call: types.CallbackQuery):
    uid = call.from_user.id
    var = call.data.split(":")[1]
    await ensure_user(uid)
    await set_personal_context(uid, var)
    await track(uid, "onboarding_click", {"variant": var})

    replies = {
        "start":       "Начнём с малого. Выбери простой кусочек и удели ему 5 минут — этого достаточно, чтобы включиться.",
        "distraction": "Убери телефон с глаз и выключи звук на 15 минут. Одно дело — один отрезок.",
        "overload":    "Разбей задачу на куски. Возьми один понятный на 15 минут — и начни.",
        "break":       "Сделай короткий перезапуск: вода, движение, дыхание. Затем 5 минут на лёгкую часть.",
    }
    text = replies.get(var, "Опиши в двух фразах, что происходит — дам короткий план.")
    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [
            types.InlineKeyboardButton(text="⏳ 5 мин", callback_data="timer:5"),
            types.InlineKeyboardButton(text="⏳ 15 мин", callback_data="timer:15"),
            types.InlineKeyboardButton(text="⏳ 30 мин", callback_data="timer:30"),
        ],
        [types.InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:root")],
    ])
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except Exception:
        await send_clean(call.message.chat.id, uid, text, reply_markup=kb, tag="onboarding_ans")
    await call.answer()

# ---------- Menu ----------
@dp.callback_query(F.data == "menu:root")
async def cb_menu_root(call: types.CallbackQuery):
    try:
        await call.message.edit_text(WELCOME_TEXT, reply_markup=menu_kb())
        await update_last_tag(call.message.chat.id, call.from_user.id, "menu")
    except Exception:
        await send_clean(call.message.chat.id, call.from_user.id, WELCOME_TEXT, reply_markup=menu_kb(), tag="menu")
    await call.answer()

@dp.callback_query(F.data == "menu:help")
async def cb_menu_help(call: types.CallbackQuery):
    try:
        await call.message.edit_text(HELP_TEXT, reply_markup=menu_kb())
        await update_last_tag(call.message.chat.id, call.from_user.id, "help")
    except Exception:
        await send_clean(call.message.chat.id, call.from_user.id, HELP_TEXT, reply_markup=menu_kb(), tag="help")
    await call.answer()

@dp.callback_query(F.data == "menu:stats")
async def cb_menu_stats(call: types.CallbackQuery):
    s = await fetch_stats(call.from_user.id)
    text = (
        "📊 Твоя статистика:\n"
        f"🏆 Победы: {s['wins']} · ❌ Неудачи: {s['loses']}\n"
        f"⏱ Минут фокуса (всего): {s['reclaimed']}\n"
        f"⏳ Таймеров сегодня: {s['today_cnt']} (мин: {s['today_min']})"
    )
    try:
        await call.message.edit_text(text, reply_markup=menu_kb())
    except Exception:
        await send_clean(call.message.chat.id, call.from_user.id, text, reply_markup=menu_kb(), tag="stats")
    await track(call.from_user.id, "stats_view", {})
    await call.answer()

@dp.callback_query(F.data == "menu:feedback")
async def cb_menu_feedback(call: types.CallbackQuery, state: FSMContext):
    await state.set_state(FeedbackStates.waiting_text)
    try:
        await call.message.edit_text("💡 Обратная связь\nРасскажи, что улучшить или чего не хватает.", reply_markup=menu_kb())
    except Exception:
        await send_clean(call.message.chat.id, call.from_user.id, "💡 Обратная связь\nРасскажи, что улучшить или чего не хватает.",
                         reply_markup=menu_kb(), tag="feedback")
    await call.answer()

@dp.message(FeedbackStates.waiting_text, F.text)
async def feedback_input(msg: types.Message, state: FSMContext):
    await state.clear()
    uid = msg.from_user.id
    await ensure_user(uid)
    await store_message(uid, "feedback", (msg.text or "")[:4000])
    await log_event(uid, "feedback", 1)
    await track(uid, "feedback", {})
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, f"🗣 Feedback от {uid}:\n{msg.text}")
        except Exception:
            pass
    await send_clean(msg.chat.id, uid, "Спасибо! Записал. Это помогает становиться лучше 🙌",
                     reply_markup=menu_kb(), tag="menu")

# ---------- "Начать сейчас" ----------
@dp.callback_query(F.data == "startnow:start")
async def cb_startnow(call: types.CallbackQuery):
    uid = call.from_user.id
    await ensure_user(uid)
    await track(uid, "start_now_click", {})
    ctx = (await get_personal_context(uid)) or ""
    pool = UNIVERSAL_KICKS.copy()
    if ctx in CONTEXT_HINTS:
        pool += CONTEXT_HINTS[ctx]
    phrase = random.choice(pool)
    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [
            types.InlineKeyboardButton(text="⏳ 5 мин", callback_data="timer:5"),
            types.InlineKeyboardButton(text="⏳ 15 мин", callback_data="timer:15"),
            types.InlineKeyboardButton(text="⏳ 30 мин", callback_data="timer:30"),
        ],
        [types.InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:root")],
    ])
    try:
        await call.message.edit_text(f"🚀 {phrase}", reply_markup=kb)
        await update_last_tag(call.message.chat.id, uid, "startnow")
    except Exception:
        await send_clean(call.message.chat.id, uid, f"🚀 {phrase}", reply_markup=kb, tag="startnow")
    await call.answer()

# ---------- Timers UI ----------
@dp.callback_query(F.data == "menu:timer")
async def cb_menu_timer(call: types.CallbackQuery):
    desc = ("Таймер — способ договориться с собой. Маленький блок времени = маленькая победа.\n"
            "Выбери длительность:")
    try:
        await call.message.edit_text(desc, reply_markup=timers_kb())
        await update_last_tag(call.message.chat.id, call.from_user.id, "timer_menu")
    except Exception:
        await send_clean(call.message.chat.id, call.from_user.id, desc, reply_markup=timers_kb(), tag="timer_menu")
    await call.answer()

@dp.callback_query(F.data.startswith("timer:"))
async def cb_timer(call: types.CallbackQuery, state: FSMContext):
    uid = call.from_user.id
    chat_id = call.message.chat.id
    data = call.data

    if data == "timer:custom":
        await state.set_state(TimerStates.waiting_minutes)
        try:
            await call.message.edit_text("Введи число минут (1–180):",
                                         reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                                             [types.InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:timer")]
                                         ]))
        except Exception:
            await send_clean(chat_id, uid, "Введи число минут (1–180):",
                             reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                                 [types.InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:timer")]
                             ]), tag="timer_custom")
        await call.answer(); return

    if data == "timer:stop":
        await cancel_user_timer(uid)
        await send_clean(chat_id, uid, "⛔️ Таймер остановлен.", reply_markup=menu_kb(), tag="menu")
        await call.answer(); return

    minutes = int(data.split(":")[1])
    await start_timer(chat_id, uid, minutes)
    await call.answer()

@dp.message(TimerStates.waiting_minutes, F.text)
async def custom_minutes_input(msg: types.Message, state: FSMContext):
    txt = (msg.text or "").strip()
    if not txt.isdigit():
        await send_clean(msg.chat.id, msg.from_user.id, "Нужно целое число 1–180. Попробуй снова или нажми «Меню».",
                         reply_markup=menu_kb(), tag="menu")
        return
    n = int(txt)
    if not (1 <= n <= 180):
        await send_clean(msg.chat.id, msg.from_user.id, "Допустимы 1–180 минут.",
                         reply_markup=menu_kb(), tag="menu")
        return
    await state.clear()
    await start_timer(msg.chat.id, msg.from_user.id, n)

# ---------- Post-timer result ----------
@dp.callback_query(F.data.startswith("posttimer:"))
async def cb_posttimer(call: types.CallbackQuery):
    uid = call.from_user.id
    _, kind, mins = call.data.split(":")
    minutes = int(mins)
    if kind == "win":
        await log_event(uid, "posttimer_win", minutes)
        s = await fetch_stats(uid)
        text = f"🏆 Отлично! +{minutes} минут записаны.\nПобеды: {s['wins']} · Неудачи: {s['loses']}"
        try:
            await call.message.edit_text(text, reply_markup=menu_kb())
        except Exception:
            await send_clean(call.message.chat.id, uid, text, reply_markup=menu_kb(), tag="menu")
        await call.answer(); return

    await log_event(uid, "posttimer_fail", minutes)
    ask = "Почему не получилось сфокусироваться?"
    try:
        await call.message.edit_text(ask, reply_markup=reasons_kb())
    except Exception:
        await send_clean(call.message.chat.id, uid, ask, reply_markup=reasons_kb(), tag="reasons")
    await call.answer()

@dp.callback_query(F.data.startswith("reason:"))
async def cb_reason(call: types.CallbackQuery):
    uid = call.from_user.id
    code = call.data.split(":")[1]
    await log_event(uid, f"reason_{code}", 1)
    await track(uid, "reason", {"code": code})

    replies = {
        "notify": ("Выключи звук или убери телефон с глаз на 15 минут. "
                   "Давай попробуем ещё раз?"),
        "hard":   ("Разбей задачу на маленький кусок и возьми самый понятный. "
                   "Начнём с 5 минут — просто включиться."),
        "tired":  ("Сделай мини-перерыв: вода, движение, глубокий вдох. "
                   "Потом лёгкий 5-минутный шаг."),
        "urgent": ("Запиши, к чему вернёшься первым делом после срочных. "
                   "А затем уделим 10 минут этому делу."),
        "other":  ("Опиши своими словами, что мешает — подскажу решение."),
    }
    timers = {"notify":15, "hard":5, "tired":5, "urgent":10}
    text = replies.get(code, "Опиши своими словами, что мешает — подскажу решение.")
    if code == "other":
        try:
            await call.message.edit_text(text, reply_markup=menu_kb())
        except Exception:
            await send_clean(call.message.chat.id, uid, text, reply_markup=menu_kb(), tag="ask_hint")
        await call.answer(); return

    minutes = timers[code]
    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text=f"⏳ {minutes} мин", callback_data=f"timer:{minutes}")],
        [types.InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:root")],
    ])
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except Exception:
        await send_clean(call.message.chat.id, uid, text, reply_markup=kb, tag="reason_suggest")
    await call.answer()

# ---------- Помощник ----------
@dp.callback_query(F.data == "ask")
async def cb_ask(call: types.CallbackQuery, state: FSMContext):
    await state.set_state(AskStates.waiting_input)
    txt = "Опиши в 1–2 предложениях, что происходит — дам короткий план и один шаг на 5–15 минут."
    try:
        await call.message.edit_text(txt, reply_markup=menu_kb())
        await update_last_tag(call.message.chat.id, call.from_user.id, "ask")
    except Exception:
        await send_clean(call.message.chat.id, call.from_user.id, txt, reply_markup=menu_kb(), tag="ask")
    await call.answer()

@dp.message(AskStates.waiting_input, F.text)
async def ask_input(msg: types.Message, state: FSMContext):
    await state.clear()
    uid = msg.from_user.id
    await ensure_user(uid)
    text = (msg.text or "").strip()
    await store_message(uid, "free_chat", text)
    await log_event(uid, "free_chat", 1)
    await track(uid, "free_chat_text", {"len": len(text)})
    try:
        await bot.send_chat_action(msg.chat.id, ChatAction.TYPING)
    except Exception:
        pass
    ctx = (await get_personal_context(uid)) or ""
    prompt = f"Контекст: {ctx}. Пользователь: «{text}». Дай короткий план (2–3 фразы) и один шаг на 5–15 минут."
    reply = await ai_reply(uid, prompt, 0.9, 300)
    await send_clean(msg.chat.id, uid, reply, reply_markup=menu_kb(), tag="coach")

@dp.message(F.text & ~F.text.startswith("/"))
async def free_chat(msg: types.Message):
    uid = msg.from_user.id
    await ensure_user(uid)
    text = (msg.text or "").strip()
    await store_message(uid, "free_chat", text)
    await log_event(uid, "free_chat", 1)
    await track(uid, "free_chat_text", {"len": len(text)})
    try:
        await bot.send_chat_action(msg.chat.id, ChatAction.TYPING)
    except Exception:
        pass
    ctx = (await get_personal_context(uid)) or ""
    prompt = f"Контекст: {ctx}. Пользователь: «{text}». Дай короткий план (2–3 фразы) и один шаг на 5–15 минут."
    reply = await ai_reply(uid, prompt, 0.8, 300)
    await send_clean(msg.chat.id, uid, reply, reply_markup=menu_kb(), tag="coach")

# ---------- Webhook server ----------
async def _health(_req): return web.Response(text="OK")

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
        log.warning("BASE_URL is not set — webhook not configured.")
        return
    url = f"{BASE_URL}/webhook/{WEBHOOK_SECRET}"
    await bot.set_webhook(url, drop_pending_updates=True, allowed_updates=["message","callback_query"])
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

# ---------- Webhook watchdog ----------
async def webhook_watchdog():
    if not BASE_URL:
        return
    want_url = f"{BASE_URL}/webhook/{WEBHOOK_SECRET}"
    while True:
        try:
            info = await bot.get_webhook_info()
            current = info.url or ""
            if current != want_url:
                await bot.set_webhook(want_url, drop_pending_updates=False, allowed_updates=["message","callback_query"])
                log.warning(f"🔁 Webhook re-set to {want_url} (was: {current or 'EMPTY'})")
        except Exception as e:
            log.warning(f"webhook_watchdog err: {e}")
        await asyncio.sleep(600)

# ---------- Nightly digest (optional) ----------
async def nightly_digest_loop():
    tz = ZoneInfo(DIGEST_TZ)
    while True:
        try:
            now = datetime.now(tz)
            if now.hour == DIGEST_HOUR:
                pool = await get_pool()
                async with pool.acquire() as con:
                    rows = await con.fetch("SELECT user_id, last_digest_date FROM users")
                today = date.today()
                for r in rows:
                    uid = int(r["user_id"]); last = r["last_digest_date"]
                    if last == today: continue
                    s = await fetch_stats(uid)
                    text = (
                        "🌙 Вечерний итог\n"
                        f"🏆 Победы/неудачи: {s['wins']}/{s['loses']}\n"
                        f"⏳ Минуты фокуса: {s['reclaimed']}\n"
                        f"⏰ Таймеров сегодня: {s['today_cnt']} ({s['today_min']} мин)\n"
                        "Завтра — ещё один маленький шаг."
                    )
                    try:
                        await bot.send_message(uid, text, reply_markup=menu_kb())
                    except Exception:
                        pass
                    async with pool.acquire() as con2:
                        await con2.execute("UPDATE users SET last_digest_date=$1 WHERE user_id=$2", today, uid)
                await asyncio.sleep(65 * 60)
            else:
                await asyncio.sleep(5 * 60)
        except Exception as e:
            log.warning(f"digest loop err: {e}")
            await asyncio.sleep(60)

# ---------- main ----------
async def main():
    log.info("🚀 AntiZalipBot запускается...")

    await init_db()
    log.info("✅ DB init complete")

    # Команды бота (убирает плашку и даёт «Меню»)
    await bot.set_my_commands([
        types.BotCommand(command="start", description="Перезапуск / онбординг"),
        types.BotCommand(command="menu",  description="Главное меню"),
        types.BotCommand(command="stats", description="Статистика"),
        types.BotCommand(command="help",  description="Польза и функции"),
    ])

    asyncio.create_task(start_web_server())
    asyncio.create_task(nightly_digest_loop())
    asyncio.create_task(webhook_watchdog())

    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
