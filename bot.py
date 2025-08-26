# bot.py
# AntiZalipBot — MVP с меню, таймерами, статистикой, стриками, аналитикой events
# + Healthcheck web-сервер для Render Free (порт сканер)
# Требования: aiogram<3.6, python-dotenv>=1.0,<2.0, aiosqlite
# Рантайм в облаке: PYTHON_VERSION=3.12.5 (или runtime.txt: python-3.12.5)

import os
import asyncio
import logging
from datetime import datetime, timedelta, timezone, date

from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext

from aiogram.client.session.aiohttp import AiohttpSession
from aiohttp import web
import aiosqlite

# ===================== ЛОГИ =====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("AntiZalipBot")

# ===================== ТОКЕН =====================
load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TOKEN:
    raise RuntimeError("Не найден TELEGRAM_TOKEN в .env")

# HTTP-сессия с увеличенным таймаутом
session = AiohttpSession(timeout=30)
bot = Bot(token=TOKEN, session=session)
dp = Dispatcher()

DB_PATH = "anti.db"

# ===================== ХРАНИЛКА (MVP) =====================
# Активные таймеры: user_id -> asyncio.Task
active_timers: dict[int, asyncio.Task] = {}
# Метаданные таймера: user_id -> (until_dt, minutes)
timer_meta: dict[int, tuple[datetime, int]] = {}

# ===================== FSM ДЛЯ "СВОЙ…" =====================
class TimerStates(StatesGroup):
    waiting_custom_minutes = State()

# ===================== КНОПКИ =====================
def main_menu_kb() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="⏳ Поставить таймер", callback_data="menu:timer")],
        [types.InlineKeyboardButton(text="🛌 Режим сна (30 мин)", callback_data="sleep:30")],
        [
            types.InlineKeyboardButton(text="📊 Статистика", callback_data="menu:stats"),
            types.InlineKeyboardButton(text="ℹ️ Справка", callback_data="menu:help"),
        ],
    ])
    return kb

def timers_kb() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [
            types.InlineKeyboardButton(text="5 мин", callback_data="timer:5"),
            types.InlineKeyboardButton(text="15 мин", callback_data="timer:15"),
            types.InlineKeyboardButton(text="30 мин", callback_data="timer:30"),
        ],
        [
            types.InlineKeyboardButton(text="Свой…", callback_data="timer:custom"),
            types.InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:root"),
        ],
    ])
    return kb

def after_timer_kb() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="🔁 Перезапустить", callback_data="again")],
        [
            types.InlineKeyboardButton(text="✍️ Сделаю дело", callback_data="do_task"),
            types.InlineKeyboardButton(text="🚶 Прогулка 5 мин", callback_data="walk"),
        ],
        [types.InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:root")],
    ])
    return kb

# ===================== ТЕКСТЫ =====================
MOTIVATION = [
    "Стоп-скролл. Один осознанный шаг возвращает тебе день.",
    "Ты не лентяй — ты залип. Встаём, дышим 10 раз, и в бой.",
    "Ещё чуть-чуть фокуса — и ты герой сегодняшнего дня.",
    "Выбор прост: скролл или жизнь. Выбирай жизнь.",
]

HELP_TEXT = (
    "Я помогаю перестать залипать и вернуть контроль над временем.\n\n"
    "Что я умею:\n"
    "• Поставить таймер 5/15/30 минут или свой\n"
    "• Напомнить и мягко «вытащить» тебя из рилсов\n"
    "• Дать быстрый толчок: перезапуск, одно дело, прогулка\n"
    "• 📊 /stats — твой прогресс и стрики\n\n"
    "Команды:\n"
    "/start — запуск и меню\n"
    "/menu — показать главное меню\n"
    "/stop — остановить активный таймер\n"
    "/help — помощь\n"
    "/stats — статистика и серия дней\n"
    "/adm_today — сводка за сегодня\n"
    "/adm_week — сводка за 7 дней\n"
)

WELCOME_TEXT = (
    "Привет! Я AntiZalipBot 👋\n"
    "Помогу вырваться из бесконечной прокрутки и вернуться к жизни.\n\n"
    "Выбери действие ниже:"
)

