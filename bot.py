"""Телеграм-бот-помощник Юлии Курицыной («Код Женщины»)."""
import asyncio
import logging
import os
import random
import sqlite3
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ParseMode
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from dotenv import load_dotenv
from openai import AsyncOpenAI

import prompts

HERE = Path(__file__).parent
load_dotenv(HERE / ".env")

BOT_TOKEN = os.environ["BOT_TOKEN"]
LLM_BASE_URL = os.environ["LLM_BASE_URL"]
LLM_API_KEY = os.environ["LLM_API_KEY"].strip().lstrip("﻿")
LLM_MODEL = os.environ["LLM_MODEL"]
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID") or 0)
CONTACT_TG = os.getenv("CONTACT_TG", "@yuliya_kuritsyna")
CONTACT_PHONE = os.getenv("CONTACT_PHONE", "+7 968 481 78 00")
CHANNEL_URL = os.getenv("CHANNEL_URL", "https://t.me/kuritsyna_yuliya")
DAILY_LIMIT = int(os.getenv("DAILY_LIMIT", "30"))
HISTORY_LEN = 16
MSK = timezone(timedelta(hours=3))
# На Bothost постоянное хранилище — /app/data, остальное стирается при обновлении
DATA_DIR = Path(os.getenv("DATA_DIR") or ("/app/data" if Path("/app/data").is_dir() else HERE))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

llm = AsyncOpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY, timeout=90)
SYSTEM = prompts.system_prompt(CONTACT_TG, CONTACT_PHONE, CHANNEL_URL)
SERVICES = prompts.read("services.md")
history: dict[int, deque] = defaultdict(lambda: deque(maxlen=HISTORY_LEN))

# --- База данных -------------------------------------------------------------

db = sqlite3.connect(DATA_DIR / "bot.db")
db.executescript("""
CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, username TEXT, name TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS usage (user_id INTEGER, day TEXT, n INTEGER, PRIMARY KEY (user_id, day));
CREATE TABLE IF NOT EXISTS cards (user_id INTEGER, day TEXT, text TEXT, PRIMARY KEY (user_id, day));
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, username TEXT, name TEXT,
    request TEXT, format TEXT, time TEXT, contact TEXT, created TEXT);
""")


def now() -> datetime:
    return datetime.now(MSK)


def today() -> str:
    return now().date().isoformat()


def remember_user(m: Message) -> None:
    u = m.from_user
    db.execute(
        "INSERT OR IGNORE INTO users VALUES (?, ?, ?, ?)",
        (u.id, u.username, u.full_name, now().isoformat(timespec="seconds")),
    )
    db.commit()


def take_quota(user_id: int) -> bool:
    """Списывает один запрос к нейросети; False, если дневной лимит исчерпан."""
    if user_id == ADMIN_CHAT_ID:
        return True
    row = db.execute("SELECT n FROM usage WHERE user_id=? AND day=?", (user_id, today())).fetchone()
    if row and row[0] >= DAILY_LIMIT:
        return False
    db.execute(
        "INSERT INTO usage VALUES (?, ?, 1) ON CONFLICT(user_id, day) DO UPDATE SET n = n + 1",
        (user_id, today()),
    )
    db.commit()
    return True


# --- Клавиатуры --------------------------------------------------------------

BTN_TALK = "🌿 Разобраться в запросе"
BTN_CARD = "🃏 Послание дня"
BTN_SERVICES = "💎 Услуги и цены"
BTN_BOOK = "✍️ Записаться"
BTN_CHANNEL = "📢 Канал «Код Женщины»"
BTN_CANCEL = "Отмена"
BTN_WRITE_TG = "Напишите мне в Телеграм"

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_TALK)],
        [KeyboardButton(text=BTN_CARD), KeyboardButton(text=BTN_SERVICES)],
        [KeyboardButton(text=BTN_BOOK), KeyboardButton(text=BTN_CHANNEL)],
    ],
    resize_keyboard=True,
    input_field_placeholder="Напишите, что у вас на душе…",
)
CANCEL_KB = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=BTN_CANCEL)]], resize_keyboard=True)
CONTACT_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📱 Отправить номер телефона", request_contact=True)],
        [KeyboardButton(text=BTN_WRITE_TG)],
        [KeyboardButton(text=BTN_CANCEL)],
    ],
    resize_keyboard=True,
)
FORMATS = [
    "Экспресс-сессия (40 мин)",
    "Расстановочная сессия (1,5–2 ч)",
    "Сопровождение (4 сессии)",
    "Расклад на таро",
    "Подарочный сертификат",
    "Пока не знаю — нужна консультация",
]
FORMAT_KB = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text=f, callback_data=f"fmt:{i}")] for i, f in enumerate(FORMATS)]
)
CHANNEL_KB = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Открыть канал", url=CHANNEL_URL)]])
BOOK_KB = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=BTN_BOOK, callback_data="book")]])

router = Router()


# --- Нейросеть ---------------------------------------------------------------

