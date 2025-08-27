# AntiZalipBot — webhook, Postgres, PostHog, onboarding, smart timers, AI limits
# Python 3.12 • aiogram 3.5
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
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Update

# ---------- OpenAI (опционально) ----------
try:
    from openai import AsyncOpenAI
except Exception:
    AsyncOpenAI = None

# ---------- ENV ----------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN отсутствует (добавь в .env или Render → Environment).")

BASE_URL       = os.getenv("BASE_URL")  # https://<service>.onrender.com
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "antizalip_secret")

OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL")
MODEL_NAME      = os.getenv("MODEL_NAME", "gpt-4o-mini")

DATABASE_URL = os.getenv("DATABASE_URL")  # Supabase/Neon URI

POSTHOG_API_KEY = os.getenv("POSTHOG_API_KEY")
POSTHOG_HOST    = os.getenv("POSTHOG_HOST", "https://app.posthog.com")

DIGEST_TZ   = os.getenv("DIGEST_TZ", "Europe/Moscow")
DIGEST_HOUR = int(os.getenv("DIGEST_HOUR", "22"))

MAX_AI_CALLS_PER_DAY = int(os.getenv("MAX_AI_CALLS_PER_DAY", "30"))
MAX_DAILY_SPEND      = float(os.getenv("MAX_DAILY_SPEND", "1.0"))  # $/day

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

# ---------- Postgres ----------
DB_POOL: asyncpg.Pool | None = None
async def get_pool() -> asyncpg.Pool:
    global DB_POOL
    if DB_POOL is None:
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL не задан (Supabase/Neon URI).")
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
          seen_onboarding BOOLEAN DEFAULT FALSE,
          last_digest_date DATE
        )""")
        await con.execute("""
        CREATE TABLE IF NOT EXISTS events(
          id BIGSERIAL PRIMARY KEY,
          user_id BIGINT NOT NULL,
          event TEXT NOT NULL,
          value NUMERIC,
          created_at TIMESTAMPTZ DEFAULT now()
        )""")
        await con.execute("CREATE INDEX IF NOT EXISTS idx_events_user_created ON events(user_id, created_at DESC)")
        await con.execute("CREATE INDEX IF NOT EXISTS idx_events_event_created ON events(event, created_at DESC)")

async def ensure_user(uid: int):
    pool = await get_pool()
    async with pool.acquire() as con:
        await con.execute("INSERT INTO users(user_id) VALUES($1) ON CONFLICT (user_id) DO NOTHING", uid)

async def log_event(uid: int, event: str, value: float | None = None):
    pool = await get_pool()
    async with pool.acquire() as con:
        await con.execute("INSERT INTO events(user_id,event,value) VALUES($1,$2,$3)", uid, event, value)

async def get_seen_onboarding(uid: int) -> bool:
    pool = await get_pool()
    async with pool.acquire() as con:
        row = await con.fetchval("SELECT seen_onboarding FROM users WHERE user_id=$1", uid)
        return bool(row)

async def set_seen_onboarding(uid: int):
    pool = await get_pool()
    async with pool.acquire() as con:
        await con.execute("UPDATE users SET seen_onboarding=TRUE WHERE user_id=$1", uid)

async def get_seen_nudge(uid: int) -> bool:
    pool = await get_pool()
    async with pool.acquire() as con:
        return bool(await con.fetchval("SELECT seen_nudge FROM users WHERE user_id=$1", uid))

async def set_seen_nudge(uid: int):
    pool = await get_pool()
    async with pool.acquire() as con:
        await con.execute("UPDATE users SET seen_nudge=TRUE WHERE user_id=$1", uid)

async def fetch_stats(uid: int) -> dict:
    pool = await get_pool()
    async with pool.acquire() as con:
        wins   = await con.fetchval("SELECT COUNT(*) FROM events WHERE user_id=$1 AND event='battle_win'", uid) or 0
        loses  = await con.fetchval("SELECT COUNT(*) FROM events WHERE user_id=$1 AND event='battle_lose'", uid) or 0
        recl   = await con.fetchval(
            "SELECT COALESCE(SUM(value),0) FROM events WHERE user_id=$1 AND event='battle_win'", uid
        ) or 0
        row    = await con.fetchrow("""
            SELECT COUNT(*) AS cnt, COALESCE(SUM(value),0) AS minutes
            FROM events
            WHERE user_id=$1 AND event='timer_done' AND created_at::date=CURRENT_DATE
        """, uid)
    return {
        "wins": int(wins), "loses": int(loses), "reclaimed": int(recl),
        "today_cnt": int(row["cnt"]), "today_min": int(row["minutes"])
    }

async def free_chat_count_today(uid: int) -> int:
    pool = await get_pool()
    async with pool.acquire() as con:
        n = await con.fetchval("""
            SELECT COUNT(*) FROM events
            WHERE user_id=$1 AND event='free_chat' AND created_at::date=CURRENT_DATE
        """, uid)
    return int(n or 0)

# ---------- AI limits ----------
async def ai_calls_today(uid: int) -> int:
    pool = await get_pool()
    async with pool.acquire() as con:
        n = await con.fetchval("""
            SELECT COUNT(*) FROM events WHERE user_id=$1 AND event='ai_call' AND created_at::date=CURRENT_DATE
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