# ===================== БД: INIT / UTILS =====================
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                finished_at TEXT NOT NULL,
                minutes INTEGER NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(user_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                event TEXT NOT NULL,
                value INTEGER,
                created_at TEXT NOT NULL
            )
        """)
        await db.commit()

async def ensure_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        if row is None:
            await db.execute(
                "INSERT INTO users (user_id, created_at) VALUES (?, ?)",
                (user_id, datetime.now(timezone.utc).isoformat())
            )
            await db.commit()

async def record_session(user_id: int, minutes: int):
    await ensure_user(user_id)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO sessions (user_id, finished_at, minutes) VALUES (?, ?, ?)",
            (user_id, datetime.now(timezone.utc).isoformat(), minutes)
        )
        await db.commit()

async def log_event(user_id: int, event: str, value: int | None = None):
    await ensure_user(user_id)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO events (user_id, event, value, created_at) VALUES (?, ?, ?, ?)",
            (user_id, event, value, datetime.now(timezone.utc).isoformat())
        )
        await db.commit()

async def fetch_stats(user_id: int):
    await ensure_user(user_id)
    async with aiosqlite.connect(DB_PATH) as db:
        # total sessions & minutes
        cur = await db.execute(
            "SELECT COUNT(*), COALESCE(SUM(minutes),0) FROM sessions WHERE user_id = ?",
            (user_id,)
        )
        total_cnt, total_min = await cur.fetchone()

        # today
        today = date.today().isoformat()
        cur = await db.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(minutes),0)
            FROM sessions
            WHERE user_id = ?
              AND substr(finished_at,1,10) = ?
            """,
            (user_id, today)
        )
        today_cnt, today_min = await cur.fetchone()

        # streak
        cur = await db.execute(
            """
            SELECT DISTINCT substr(finished_at,1,10) AS d
            FROM sessions
            WHERE user_id = ?
            ORDER BY d DESC
            """,
            (user_id,)
        )
        rows = await cur.fetchall()
        days = [date.fromisoformat(r[0]) for r in rows]
        streak = 0
        cur_day = date.today()
        for d in days:
            if d == cur_day:
                streak += 1
                cur_day = cur_day - timedelta(days=1)
            elif d == (cur_day - timedelta(days=1)):
                streak += 1
                cur_day = cur_day - timedelta(days=1)
            else:
                break

        return {
            "total_cnt": total_cnt or 0,
            "total_min": total_min or 0,
            "today_cnt": today_cnt or 0,
            "today_min": today_min or 0,
            "streak": streak,
        }

# ===================== УТИЛИТЫ =====================
def pick_motivation(uid: int, minutes: int) -> str:
    return MOTIVATION[hash((uid, minutes)) % len(MOTIVATION)]

async def cancel_user_timer(uid: int):
    task = active_timers.pop(uid, None)
    timer_meta.pop(uid, None)
    if task and not task.done():
        task.cancel()

async def schedule_timer(chat_id: int, user_id: int, minutes: int):
    """Асинхронная задача: ждёт minutes, логирует результат, затем шлёт сообщение с кнопками."""
    try:
        await asyncio.sleep(minutes * 60)
        await record_session(user_id, minutes)
        await log_event(user_id, "timer_finish", minutes)
        timer_meta.pop(user_id, None)
        active_timers.pop(user_id, None)
        await bot.send_message(
            chat_id,
            f"⏰ {minutes} минут вышло! Что дальше?",
            reply_markup=after_timer_kb()
        )
    except asyncio.CancelledError:
        pass

async def start_timer(chat_id: int, user_id: int, minutes: int):
    await cancel_user_timer(user_id)
    until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    timer_meta[user_id] = (until, minutes)
    await log_event(user_id, "timer_start", minutes)
    task = asyncio.create_task(schedule_timer(chat_id, user_id, minutes))
    active_timers[user_id] = task

# ===================== ОБРАБОТЧИКИ: КОМАНДЫ =====================
@dp.message(Command("start"))
async def cmd_start(msg: types.Message):
    await ensure_user(msg.from_user.id)
    await log_event(msg.from_user.id, "start")
    await msg.answer(WELCOME_TEXT, reply_markup=main_menu_kb())