async def ask_llm(messages: list[dict], max_tokens: int = 4000) -> str:
    # Лимит с запасом: модели с «размышлением» тратят часть токенов до ответа
    resp = await llm.chat.completions.create(
        model=LLM_MODEL, messages=messages, temperature=0.8, max_tokens=max_tokens
    )
    return (resp.choices[0].message.content or "").strip()


async def typing(m: Message) -> None:
    await m.bot.send_chat_action(m.chat.id, ChatAction.TYPING)


LIMIT_TEXT = (
    "На сегодня я исчерпал лимит сообщений 🙏 Давайте продолжим завтра. "
    f"А если вопрос срочный — напишите Юлии напрямую: {CONTACT_TG}"
)
ERROR_TEXT = (
    "Простите, у меня сейчас не получилось ответить. Попробуйте ещё раз через минуту "
    f"или напишите Юлии напрямую: {CONTACT_TG}"
)


# --- Общие команды -----------------------------------------------------------

@router.message(CommandStart())
async def start(m: Message, state: FSMContext) -> None:
    await state.clear()
    remember_user(m)
    history.pop(m.from_user.id, None)
    await m.answer(prompts.WELCOME.format(name=escape(m.from_user.first_name or "")), reply_markup=MAIN_KB)


@router.message(Command("myid"))
async def my_id(m: Message) -> None:
    await m.answer(f"Ваш идентификатор чата: <code>{m.chat.id}</code>")


@router.message(Command("stats"))
async def stats(m: Message) -> None:
    if m.from_user.id != ADMIN_CHAT_ID:
        return
    users = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    leads = db.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    active = db.execute("SELECT COUNT(*) FROM usage WHERE day=?", (today(),)).fetchone()[0]
    await m.answer(f"Пользователей: {users}\nЗаявок: {leads}\nПисали боту сегодня: {active}")


@router.message(Command("cancel"))
@router.message(F.text == BTN_CANCEL)
async def cancel(m: Message, state: FSMContext) -> None:
    await state.clear()
    await m.answer("Хорошо, отменили. Я здесь, если понадоблюсь.", reply_markup=MAIN_KB)


@router.message(StateFilter(None), F.text == BTN_SERVICES)
async def services(m: Message) -> None:
    await m.answer(SERVICES, reply_markup=BOOK_KB)


@router.message(StateFilter(None), F.text == BTN_CHANNEL)
async def channel(m: Message) -> None:
    await m.answer(
        "В канале «Код Женщины» — тексты из тишины, практики, разборы и анонсы групп 💎",
        reply_markup=CHANNEL_KB,
    )


@router.message(StateFilter(None), F.text == BTN_TALK)
async def talk(m: Message) -> None:
    history.pop(m.from_user.id, None)
    await m.answer(prompts.TALK_INTRO)


@router.message(StateFilter(None), F.text == BTN_CARD)
async def card(m: Message) -> None:
    uid = m.from_user.id
    row = db.execute("SELECT text FROM cards WHERE user_id=? AND day=?", (uid, today())).fetchone()
    if row:
        await m.answer(row[0] + "\n\nНовое послание будет завтра 🌙")
        return
    if not take_quota(uid):
        await m.answer(LIMIT_TEXT)
        return
    await typing(m)
    image = random.choice(prompts.CARD_IMAGES)
    try:
        text = await ask_llm(
            [{"role": "system", "content": SYSTEM},
             {"role": "user", "content": prompts.CARD_PROMPT.format(image=image)}]
        )
    except Exception:
        log.exception("Ошибка нейросети (послание дня)")
        await m.answer(ERROR_TEXT)
        return
    db.execute("INSERT OR REPLACE INTO cards VALUES (?, ?, ?)", (uid, today(), text))
    db.commit()
    await m.answer(text, parse_mode=None)


# --- Запись ------------------------------------------------------------------

class Booking(StatesGroup):
    name = State()
    request = State()
    format = State()
    time = State()
    contact = State()


async def book_start(m: Message, state: FSMContext, name: str) -> None:
    await state.set_state(Booking.name)
    await m.answer(
        f"Давайте запишемся к Юлии ✍️ Это займёт минуту.\n\nКак к вам обращаться?"
        + (f" Можно просто ответить «{escape(name)}»." if name else ""),
        reply_markup=CANCEL_KB,
    )


@router.message(StateFilter(None), F.text == BTN_BOOK)
async def book_from_menu(m: Message, state: FSMContext) -> None:
    await book_start(m, state, m.from_user.first_name or "")


@router.callback_query(F.data == "book")
async def book_from_button(c: CallbackQuery, state: FSMContext) -> None:
    await c.answer()
    await book_start(c.message, state, c.from_user.first_name or "")


@router.message(Booking.name, F.text)
async def book_name(m: Message, state: FSMContext) -> None:
    await state.update_data(name=m.text.strip()[:100])
    await state.set_state(Booking.request)
    await m.answer(
        "С чем хотите прийти? Пара предложений — достаточно. "
        "Можно и «не знаю, просто чувствую, что нужна поддержка»."
    )