# ---------- PostHog ----------
async def track(uid: int, event: str, props: dict | None = None):
    if not POSTHOG_API_KEY:
        return
    payload = {
        "api_key": POSTHOG_API_KEY,
        "event": event,
        "distinct_id": str(uid),
        "properties": props or {},
    }
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(f"{POSTHOG_HOST}/capture/", json=payload)
    except Exception:
        pass

# ---------- AI generation ----------
SYSTEM_COACH = (
    "Ты помощник по фокусу: коротко, по делу, 2–3 фразы. "
    "Дай один конкретный шаг ≤2 минут, без психотерапии, без слов 'депрессия/выгорание/туплю'."
)

async def ai_generate(prompt: str, temperature: float = 0.8, max_tokens: int = 200) -> str | None:
    if not oa_client:
        return random.choice([
            "Сделай 5 глубоких вдохов, запиши один следующий шаг и начни его сейчас на 2 минуты.",
            "Положи телефон экраном вниз. Открой нужный файл/таску и дай себе 120 секунд простого действия.",
            "Закрой лишние вкладки и сделай один маленький шаг. Главное — начать на 2 минуты.",
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

async def can_use_ai(uid: int) -> bool:
    return (await ai_calls_today(uid)) < MAX_AI_CALLS_PER_DAY

async def can_spend(amount: float) -> bool:
    return (await total_spend_today()) + float(amount) <= MAX_DAILY_SPEND

async def ai_reply(uid: int, prompt: str, temperature=0.8, max_tokens=200) -> str:
    if not await can_use_ai(uid):
        await log_event(uid, "ai_block", 1)
        return "Сегодня лимит подсказок исчерпан. Без ИИ: 5 вдохов и 2 минуты на самый простой шаг."
    est_cost = (max_tokens / 1000.0) * 0.0006  # грубая оценка
    if not await can_spend(est_cost):
        await log_event(uid, "ai_block", 1)
        return "На сегодня достигли глобального лимита 💸. Завтра ИИ вернётся. Сейчас — мини-шаг на 2 минуты."
    text = await ai_generate(prompt, temperature=temperature, max_tokens=max_tokens)
    await log_event(uid, "ai_call", 1)
    await add_spend(est_cost)
    return text or "Сделай короткую паузу, расправь плечи и начни с шага на 2 минуты."

# ---------- Chat cleanliness ----------
LAST_BOT_MSG: dict[tuple[int, int], tuple[int, str | None]] = {}  # (chat_id,user_id) -> (msg_id, tag)

async def send_clean(chat_id: int, user_id: int, text: str,
                     reply_markup: types.InlineKeyboardMarkup | None = None,
                     parse_mode: str | None = None,
                     tag: str | None = None,
                     preserve_tags: set[str] | None = None):
    key = (chat_id, user_id)
    mid_tag = LAST_BOT_MSG.get(key)
    if mid_tag:
        mid, last_tag = mid_tag
        if not (preserve_tags and last_tag in preserve_tags):
            try:
                await bot.delete_message(chat_id, mid)
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

async def delete_after(chat_id: int, message_id: int, seconds: int = 20):
    try:
        await asyncio.sleep(seconds)
        await bot.delete_message(chat_id, message_id)
    except Exception:
        pass

# ---------- Keyboards ----------
def main_menu_kb() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="🎯 Вернуть фокус", callback_data="focus:start")],
        [
            types.InlineKeyboardButton(text="⏳ Таймеры", callback_data="menu:timer"),
            types.InlineKeyboardButton(text="💬 Помощник", callback_data="ask"),
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
        [types.InlineKeyboardButton(text="🍅 Помидор 25/5", callback_data="timer:pomodoro")],
        [types.InlineKeyboardButton(text="Свой…", callback_data="timer:custom")],
        [types.InlineKeyboardButton(text="⬅️ Главное меню", callback_data="menu:root")],
    ])

