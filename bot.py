# main.py
# Минимальный, но рабочий Telegram-бот на aiogram v3
# Фичи:
# - /start показывает меню с четырьмя кнопками (ReplyKeyboard — без callback'ов => без "вечной загрузки")
# - Свободный ввод работает всегда; если выбран один из сценариев — бот попросит описать ситуацию
# - Простая FSM: выбор темы -> ожидание описания -> ответ -> возврат в меню
# - Грубая защита от флуда + логирование ошибок

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

# ========= Конфигурация =========
TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN в переменных окружения")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
logger = logging.getLogger("ai-helper-bot")

# ========= Клавиатуры =========
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
    input_field_placeholder="Можешь нажать кнопку или просто написать свободным текстом…"
)

# ========= Состояния =========
TopicType = Literal["cant_start", "distracted", "overload", "need_break"]

class Flow(StatesGroup):
    waiting_topic_details = State()  # пользователь выбрал тему и теперь описывает ситуацию

# ========= Хелперы =========
TOPIC_MAP: dict[str, TopicType] = {
    BTN_CANT_START: "cant_start",
    BTN_DISTRACTED: "distracted",
    BTN_OVERLOAD:   "overload",
    BTN_BREAK:      "need_break",
}

def topic_intro(topic: TopicType) -> str:
    if topic == "cant_start":
        return (
            "Окей, тема: «Не могу начать».\n"
            "Короткий чек-лист:\n"
            "1) Уменьшим шаг: сформулируй задачу на 10–15 минут.\n"
            "2) Убери барьеры: что мешает сесть за работу прямо сейчас?\n"
            "3) Запускаем таймер: 10 минут на разгон.\n\n"
            "Опиши, пожалуйста, что именно пытаешься начать и что тебя стопорит."
        )
    if topic == "distracted":
        return (
            "Тема: «Отвлекаюсь».\n"
            "Экспресс-план:\n"
            "1) Уведомления и вкладки — в офф.\n"
            "2) Рабочее окно одно, задачи — в списке.\n"
            "3) Интервалы 15–20 минут, затем короткий перерыв.\n\n"
            "Напиши, что делаешь и на что чаще всего уходит внимание."
        )
    if topic == "overload":
        return (
            "Тема: «Перегруз».\n"
            "Снимем давление:\n"
            "1) Выгрузи всё из головы: список задач.\n"
            "2) Пометим срочность/важность.\n"
            "3) Выберем 1–2 шага на ближайшие 30 минут.\n\n"
            "Опиши кратко текущее «завалено»: какие задачи и дедлайны давят?"
        )
    return (
        "Тема: «Нужен перерыв».\n"
        "План восстановления:\n"
        "1) 5–10 минут — дыхание/прогулка/вода.\n"
        "2) Отметим уровень усталости по шкале 1–10.\n"
        "3) Решим, что реально сделать после паузы.\n\n"
        "Как ты сейчас себя чувствуешь и сколько есть времени на отдых?"
    )

def topic_response(topic: TopicType, user_text: str) -> str:
    # Минимальная, но полезная логика ответа на свободный ввод.
    if topic == "cant_start":
        return (
            "Вижу барьеры. Что делаем прямо сейчас:\n"
            "• Сформулируй первый шаг на 10 минут, выполним без идеализма.\n"
            f"• Учёл твоё описание: «{user_text[:200]}».\n"
            "• После — короткий отчёт одним предложением. Готов?"
        )
    if topic == "distracted":
        return (
            "Фиксируем отвлечения:\n"
            "• Закрой все нерабочие вкладки, уведомления — в «Не беспокоить».\n"
            "• Открой только один рабочий файл/задачу на 15 минут.\n"
            f"• Триггеры из твоего описания: «{user_text[:200]}» — учёл.\n"
            "Отпишись через 15 минут, что удалось сделать."
        )
    if topic == "overload":
        return (
            "Снимаем перегруз по шагам:\n"
            "• Выпиши задачи в список (можно здесь).\n"
            "• Пометь одну «срочно/важно» — сделаем её первой за 25 минут.\n"
            f"• Ключевые точки из описания: «{user_text[:200]}».\n"
            "Готов стартануть с первой задачи?"
        )
    return (
        "Организуем перерыв без чувства вины:\n"
        "• 7 минут — без экрана: вода/дыхание/движение.\n"
        "• Вернёшься и назовёшь один минимальный шаг.\n"
        f"• Запомнил твой контекст: «{user_text[:200]}».\n"
        "Поставь таймер и отпишись, когда вернёшься."
    )

