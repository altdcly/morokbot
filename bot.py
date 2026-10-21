import os
import html
import logging
import asyncio
import secrets
import sqlite3
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command, CommandObject
from aiogram.types import Message

# ==================== НАСТРОЙКИ ====================
# Токен берём из переменной окружения BOT_TOKEN (в Amvera: раздел «Переменные»)
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = 641107229
CHANNEL_ID = -1004394668922

# Кто может проверять билеты командами /check и /list.
# Чтобы добавить человека на входе — впишите его Telegram ID через запятую.
STAFF_IDS = {ADMIN_ID}

# Файл базы. В Amvera папка /data сохраняется между перезапусками.
DB_PATH = os.getenv("DB_PATH", "/data/tickets.db")

# Ссылка на канал перестаёт работать после этой даты
LINK_EXPIRES = datetime(2026, 11, 1, 12, 0)

WELCOME_TEXT = (
    "Привет, {name}! 👋\n\n"
    "Вы оплачиваете билет на <b>«Морок и бал»</b>\n"
    "📅 31 октября, 20:00\n"
    "📍 ЧИНЫ, 9-я линия В.О., 54\n"
    "🎟 Стоимость билета: <b>750 ₽</b>\n\n"
    "🔗 <b>Оплата по СБП:</b>\n"
    "https://qr.nspk.ru/AS10003PQTEDCL9S9B5Q432RU29RFISJ\n\n"
    "После оплаты, пожалуйста, <b>пришлите скриншот чека прямо в этот чат</b> — "
    "и я сразу выдам вам билет и ссылку на канал с дополнительной информацией."
)
# ===================================================

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


# ---------- база данных ----------
def db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS tickets (
                code TEXT PRIMARY KEY,
                user_id INTEGER UNIQUE,
                username TEXT,
                full_name TEXT,
                invite_link TEXT,
                created_at TEXT,
                used INTEGER DEFAULT 0
            )"""
        )


def get_ticket_by_user(user_id):
    with db() as conn:
        return conn.execute("SELECT * FROM tickets WHERE user_id = ?", (user_id,)).fetchone()


def get_ticket_by_code(code):
    with db() as conn:
        return conn.execute("SELECT * FROM tickets WHERE code = ?", (code,)).fetchone()


def save_ticket(code, user, link):
    with db() as conn:
        conn.execute(
            "INSERT INTO tickets (code, user_id, username, full_name, invite_link, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (code, user.id, user.username, user.full_name, link,
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )


def new_code():
    # Уникальный код вида A3F9K2 (без похожих символов 0/O, 1/I)
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    while True:
        code = "".join(secrets.choice(alphabet) for _ in range(6))
        if not get_ticket_by_code(code):
            return code


def ticket_message(code, link):
    return (
        "✅ <b>Ваш билет готов!</b>\n\n"
        f"🎟 <b>Код билета:</b> <code>{code}</code>\n"
        f"🔗 <b>Канал с информацией:</b> {link}\n\n"
        "<i>Ссылка одноразовая — перейдите по ней сами и не передавайте другим. "
        "Сохраните код билета (или этот скриншот): на входе его нужно назвать.</i>"
    )


# ---------- хендлеры клиента ----------
@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(WELCOME_TEXT.format(name=html.escape(message.from_user.full_name)))


@dp.message(F.photo | F.document)
async def handle_receipt(message: Message):
    user = message.from_user

    # Если билет уже выдавали — повторяем тот же, а не создаём новый
    existing = get_ticket_by_user(user.id)
    if existing:
        await message.answer("Ваш билет уже выдан 👇")
        await message.answer(ticket_message(existing["code"], existing["invite_link"]))
        return

    await message.answer("🔄 Чек получен! Готовлю ваш билет...")

    try:
        code = new_code()
        invite = await bot.create_chat_invite_link(
            chat_id=CHANNEL_ID,
            member_limit=1,
            expire_date=LINK_EXPIRES,
            name=f"Ticket {code}",
        )
        link = invite.invite_link
        save_ticket(code, user, link)
    except Exception as e:
        logging.error(f"Ошибка создания билета: {e}")
        await message.answer("❌ Произошла ошибка. Пожалуйста, свяжитесь с организатором.")
        return

    await message.answer(ticket_message(code, link))

    # Копия организатору (ошибка здесь не должна ломать выдачу билета клиенту)
    try:
        uname = f"@{user.username}" if user.username else "нет username"
        admin_text = (
            "💰 <b>Новый чек!</b>\n"
            f"👤 {html.escape(user.full_name)} (ID: <code>{user.id}</code>, {html.escape(uname)})\n"
            f"🎟 Билет: <code>{code}</code>\n"
            f"🔗 {link}"
        )
        if message.photo:
            await bot.send_photo(ADMIN_ID, message.photo[-1].file_id, caption=admin_text)
        else:
            await bot.send_document(ADMIN_ID, message.document.file_id, caption=admin_text)
    except Exception as e:
        logging.error(f"Не удалось отправить копию админу: {e}")


# ---------- команды для организаторов ----------
@dp.message(Command("check"), F.from_user.id.in_(STAFF_IDS))
async def cmd_check(message: Message, command: CommandObject):
    code = (command.args or "").strip().upper()
    if not code:
        await message.answer("Использование: /check КОД")
        return
    t = get_ticket_by_code(code)
    if not t:
        await message.answer(f"❌ Билет <code>{html.escape(code)}</code> не найден.")
        return
    if t["used"]:
        await message.answer(
            f"⚠️ Билет <code>{code}</code> УЖЕ ИСПОЛЬЗОВАН\n👤 {html.escape(t['full_name'])}"
        )
        return
    with db() as conn:
        conn.execute("UPDATE tickets SET used = 1 WHERE code = ?", (code,))
    await message.answer(f"✅ Билет <code>{code}</code> действителен — пропускаем!\n👤 {html.escape(t['full_name'])}")


@dp.message(Command("list"), F.from_user.id.in_(STAFF_IDS))
async def cmd_list(message: Message):
    with db() as conn:
        rows = conn.execute("SELECT * FROM tickets ORDER BY created_at").fetchall()
    if not rows:
        await message.answer("Билетов пока нет.")
        return
    used = sum(1 for r in rows if r["used"])
    lines = [f"Всего билетов: {len(rows)}, прошли: {used}\n"]
    for r in rows:
        mark = "✅" if r["used"] else "▫️"
        lines.append(f"{mark} <code>{r['code']}</code> — {html.escape(r['full_name'])}")
    text = "\n".join(lines)
    for i in range(0, len(text), 4000):
        await message.answer(text[i:i + 4000])


# ---------- всё остальное ----------
@dp.message()
async def fallback(message: Message):
    await message.answer(
        "Чтобы получить билет, оплатите по ссылке из /start и пришлите сюда скриншот чека 📎"
    )


async def main():
    init_db()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