def onboarding_kb() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [
            types.InlineKeyboardButton(text="🏁 Не могу начать", callback_data="ob:start"),
            types.InlineKeyboardButton(text="📱 Отвлекаюсь", callback_data="ob:distraction"),
        ],
        [
            types.InlineKeyboardButton(text="😵 Перегруз", callback_data="ob:overload"),
            types.InlineKeyboardButton(text="☕ Перерыв", callback_data="ob:break"),
        ],
        [types.InlineKeyboardButton(text="✍️ Напишу сам", callback_data="ask")],
    ])

# ---------- Texts ----------
ONBOARDING_TEXT = (
    "Привет 👋 Я AntiZalip — помощник по фокусу.\n\n"
    "Что мешает прямо сейчас?"
)
HELP_TEXT = (
    "Как я помогаю:\n"
    "• 🎯 Вернуть фокус — короткий челлендж на 30–60 сек\n"
    "• 💬 Помощник — опиши ситуацию, получи план на 2–3 фразы\n"
    "• ⏳ Таймеры 5/15/30 и 🍅 25/5\n"
    "• 📊 Статистика — минуты фокуса и победы\n\n"
    "Пиши своими словами в любой момент."
)
WELCOME_TEXT = "Главное меню. Выбери действие ниже или напиши своими словами."

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
        await log_event(uid, "timer_cancel")

async def schedule_timer(chat_id: int, uid: int, minutes: int):
    try:
        await asyncio.sleep(minutes * 60)
        await log_event(uid, "timer_done", minutes)
        active_timers.pop(uid, None); timer_meta.pop(uid, None)
        await bot.send_message(
            chat_id,
            f"⏰ {minutes} минут вышло! Что дальше?",
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="🔁 Ещё столько же", callback_data="timer:again")],
                [types.InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:root")],
            ])
        )
        await track(uid, "timer_done", {"min": minutes})
    except asyncio.CancelledError:
        pass

async def start_timer(chat_id: int, uid: int, minutes: int):
    await cancel_user_timer(uid)
    until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    timer_meta[uid] = (until, minutes)
    task = asyncio.create_task(schedule_timer(chat_id, uid, minutes))
    active_timers[uid] = task
    await log_event(uid, "timer_start", minutes)
    await track(uid, "timer_start", {"min": minutes})

async def schedule_pomodoro(chat_id: int, uid: int):
    # 25 работы
    await start_timer(chat_id, uid, 25)
    try:
        m = await bot.send_message(
            chat_id,
            "🍅 Помидор: 25 минут фокуса запущены.",
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="⏹ Остановить", callback_data="timer:stop")],
                [types.InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:root")],
            ])
        )
        LAST_BOT_MSG[(chat_id, uid)] = (m.message_id, "timer")
    except Exception:
        pass
    # ждём завершения 25-минутки
    try:
        await active_timers[uid]
    except Exception:
        return
    # 5 отдыха
    await start_timer(chat_id, uid, 5)
    await bot.send_message(
        chat_id,
        "⏳ Отдых 5 минут. Затем продолжим?",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="🔁 Ещё помидор 25/5", callback_data="timer:pomodoro")],
            [types.InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:root")],
        ])
    )