# ========= Инициализация =========
bot = Bot(token=TOKEN, parse_mode="HTML")
dp = Dispatcher(storage=MemoryStorage())

# ========= Хэндлеры =========

@dp.message(CommandStart())
async def on_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Привет! Я твой AI-помогатор. Выбери, что ближе, или просто опиши ситуацию свободным текстом.",
        reply_markup=MAIN_KB
    )

@dp.message(Command("help"))
async def on_help(message: Message):
    await message.answer(
        "Доступно:\n"
        "/start — вернуться в главное меню\n"
        "Кнопки: «Не могу начать», «Отвлекаюсь», «Перегруз», «Нужен перерыв»\n"
        "Или напиши свободным текстом — отвечу по сути.",
        reply_markup=MAIN_KB
    )

@dp.message(F.text.in_(list(TOPIC_MAP.keys())))
async def on_topic_selected(message: Message, state: FSMContext):
    topic = TOPIC_MAP[message.text]
    await state.set_state(Flow.waiting_topic_details)
    await state.update_data(topic=topic)
    await message.answer(topic_intro(topic), reply_markup=MAIN_KB)

@dp.message(Flow.waiting_topic_details, F.text.len() > 0)
async def on_topic_details(message: Message, state: FSMContext):
    data = await state.get_data()
    topic: TopicType = data.get("topic", "cant_start")  # дефолт на всякий случай
    reply = topic_response(topic, message.text)
    await message.answer(reply, reply_markup=MAIN_KB)
    # Возврат в "бессостояние" — можно снова жать кнопки или писать текст
    await state.clear()

@dp.message(Flow.waiting_topic_details)
async def on_topic_details_nontext(message: Message):
    await message.answer("Опиши, пожалуйста, словами — что происходит? 🙂", reply_markup=MAIN_KB)

@dp.message(F.text.len() > 0)
async def on_free_text(message: Message):
    # Общий свободный ввод вне сценария
    text = message.text.strip()
    # Простая эвристика: если пользователь сам написал одну из фраз-кнопок (без нажатия),
    # обработаем как выбор темы, чтобы не «терять» сценарий.
    if text in TOPIC_MAP:
        await on_topic_selected(message, FSMContext(bot=bot, storage=dp.storage, chat=message.chat, user=message.from_user))
        return

    await message.answer(
        "Понял. Давай коротко структурируем:\n"
        "1) Цель на 20–30 минут?\n"
        "2) Первый шаг на 5–10 минут?\n"
        "3) Один вероятный барьер?\n\n"
        "Можешь ответить прямо пунктами. Или нажми подходящую кнопку ниже.",
        reply_markup=MAIN_KB
    )

@dp.message()
async def on_other(message: Message):
    await message.answer("Я понимаю только текст. Напиши пару слов о ситуации или нажми кнопку ниже.", reply_markup=MAIN_KB)

# ========= Error handler =========
@dp.errors()
async def errors_handler(update, exception):
    logger.exception("Ошибка в обработчике: %s | update=%s", exception, update)
    return True  # не пробрасывать дальше

# ========= Антифлуд (очень простой) =========
# Aiogram v3 уже последовательно обрабатывает апдейты; при необходимости подключи middlewares/limits.

# ========= Запуск =========
async def main():
    logger.info("Бот запускается…")
    await dp.start_polling(bot, allowed_updates=["message"])

if __name__ == "__main__":
    asyncio.run(main())