@router.message(Booking.request, F.text)
async def book_request(m: Message, state: FSMContext) -> None:
    await state.update_data(request=m.text.strip()[:1500])
    await state.set_state(Booking.format)
    await m.answer("Какой формат вам ближе?", reply_markup=FORMAT_KB)


@router.callback_query(Booking.format, F.data.startswith("fmt:"))
async def book_format(c: CallbackQuery, state: FSMContext) -> None:
    fmt = FORMATS[int(c.data.split(":")[1])]
    await state.update_data(format=fmt)
    await state.set_state(Booking.time)
    await c.answer()
    await c.message.edit_text(f"Формат: {fmt}")
    await c.message.answer(
        "Когда вам удобно? Напишите дни и время, а также онлайн или очно в Москве.",
        reply_markup=CANCEL_KB,
    )


@router.message(Booking.format)
async def book_format_text(m: Message) -> None:
    await m.answer("Пожалуйста, выберите формат кнопкой выше 👆 или нажмите «Отмена».")


@router.message(Booking.time, F.text)
async def book_time(m: Message, state: FSMContext) -> None:
    await state.update_data(time=m.text.strip()[:300])
    await state.set_state(Booking.contact)
    hint = "" if m.from_user.username else (
        "\n\nУ вас в Телеграме не указано имя пользователя, поэтому лучше оставить номер телефона."
    )
    await m.answer("Как Юлии с вами связаться?" + hint, reply_markup=CONTACT_KB)


@router.message(Booking.contact)
async def book_contact(m: Message, state: FSMContext) -> None:
    u = m.from_user
    if m.contact:
        contact = m.contact.phone_number
    elif m.text == BTN_WRITE_TG:
        contact = f"@{u.username}" if u.username else f"Телеграм, id {u.id} (без имени пользователя)"
    elif m.text:
        contact = m.text.strip()[:200]
    else:
        await m.answer("Отправьте номер кнопкой или напишите контакт текстом.")
        return
    data = await state.get_data()
    await state.clear()
    db.execute(
        "INSERT INTO leads (user_id, username, name, request, format, time, contact, created) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (u.id, u.username, data["name"], data["request"], data["format"], data["time"], contact,
         now().isoformat(timespec="seconds")),
    )
    db.commit()

    sent = False
    if ADMIN_CHAT_ID:
        link = f'<a href="tg://user?id={u.id}">{escape(u.full_name)}</a>'
        text = (
            "🔔 <b>Новая заявка из бота</b>\n\n"
            f"<b>Имя:</b> {escape(data['name'])}\n"
            f"<b>Формат:</b> {escape(data['format'])}\n"
            f"<b>Запрос:</b> {escape(data['request'])}\n"
            f"<b>Когда удобно:</b> {escape(data['time'])}\n"
            f"<b>Контакт:</b> {escape(contact)}\n"
            f"<b>Профиль:</b> {link}" + (f" (@{u.username})" if u.username else "")
        )
        try:
            await m.bot.send_message(ADMIN_CHAT_ID, text)
            sent = True
        except Exception:
            log.exception("Не удалось отправить заявку администратору")

    if sent:
        reply = "Спасибо 🫂 Заявка у Юлии. Она свяжется с вами в ближайшее время и предложит время."
    else:
        reply = (
            "Спасибо 🫂 Заявку я сохранил. Чтобы было быстрее, напишите, пожалуйста, "
            f"Юлии напрямую: {CONTACT_TG}"
        )
    await m.answer(reply, reply_markup=MAIN_KB)


# --- Свободный диалог --------------------------------------------------------

@router.message(StateFilter(None), F.text)
async def chat(m: Message) -> None:
    uid = m.from_user.id
    if prompts.CRISIS_RE.search(m.text):
        log.warning("Кризисное сообщение от %s", uid)
        await m.answer(prompts.CRISIS)
        return
    if not take_quota(uid):
        await m.answer(LIMIT_TEXT)
        return
    await typing(m)
    hist = history[uid]
    hist.append({"role": "user", "content": m.text[:3000]})
    try:
        answer = await ask_llm([{"role": "system", "content": SYSTEM}, *hist])
    except Exception:
        log.exception("Ошибка нейросети (диалог)")
        hist.pop()
        await m.answer(ERROR_TEXT)
        return
    if not answer:
        hist.pop()
        await m.answer(ERROR_TEXT)
        return
    hist.append({"role": "assistant", "content": answer})
    await m.answer(answer, parse_mode=None)


@router.message(StateFilter(None))
async def other(m: Message) -> None:
    await m.answer("Я пока понимаю только текст 🙏 Напишите, пожалуйста, словами.")


async def main() -> None:
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    me = await bot.get_me()
    log.info("Бот @%s запущен, модель %s", me.username, LLM_MODEL)
    if not ADMIN_CHAT_ID:
        log.warning("ADMIN_CHAT_ID не задан — заявки будут только сохраняться в bot.db")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