# ---------- One-time nudge ----------
async def maybe_show_free_chat_nudge(uid: int, chat_id: int):
    if await get_seen_nudge(uid):
        return
    m = await bot.send_message(
        chat_id,
        "💬 Можно писать своими словами. Примеры:\n"
        "• «Не могу начать»\n"
        "• «Отвлекаюсь и теряю фокус»\n"
        "• «Устал — как перезапуститься?»",
    )
    asyncio.create_task(delete_after(chat_id, m.message_id, 18))
    await set_seen_nudge(uid)

# ---------- Admin utils ----------
def is_admin(uid: int) -> bool: return uid in ADMIN_IDS
def fmt_usd(x: float) -> str: return f"${x:.4f}"

# ---------- Commands ----------
@dp.message(Command("start"))
async def cmd_start(msg: types.Message):
    uid = msg.from_user.id
    await ensure_user(uid)
    await log_event(uid, "start")
    await track(uid, "start", {})
    if not await get_seen_onboarding(uid):
        await send_clean(msg.chat.id, uid, ONBOARDING_TEXT, reply_markup=onboarding_kb(), tag="onboarding")
        await set_seen_onboarding(uid)
        return
    await send_clean(msg.chat.id, uid, WELCOME_TEXT, reply_markup=main_menu_kb(), tag="menu", preserve_tags={"timer"})
    await maybe_show_free_chat_nudge(uid, msg.chat.id)

@dp.message(Command("menu"))
async def cmd_menu(msg: types.Message):
    await send_clean(msg.chat.id, msg.from_user.id, WELCOME_TEXT, reply_markup=main_menu_kb(),
                     tag="menu", preserve_tags={"timer"})

@dp.message(Command("help"))
async def cmd_help(msg: types.Message):
    await send_clean(msg.chat.id, msg.from_user.id, HELP_TEXT, reply_markup=main_menu_kb(),
                     tag="help", preserve_tags={"timer"})

@dp.message(Command("stats"))
async def cmd_stats(msg: types.Message):
    s = await fetch_stats(msg.from_user.id)
    chats = await free_chat_count_today(msg.from_user.id)
    text = (
        "📊 Статистика:\n"
        f"🏆 Победы с фокусом: {s['wins']}\n"
        f"🙈 Сдачи: {s['loses']}\n"
        f"⏳ Минуты фокуса: {s['reclaimed']}\n"
        f"⏰ Таймеров сегодня: {s['today_cnt']} (мин: {s['today_min']})\n"
        f"💬 Сообщений помощнику сегодня: {chats}"
    )
    await send_clean(msg.chat.id, msg.from_user.id, text, reply_markup=main_menu_kb(),
                     tag="stats", preserve_tags={"timer"})

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
    await send_clean(msg.chat.id, msg.from_user.id, text, reply_markup=main_menu_kb(),
                     tag="stats", preserve_tags={"timer"})

@dp.message(Command("ping"))
async def cmd_ping(msg: types.Message):
    await msg.answer(f"pong {datetime.utcnow().isoformat(timespec='seconds')}Z")

@dp.message(Command("adm_webhook"))
async def adm_webhook(msg: types.Message):
    if not is_admin(msg.from_user.id): return
    info = await bot.get_webhook_info()
    txt = (f"URL: {info.url or '-'}\n"
           f"Pending: {info.pending_update_count}\n"
           f"MaxConn: {getattr(info, 'max_connections', 'n/a')}")
    await msg.answer(txt)