@dp.message(Command("menu"))
async def cmd_menu(msg: types.Message):
    await log_event(msg.from_user.id, "menu_open")
    await msg.answer("Главное меню:", reply_markup=main_menu_kb())

@dp.message(Command("help"))
async def cmd_help(msg: types.Message):
    await msg.answer(HELP_TEXT, reply_markup=main_menu_kb())

@dp.message(Command("stop"))
async def cmd_stop(msg: types.Message):
    await cancel_user_timer(msg.from_user.id)
    await log_event(msg.from_user.id, "stop")
    await msg.answer("⏹ Таймер остановлен. Контроль у тебя.", reply_markup=main_menu_kb())

@dp.message(Command("stats"))
async def cmd_stats(msg: types.Message):
    await log_event(msg.from_user.id, "stats_open")
    s = await fetch_stats(msg.from_user.id)
    text = (
        "📊 *Твоя статистика*\n\n"
        f"Всего таймеров: *{s['total_cnt']}*\n"
        f"Суммарно минут: *{s['total_min']}*\n\n"
        f"Сегодня таймеров: *{s['today_cnt']}*\n"
        f"Сегодня минут: *{s['today_min']}*\n\n"
        f"🔥 Текущий стрик (дни подряд): *{s['streak']}*"
    )
    await msg.answer(text, parse_mode="Markdown", reply_markup=main_menu_kb())

# -------- Админ-сводки --------
@dp.message(Command("adm_today"))
async def adm_today(msg: types.Message):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT COUNT(DISTINCT user_id), COUNT(*)
            FROM events
            WHERE substr(created_at,1,10)=date('now')
        """)
        users, events = await cur.fetchone()
    await msg.answer(f"📊 Сегодня: {users} уникальных пользователей, {events} событий")

@dp.message(Command("adm_week"))
async def adm_week(msg: types.Message):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT COUNT(DISTINCT user_id), COUNT(*)
            FROM events
            WHERE created_at >= datetime('now','-7 days')
        """)
        users, events = await cur.fetchone()
    await msg.answer(f"📊 За 7 дней: {users} уникальных пользователей, {events} событий")

# ===================== ОБРАБОТЧИКИ: КНОПКИ МЕНЮ =====================
@dp.callback_query(F.data == "menu:root")
async def cb_menu_root(call: types.CallbackQuery):
    await log_event(call.from_user.id, "menu_open")
    await call.message.edit_text(WELCOME_TEXT, reply_markup=main_menu_kb())
    await call.answer()

@dp.callback_query(F.data == "menu:help")
async def cb_menu_help(call: types.CallbackQuery):
    await call.message.edit_text(HELP_TEXT, reply_markup=main_menu_kb())
    await call.answer()

@dp.callback_query(F.data == "menu:timer")
async def cb_menu_timer(call: types.CallbackQuery):
    await call.message.edit_text("Выбери длительность таймера:", reply_markup=timers_kb())
    await call.answer()

@dp.callback_query(F.data == "menu:stats")
async def cb_menu_stats(call: types.CallbackQuery):
    s = await fetch_stats(call.from_user.id)
    text = (
        "📊 *Твоя статистика*\n\n"
        f"Всего таймеров: *{s['total_cnt']}*\n"
        f"Суммарно минут: *{s['total_min']}*\n\n"
        f"Сегодня таймеров: *{s['today_cnt']}*\n"
        f"Сегодня минут: *{s['today_min']}*\n\n"
        f"🔥 Текущий стрик (дни подряд): *{s['streak']}*"
    )
    await call.message.edit_text(text, parse_mode="Markdown", reply_markup=main_menu_kb())
    await call.answer()

# ===================== ОБРАБОТЧИКИ: ТАЙМЕРЫ =====================
@dp.callback_query(F.data.startswith("timer:"))
async def cb_timer(call: types.CallbackQuery, state: FSMContext):
    uid = call.from_user.id
    chat_id = call.message.chat.id
    data = call.data or ""

    if data == "timer:custom":
        await state.set_state(TimerStates.waiting_custom_minutes)
        await call.message.edit_text(
            "Введи число минут (1–180):",
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text="⬅️ Отмена", callback_data="menu:timer")]
                ]
            )
        )
        await call.answer()
        return

    minutes = int(data.split(":")[1])  # 5/15/30
    await start_timer(chat_id, uid, minutes)
    mot = pick_motivation(uid, minutes)
    await call.message.edit_text(
        f"✅ Таймер включён на {minutes} мин.\n{mot}",
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text="⏹ Остановить", callback_data="stop")],
                [types.InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:root")],
            ]
        )
    )
    await call.answer("Погнали!")

