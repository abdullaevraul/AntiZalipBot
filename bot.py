# bot.py — aiogram 3.7+, long polling, автоснос webhook при конфликте

import os
import asyncio
import logging
from typing import Literal

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramConflictError  # <- важно

TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN в переменных окружения")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
logger = logging.getLogger("ai-helper-bot")

# ==== Клавиатура ====
BTN_CANT_START = "Не могу начать"
BTN_DISTRACTED = "Отвлекаюсь"
BTN_OVERLOAD   = "Перегруз"
BTN_BREAK      = "Нужен перерыв"

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_CANT_START), KeyboardButton(text=BTN_DISTRACTED)],
        [KeyboardButton(text=BTN_OVERLOAD),   KeyboardButton(text=BTN_BREAK)]
    ],
    resize_keyboard=True,
    input_field_placeholder="Нажми кнопку или опиши ситуацию текстом…"
)

# ==== FSM ====
TopicType = Literal["cant_start", "distracted", "overload", "need_break"]
class Flow(StatesGroup):
    waiting_topic_details = State()

TOPIC_MAP = {
    BTN_CANT_START: "cant_start",
    BTN_DISTRACTED: "distracted",
    BTN_OVERLOAD:   "overload",
    BTN_BREAK:      "need_break",
}

def topic_intro(topic: TopicType) -> str:
    if topic == "cant_start":
        return ("Окей, тема: «Не могу начать».\n"
                "1) Сократим шаг до 10–15 минут.\n"
                "2) Что мешает начать прямо сейчас?\n"
                "3) Таймер 10 минут.\n\n"
                "Опиши, что пытаешься начать и что стопорит.")
    if topic == "distracted":
        return ("Тема: «Отвлекаюсь».\n"
                "1) Вылечи уведомления/вкладки.\n"
                "2) Одно рабочее окно.\n"
                "3) 15–20 минут фокус + короткий перерыв.\n\n"
                "Что именно отвлекает?")
    if topic == "overload":
        return ("Тема: «Перегруз».\n"
                "1) Выгрузи задачи (можно сюда).\n"
                "2) Отметь срочность/важность.\n"
                "3) 1 шаг на 25 минут.\n\n"
                "Что давит сильнее всего?")
    return ("Тема: «Нужен перерыв».\n"
            "1) 5–10 минут — вода/движение/дыхание.\n"
            "2) Усталость 1–10.\n"
            "3) Один шаг после паузы.\n\n"
            "Как самочувствие и сколько времени на отдых?")

def topic_response(topic: TopicType, user_text: str) -> str:
    clip = user_text.strip()[:200]
    if topic == "cant_start":
        return ("Действуем:\n"
                "• Первый шаг на 10 минут, без перфекционизма.\n"
                f"• Учёл контекст: «{clip}».\n"
                "• После — короткий отчёт. Готов?")
    if topic == "distracted":
        return ("Фиксируем отвлечения:\n"
                "• Закрой лишнее, включи «Не беспокоить».\n"
                "• Одно рабочее окно на 15 минут.\n"
                f"• Триггеры: «{clip}» — учёл.\n"
                "Отпишись через 15 минут.")
    if topic == "overload":
        return ("Снимаем перегруз:\n"
                "• Выпиши задачи.\n"
                "• Выбери одну «срочно/важно» на 25 минут.\n"
                f"• Ключевые пункты: «{clip}».\n"
                "Стартуем?")
    return ("Перерыв без чувства вины:\n"
            "• 7 минут оффлайн: вода/движение/дыхание.\n"
            "• Вернёшься — один минимальный шаг.\n"
            f"• Запомнил контекст: «{clip}».\n"
            "Поставь таймер и вернись.")

async def select_topic(message: Message, state: FSMContext, topic: TopicType):
    await state.set_state(Flow.waiting_topic_details)
    await state.update_data(topic=topic)
    await message.answer(topic_intro(topic), reply_markup=MAIN_KB)

# ==== Инициализация ====
bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher(storage=MemoryStorage())

# ==== Хэндлеры ====
@dp.message(CommandStart())
async def on_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Привет! Я твой AI-помогатор. Нажми кнопку ниже или опиши ситуацию текстом.",
        reply_markup=MAIN_KB
    )

@dp.message(Command("help"))
async def on_help(message: Message):
    await message.answer(
        "Доступно:\n"
        "• /start — главное меню\n"
        "• Кнопки: «Не могу начать», «Отвлекаюсь», «Перегруз», «Нужен перерыв»\n"
        "• Или просто напиши свободным текстом.",
        reply_markup=MAIN_KB
    )

@dp.message(F.text.in_(list(TOPIC_MAP.keys())))
async def on_topic_selected(message: Message, state: FSMContext):
    await select_topic(message, state, TOPIC_MAP[message.text])

@dp.message(Flow.waiting_topic_details, F.text.len() > 0)
async def on_topic_details(message: Message, state: FSMContext):
    data = await state.get_data()
    topic: TopicType = data.get("topic", "cant_start")
    reply = topic_response(topic, message.text)
    await message.answer(reply, reply_markup=MAIN_KB)
    await state.clear()

@dp.message(Flow.waiting_topic_details)
async def on_topic_details_nontext(message: Message):
    await message.answer("Опиши, пожалуйста, словами — что происходит? 🙂", reply_markup=MAIN_KB)

@dp.message(F.text.len() > 0)
async def on_free_text(message: Message, state: FSMContext):
    text = message.text.strip()
    if text in TOPIC_MAP:
        await select_topic(message, state, TOPIC_MAP[text])
        return
    await message.answer(
        "Понял. Давай структурируем:\n"
        "1) Цель на 20–30 минут?\n"
        "2) Первый шаг на 5–10 минут?\n"
        "3) Один барьер?\n\n"
        "Можешь ответить пунктами или нажми кнопку ниже.",
        reply_markup=MAIN_KB
    )

@dp.message()
async def on_other(message: Message):
    await message.answer("Я понимаю только текст. Напиши пару слов или нажми кнопку ниже.", reply_markup=MAIN_KB)

# ==== Errors ====
@dp.errors()
async def errors_handler(update, exception):
    logger.exception("Ошибка в обработчике: %s | update=%s", exception, update)
    return True

# ==== Старт с авто-ретраем при конфликте webhook ====
async def main():
    logger.info("Бот запускается…")
    # первичная зачистка
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Webhook удалён, drop_pending_updates=True")
    except Exception as e:
        logger.warning(f"Не удалось удалить webhook: {e!r}")

    # бесконечный цикл: если кто-то снова поставит webhook — снесём и продолжим
    backoff = 1
    while True:
        try:
            await dp.start_polling(bot, allowed_updates=["message"])
        except TelegramConflictError:
            logger.warning("Конфликт: активен webhook. Удаляю и перезапускаю polling…")
            try:
                await bot.delete_webhook(drop_pending_updates=True)
            except Exception as e:
                logger.warning(f"Не удалось удалить webhook: {e!r}")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)  # экспоненциальный бэкофф до 30с
            continue
        except Exception as e:
            logger.exception(f"Неожиданная ошибка polling: {e!r}. Рестарт через 3с…")
            await asyncio.sleep(3)
            continue

if __name__ == "__main__":
    asyncio.run(main())