@dp.message(Command("adm_report"))
async def adm_report(msg: types.Message):
    if not is_admin(msg.from_user.id): return
    pool = await get_pool()
    async with pool.acquire() as con:
        dau = await con.fetchval("SELECT COUNT(DISTINCT user_id) FROM events WHERE created_at::date=CURRENT_DATE") or 0
        new = await con.fetchval("SELECT COUNT(*) FROM users WHERE created_at::date=CURRENT_DATE") or 0
        timers = await con.fetchrow("""
            SELECT
              COUNT(*) FILTER (WHERE event='timer_start') AS starts,
              COUNT(*) FILTER (WHERE event='timer_done')  AS dones,
              COALESCE(SUM(value) FILTER (WHERE event='timer_done'),0) AS min_done,
              COUNT(*) FILTER (WHERE event='timer_cancel') AS cancels
            FROM events WHERE created_at::date=CURRENT_DATE
        """)
        battles = await con.fetchrow("""
            SELECT
              COUNT(*) FILTER (WHERE event='battle_win')  AS wins,
              COUNT(*) FILTER (WHERE event='battle_lose') AS loses,
              COALESCE(SUM(value) FILTER (WHERE event='battle_win'),0) AS reclaimed
            FROM events WHERE created_at::date=CURRENT_DATE
        """)
        ai = await con.fetchrow("""
            SELECT
              COUNT(*) FILTER (WHERE event='ai_call') AS calls,
              COALESCE(SUM(value) FILTER (WHERE event='ai_usd'),0) AS usd
            FROM events WHERE created_at::date=CURRENT_DATE
        """)
        free_chat = await con.fetchval("""
            SELECT COUNT(*) FROM events WHERE event='free_chat' AND created_at::date=CURRENT_DATE
        """) or 0
    timer_cr = (int(timers["dones"] or 0) / int(timers["starts"] or 1)) * 100
    dau_avg_focus = (float(timers["min_done"] or 0) / dau) if dau else 0.0
    text = (
        "📈 *AntiZalip — отчёт за сегодня*\n"
        f"👥 DAU: *{dau}* · 🆕 New: *{new}*\n"
        f"⏱ Таймеры: старт *{int(timers['starts'] or 0)}*, финиш *{int(timers['dones'] or 0)}* "
        f"(CR *{timer_cr:.0f}%*), отмен *{int(timers['cancels'] or 0)}*, мин *{int(timers['min_done'] or 0)}*\n"
        f"🏆 Победы/сдачи: *{int(battles['wins'] or 0)}* / *{int(battles['loses'] or 0)}* · "
        f"вернул минут: *{int(battles['reclaimed'] or 0)}*\n"
        f"🤖 AI: вызовов *{int(ai['calls'] or 0)}* · расход *{fmt_usd(float(ai['usd'] or 0.0))}*\n"
        f"💬 Чатов: *{free_chat}* · ⏳ мин/DAU: *{dau_avg_focus:.1f}*"
    )
    await msg.answer(text, parse_mode="Markdown")

# ---------- Onboarding callbacks ----------
async def onboarding_answer(uid: int, chat_id: int, text: str):
    # короткий ответ + кнопка «Главное меню»
    try:
        await send_clean(
            chat_id, uid, text,
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:root")]
            ]),
            tag="onboarding_ans", preserve_tags={"timer"}
        )
    except Exception:
        pass

@dp.callback_query(F.data.startswith("ob:"))
async def cb_onboarding(call: types.CallbackQuery):
    uid = call.from_user.id
    variant = call.data.split(":")[1]
    await track(uid, "onboarding_click", {"variant": variant})
    answers = {
        "start": "Запиши одно маленькое действие и сделай его 2 минуты. Начнёшь — поедет.",
        "distraction": "Положи телефон экраном вниз и закрой лишнюю вкладку. 2 минуты только одно дело.",
        "overload": "Возьми листок и выпиши 3 пункта. Выбери один — самое простое действие на 2 минуты.",
        "break": "Сделай 5 вдохов, встань и походи 30 секунд. Затем 2 минуты — один маленький шаг.",
    }
    text = answers.get(variant, "Опиши в двух фразах, что происходит — дам точный план.")
    await onboarding_answer(uid, call.message.chat.id, text)
    await call.answer()