@dp.message(TimerStates.waiting_custom_minutes, F.text)
async def custom_minutes_input(msg: types.Message, state: FSMContext):
    text = (msg.text or "").strip()
    if not text.isdigit():
        await msg.reply("Нужно число от 1 до 180. Попробуй снова или нажми /menu.")
        return
    n = int(text)
    if not (1 <= n <= 180):
        await msg.reply("Допустимый диапазон: 1–180 минут. Попробуй снова.")
        return

    await state.clear()
    uid = msg.from_user.id
    chat_id = msg.chat.id
    await start_timer(chat_id, uid, n)
    mot = pick_motivation(uid, n)
    await msg.reply(
        f"✅ Таймер включён на {n} мин.\n{mot}",
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text="⏹ Остановить", callback_data="stop")],
                [types.InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:root")],
            ]
        )
    )

@dp.callback_query(F.data == "stop")
async def cb_stop(call: types.CallbackQuery):
    await cancel_user_timer(call.from_user.id)
    await log_event(call.from_user.id, "stop")
    await call.message.edit_text("⏹ Таймер остановлен. Что дальше?", reply_markup=main_menu_kb())
    await call.answer()

# ===================== ОБРАБОТЧИКИ: ПОСЛЕ ТАЙМЕРА =====================
@dp.callback_query(F.data == "again")
async def cb_again(call: types.CallbackQuery):
    uid = call.from_user.id
    chat_id = call.message.chat.id
    minutes = 15
    if uid in timer_meta and timer_meta[uid][1] > 0:
        minutes = timer_meta[uid][1]
    await start_timer(chat_id, uid, minutes)
    await call.message.edit_text(
        f"🔁 Перезапустил на {minutes} мин. Держимся.",
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text="⏹ Остановить", callback_data="stop")],
                [types.InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:root")],
            ]
        )
    )
    await call.answer()

@dp.callback_query(F.data == "do_task")
async def cb_do_task(call: types.CallbackQuery):
    await call.message.edit_text(
        "✍️ Напиши одно дело, которое сделаешь за 15–30 минут. Когда закончишь — вернись в /menu."
    )
    await call.answer()

@dp.callback_query(F.data == "walk")
async def cb_walk(call: types.CallbackQuery):
    await call.message.edit_text(
        "🚶 Выйди на 5 минут. Посмотри вдаль, разомни шею и плечи, сделай 20 глубоких вдохов.\n"
        "Вернёшься — нажми /menu."
    )
    await call.answer()

# ===================== РЕЖИМ СНА =====================
@dp.callback_query(F.data == "sleep:30")
async def cb_sleep(call: types.CallbackQuery):
    uid = call.from_user.id
    chat_id = call.message.chat.id
    minutes = 30
    await start_timer(chat_id, uid, minutes)
    await call.message.edit_text(
        "🌙 Режим сна: через 30 минут — отбой.\n"
        "До этого времени:\n"
        "• тёплый душ\n"
        "• экранный свет убавить\n"
        "• любой лёгкий текст без новостей",
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text="⏹ Остановить", callback_data="stop")],
                [types.InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:root")],
            ]
        )
    )
    await call.answer()

# ===================== HEALTHCHECK WEB (для Render Free) =====================
async def _healthcheck(request: web.Request):
    return web.Response(text="OK")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", _healthcheck)
    app.router.add_get("/health", _healthcheck)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.getenv("PORT", "10000"))
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    logger.info(f"🌐 Healthcheck server started on 0.0.0.0:{port}")

# ===================== ТОЧКА ВХОДА =====================
async def main():
    logger.info("Инициализация БД…")
    await init_db()

    # запускаем веб-сервер (обманка порта для Render Web Service)
    asyncio.create_task(start_web_server())

    logger.info("✅ AntiZalipBot запущен")
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped")