# ---------- Menu callbacks ----------
@dp.callback_query(F.data == "menu:root")
async def cb_menu_root(call: types.CallbackQuery):
    try:
        await call.message.edit_text(WELCOME_TEXT, reply_markup=main_menu_kb())
        await update_last_tag(call.message.chat.id, call.from_user.id, "menu")
    except Exception:
        await send_clean(call.message.chat.id, call.from_user.id, WELCOME_TEXT,
                         reply_markup=main_menu_kb(), tag="menu", preserve_tags={"timer"})
    finally:
        await call.answer()

@dp.callback_query(F.data == "menu:help")
async def cb_menu_help(call: types.CallbackQuery):
    try:
        await call.message.edit_text(HELP_TEXT, reply_markup=main_menu_kb())
        await update_last_tag(call.message.chat.id, call.from_user.id, "help")
    except Exception:
        await send_clean(call.message.chat.id, call.from_user.id, HELP_TEXT,
                         reply_markup=main_menu_kb(), tag="help", preserve_tags={"timer"})
    finally:
        await call.answer()

@dp.callback_query(F.data == "menu:stats")
async def cb_menu_stats(call: types.CallbackQuery):
    s = await fetch_stats(call.from_user.id)
    chats = await free_chat_count_today(call.from_user.id)
    text = (
        "📊 Статистика:\n"
        f"🏆 Победы с фокусом: {s['wins']}\n"
        f"🙈 Сдачи: {s['loses']}\n"
        f"⏳ Минуты фокуса: {s['reclaimed']}\n"
        f"⏰ Таймеров сегодня: {s['today_cnt']} (мин: {s['today_min']})\n"
        f"💬 Сообщений помощнику сегодня: {chats}"
    )
    try:
        await call.message.edit_text(text, reply_markup=main_menu_kb())
        await update_last_tag(call.message.chat.id, call.from_user.id, "stats")
    except Exception:
        await send_clean(call.message.chat.id, call.from_user.id, text,
                         reply_markup=main_menu_kb(), tag="stats", preserve_tags={"timer"})
    finally:
        await call.answer()

# ---------- Focus (челлендж) ----------
@dp.callback_query(F.data == "focus:start")
async def cb_focus(call: types.CallbackQuery):
    uid = call.from_user.id
    await ensure_user(uid); await track(uid, "focus_click", {})
    challenge = random.choice([
        "Сделай 5 вдохов и встань на 10 секунд.",
        "Закрой лишнее окно/вкладку прямо сейчас.",
        "Положи телефон экраном вниз.",
        "Сделай 10 шагов и вернись к столу.",
        "Запиши один микро-шаг на бумагу.",
    ])
    text = f"🎯 Вернём фокус!\n\nЧеллендж: {challenge}\nСправишься?"
    try:
        await call.message.edit_text(
            text,
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="✅ Сделал", callback_data="focus:win")],
                [types.InlineKeyboardButton(text="❌ Пока пропущу", callback_data="focus:lose")],
            ])
        )
        await update_last_tag(call.message.chat.id, uid, "focus")
    except Exception:
        await send_clean(call.message.chat.id, uid, text,
                         reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                             [types.InlineKeyboardButton(text="✅ Сделал", callback_data="focus:win")],
                             [types.InlineKeyboardButton(text="❌ Пока пропущу", callback_data="focus:lose")],
                         ]), tag="focus", preserve_tags={"timer"})
    await call.answer()

@dp.callback_query(F.data == "focus:win")
async def cb_focus_win(call: types.CallbackQuery):
    uid = call.from_user.id
    await log_event(uid, "battle_win", 5)
    s = await fetch_stats(uid)
    await call.message.edit_text(
        f"🏆 Отлично! +5 минут в копилку.\nПобед: {s['wins']} | Сдач: {s['loses']}",
        reply_markup=main_menu_kb()
    )
    await update_last_tag(call.message.chat.id, uid, "menu")
    await call.answer()

@dp.callback_query(F.data == "focus:lose")
async def cb_focus_lose(call: types.CallbackQuery):
    uid = call.from_user.id
    await log_event(uid, "battle_lose", 0)
    s = await fetch_stats(uid)
    await call.message.edit_text(
        f"Ок, позже. Побед: {s['wins']} | Сдач: {s['loses']}\nГотов — жми «🎯 Вернуть фокус».",
        reply_markup=main_menu_kb()
    )
    await update_last_tag(call.message.chat.id, uid, "menu")
    await call.answer()

# ---------- Timers callbacks ----------
@dp.callback_query(F.data == "menu:timer")
async def cb_menu_timer(call: types.CallbackQuery):
    try:
        await call.message.edit_text("Выбери длительность таймера:", reply_markup=timers_kb())
        await update_last_tag(call.message.chat.id, call.from_user.id, "menu")
    except Exception:
        await send_clean(call.message.chat.id, call.from_user.id, "Выбери длительность таймера:",
                         reply_markup=timers_kb(), tag="menu", preserve_tags={"timer"})
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
            await update_last_tag(chat_id, uid, "menu")
        except Exception:
            await send_clean(chat_id, uid, "Введи число минут (1–180):",
                             reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                                 [types.InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:timer")]
                             ]), tag="menu", preserve_tags={"timer"})
        await call.answer(); return

    if data == "timer:again":
        minutes = timer_meta.get(uid, (None, 15))[1]
    elif data == "timer:pomodoro":
        asyncio.create_task(schedule_pomodoro(chat_id, uid))
        await call.answer("Помидор 25/5 запущен"); return
    else:
        minutes = int(data.split(":")[1])

    await start_timer(chat_id, uid, minutes)
    try:
        m = await bot.send_message(
            chat_id, f"✅ Таймер включён на {minutes} мин.",
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="⏹ Остановить", callback_data="timer:stop")],
                [types.InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:root")],
            ])
        )
        LAST_BOT_MSG[(chat_id, uid)] = (m.message_id, "timer")
    except Exception:
        await send_clean(chat_id, uid, f"✅ Таймер включён на {minutes} мин.",
                         reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                             [types.InlineKeyboardButton(text="⏹ Остановить", callback_data="timer:stop")],
                             [types.InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:root")],
                         ]), tag="timer")
    await call.answer()

@dp.callback_query(F.data == "timer:stop")
async def cb_timer_stop(call: types.CallbackQuery):
    await cancel_user_timer(call.from_user.id)
    try:
        await call.message.edit_text("⛔️ Таймер остановлен.", reply_markup=main_menu_kb())
        await update_last_tag(call.message.chat.id, call.from_user.id, "menu")
    except Exception:
        await send_clean(call.message.chat.id, call.from_user.id, "⛔️ Таймер остановлен.",
                         reply_markup=main_menu_kb(), tag="menu", preserve_tags={"timer"})
    await call.answer()

@dp.message(TimerStates.waiting_minutes, F.text)
async def custom_minutes_input(msg: types.Message, state: FSMContext):
    text = (msg.text or "").strip()
    if not text.isdigit():
        await send_clean(msg.chat.id, msg.from_user.id, "Нужно целое число 1–180. Попробуй снова или /menu.",
                         reply_markup=main_menu_kb(), tag="menu", preserve_tags={"timer"})
        return
    n = int(text)
    if not (1 <= n <= 180):
        await send_clean(msg.chat.id, msg.from_user.id, "Допустимы 1–180 минут.",
                         reply_markup=main_menu_kb(), tag="menu", preserve_tags={"timer"})
        return
    await state.clear()
    await start_timer(msg.chat.id, msg.from_user.id, n)
    m = await bot.send_message(
        msg.chat.id, f"✅ Таймер включён на {n} мин.",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="⏹ Остановить", callback_data="timer:stop")],
            [types.InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:root")],
        ])
    )
    LAST_BOT_MSG[(msg.chat.id, msg.from_user.id)] = (m.message_id, "timer")

# ---------- Ask (Помощник) ----------
@dp.callback_query(F.data == "ask")
async def cb_ask(call: types.CallbackQuery, state: FSMContext):
    await state.set_state(AskStates.waiting_input)
    text = "Опиши в 1–2 предложениях: что происходит и чем помочь."
    try:
        await call.message.edit_text(text, reply_markup=main_menu_kb())
        await update_last_tag(call.message.chat.id, call.from_user.id, "menu")
        m = await bot.send_message(call.message.chat.id, "Жду твоё сообщение…",
                                   input_field_placeholder="Опиши ситуацию…")
        asyncio.create_task(delete_after(call.message.chat.id, m.message_id, 15))
    except Exception:
        await send_clean(call.message.chat.id, call.from_user.id, text,
                         reply_markup=main_menu_kb(), tag="menu", preserve_tags={"timer"})
    await call.answer()

@dp.message(AskStates.waiting_input, F.text)
async def ask_input(msg: types.Message, state: FSMContext):
    await state.clear()
    uid = msg.from_user.id
    await ensure_user(uid); await log_event(uid, "free_chat"); await track(uid, "free_chat", {})
    try: await bot.send_chat_action(msg.chat.id, ChatAction.TYPING)
    except: pass
    prompt = (
        f"Пользователь просит совет: «{msg.text}». "
        "Ответь как помощник по фокусу: 2–3 фразы, один конкретный шаг ≤2 минут."
    )
    reply = await ai_reply(uid, prompt, 0.9, 400)
    await send_clean(msg.chat.id, uid, reply, reply_markup=main_menu_kb(),
                     tag="coach", preserve_tags={"timer"})

@dp.message(F.text & ~F.text.startswith("/"))
async def free_chat(msg: types.Message):
    uid = msg.from_user.id
    await ensure_user(uid); await log_event(uid, "free_chat"); await track(uid, "free_chat", {})
    try: await bot.send_chat_action(msg.chat.id, ChatAction.TYPING)
    except: pass
    prompt = (
        f"Пользователь: «{msg.text}». "
        "Дай короткую подсказку (2–3 фразы) и один шаг ≤2 минут."
    )
    reply = await ai_reply(uid, prompt, 0.8, 400)
    await send_clean(msg.chat.id, uid, reply, reply_markup=main_menu_kb(),
                     tag="coach", preserve_tags={"timer"})

# ---------- Nightly digest ----------
async def _send_digest_to_user(uid: int):
    s = await fetch_stats(uid)
    chats = await free_chat_count_today(uid)
    prompt = (
        f"Сводка дня: победы={s['wins']}, сдачи={s['loses']}, фокус={s['reclaimed']} мин, "
        f"таймеров сегодня={s['today_cnt']} ({s['today_min']} мин), чатов={chats}. "
        "Сделай 2–3 фразы: тёплая похвала + один конкретный совет на завтра."
    )
    ai = await ai_reply(uid, prompt, 0.8, 280)
    text = (
        "🌙 Вечерний итог\n"
        f"🏆 Победы/сдачи: {s['wins']}/{s['loses']}\n"
        f"⏳ Минуты фокуса: {s['reclaimed']}\n"
        f"⏰ Таймеров: {s['today_cnt']} ({s['today_min']} мин)\n"
        f"💬 Сообщений помощнику: {chats}\n\n"
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
            if now.hour == DIGEST_HOUR:
                pool = await get_pool()
                async with pool.acquire() as con:
                    rows = await con.fetch("SELECT user_id, last_digest_date FROM users")
                today = date.today()
                for r in rows:
                    uid = int(r["user_id"]); last = r["last_digest_date"]
                    if last == today: continue
                    await _send_digest_to_user(uid)
                    pool = await get_pool()
                    async with pool.acquire() as con:
                        await con.execute("UPDATE users SET last_digest_date=$1 WHERE user_id=$2", today, uid)
                await asyncio.sleep(65 * 60)
            else:
                await asyncio.sleep(5 * 60)
        except Exception as e:
            log.warning(f"digest loop err: {e}")
            await asyncio.sleep(60)

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
        log.warning("BASE_URL не задан — webhook не настраивается.")
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

# ---------- main (keep-alive) ----------
async def main():
    await init_db()
    asyncio.create_task(start_web_server())
    asyncio.create_task(nightly_digest_loop())
    # keep alive
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass

if __name__ == "__main__":
    asyncio.run(main())
