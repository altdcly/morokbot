import os
import html
import logging
import asyncio
import secrets
import sqlite3
from datetime import datetime
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Font

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery, BufferedInputFile, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
)

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

EXPORT_BTN = "📥 Выгрузить Excel"
STAFF_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=EXPORT_BTN)]],
    resize_keyboard=True,
)

# Кодовое слово «для друзей»: вместо чека присылаете его — бот даёт ссылку на 350 ₽
FRIEND_CODEWORD = "кикимора"
FRIEND_TEXT = (
    "Кодовое слово принято! 🎉\n\n"
    "🎟 Стоимость билета для вас: <b>350 ₽</b>\n\n"
    "🔗 <b>Оплата по СБП:</b>\n"
    "https://qr.nspk.ru/BS10004NU3RV59CV8AM9RCCI12BUU36U\n\n"
    "После оплаты пришлите <b>скриншот чека</b> прямо в этот чат — "
    "и я выдам вам билет и ссылку на канал."
)

ASK_BTN = "❓ Задать вопрос"
CLIENT_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=ASK_BTN)]],
    resize_keyboard=True,
)


class Ask(StatesGroup):
    waiting = State()


class Reply(StatesGroup):
    waiting = State()


def reply_kb(user_id):
    """Кнопка «Ответить» под сообщением, которое приходит админу."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="✉️ Ответить", callback_data=f"reply:{user_id}")
        ]]
    )


WELCOME_TEXT = (
    "Привет, {name}! 👋\n\n"
    "Вы оплачиваете билет на <b>«Морок и бал»</b>\n"
    "📅 31 октября, 20:00\n"
    "📍 ЧИНЫ, 9-я линия В.О., 54\n"
    "🎟 Стоимость билета: <b>750 ₽</b>\n\n"
    "🔗 <b>Оплата по СБП:</b>\n"
    "https://qr.nspk.ru/AS10003PQTEDCL9S9B5Q432RU29RFISJ\n\n"
    "p.s. оплата может не открываться при включенном VPN\n"
    "После оплаты, пожалуйста, <b>пришлите скриншот чека прямо в этот чат</b> — "
    "и я сразу выдам вам билет и ссылку на канал с дополнительной информацией.\n\n"
    "Остались вопросы? Нажмите кнопку «❓ Задать вопрос» внизу."
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
        # какому клиенту принадлежит сообщение, пришедшее админу (для ответов)
        conn.execute(
            """CREATE TABLE IF NOT EXISTS msg_map (
                admin_msg_id INTEGER PRIMARY KEY,
                user_id INTEGER
            )"""
        )
        # кто ввёл кодовое слово и ждёт билет «друг»
        conn.execute(
            "CREATE TABLE IF NOT EXISTS friend_pending (user_id INTEGER PRIMARY KEY)"
        )
        # для баз, созданных до обновления: добавляем колонку «категория»
        try:
            conn.execute("ALTER TABLE tickets ADD COLUMN category TEXT DEFAULT 'обычный'")
        except sqlite3.OperationalError:
            pass


def get_ticket_by_user(user_id):
    with db() as conn:
        return conn.execute("SELECT * FROM tickets WHERE user_id = ?", (user_id,)).fetchone()


def get_ticket_by_code(code):
    with db() as conn:
        return conn.execute("SELECT * FROM tickets WHERE code = ?", (code,)).fetchone()


def save_ticket(code, user, link, category="обычный"):
    with db() as conn:
        conn.execute(
            "INSERT INTO tickets (code, user_id, username, full_name, invite_link, created_at, category) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (code, user.id, user.username, user.full_name, link,
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"), category),
        )


def mark_friend(user_id):
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO friend_pending (user_id) VALUES (?)", (user_id,))


def is_friend(user_id):
    with db() as conn:
        return conn.execute(
            "SELECT 1 FROM friend_pending WHERE user_id = ?", (user_id,)
        ).fetchone() is not None


def friend_tag(t):
    return "\n🏷 Пометка: <b>друг</b>" if t["category"] == "друг" else ""


def is_codeword(m):
    return bool(m.text) and m.text.strip().lower() == FRIEND_CODEWORD


def save_msg_map(admin_msg_id, user_id):
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO msg_map (admin_msg_id, user_id) VALUES (?, ?)",
            (admin_msg_id, user_id),
        )


def get_user_by_admin_msg(admin_msg_id):
    with db() as conn:
        row = conn.execute(
            "SELECT user_id FROM msg_map WHERE admin_msg_id = ?", (admin_msg_id,)
        ).fetchone()
    return row["user_id"] if row else None


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
    kb = STAFF_KB if message.from_user.id in STAFF_IDS else CLIENT_KB
    await message.answer(
        WELCOME_TEXT.format(name=html.escape(message.from_user.full_name)),
        reply_markup=kb,
    )


@dp.message(Command("cancel"), F.from_user.id == ADMIN_ID)
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.")


@dp.callback_query(F.data.startswith("reply:"), F.from_user.id == ADMIN_ID)
async def reply_button(call: CallbackQuery, state: FSMContext):
    user_id = int(call.data.split(":", 1)[1])
    t = get_ticket_by_user(user_id)
    who = f"{t['full_name']} (ID {user_id})" if t else f"ID {user_id}"
    await state.set_state(Reply.waiting)
    await state.update_data(target=user_id)
    await call.answer()
    await call.message.answer(
        f"✍️ Напишите ответ для <b>{html.escape(who)}</b> — следующее ваше сообщение уйдёт ему.\n"
        "Отмена: /cancel"
    )


@dp.message(Reply.waiting, F.from_user.id == ADMIN_ID)
async def reply_send(message: Message, state: FSMContext):
    if message.text and message.text.startswith("/"):
        await message.answer("Сейчас вы пишете ответ клиенту. Отправьте текст ответа или /cancel для отмены.")
        return
    data = await state.get_data()
    user_id = data.get("target")
    try:
        if message.text:
            await bot.send_message(
                user_id, f"💬 <b>Ответ организатора:</b>\n\n{html.escape(message.text)}"
            )
        else:
            await bot.send_message(user_id, "💬 <b>Ответ организатора:</b>")
            await message.copy_to(user_id)
        await message.answer("✅ Ответ отправлен клиенту.")
    except Exception as e:
        logging.error(f"Не удалось отправить ответ: {e}")
        await message.answer("❌ Не удалось отправить (возможно, клиент заблокировал бота).")
    await state.clear()


@dp.message(F.from_user.id == ADMIN_ID, F.reply_to_message)
async def admin_reply(message: Message):
    """Админ отвечает (функция «Ответить») на вопрос или чек — ответ уходит клиенту."""
    user_id = get_user_by_admin_msg(message.reply_to_message.message_id)
    if not user_id:
        await message.answer("Не нашла, какому клиенту отвечать. Отвечайте на сообщение с вопросом или чеком.")
        return
    try:
        if message.text:
            await bot.send_message(
                user_id, f"💬 <b>Ответ организатора:</b>\n\n{html.escape(message.text)}"
            )
        else:
            await bot.send_message(user_id, "💬 <b>Ответ организатора:</b>")
            await message.copy_to(user_id)
        await message.answer("✅ Ответ отправлен клиенту.")
    except Exception as e:
        logging.error(f"Не удалось отправить ответ: {e}")
        await message.answer("❌ Не удалось отправить (возможно, клиент заблокировал бота).")


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
        category = "друг" if is_friend(user.id) else "обычный"
        code = new_code()
        invite = await bot.create_chat_invite_link(
            chat_id=CHANNEL_ID,
            member_limit=1,
            expire_date=LINK_EXPIRES,
            name=f"Ticket {code}",
        )
        link = invite.invite_link
        save_ticket(code, user, link, category)
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
            f"🏷 Категория: <b>{category}</b>\n"
            f"👤 {html.escape(user.full_name)} (ID: <code>{user.id}</code>, {html.escape(uname)})\n"
            f"🎟 Билет: <code>{code}</code>\n"
            f"🔗 {link}"
        )
        if message.photo:
            sent = await bot.send_photo(
                ADMIN_ID, message.photo[-1].file_id, caption=admin_text, reply_markup=reply_kb(user.id)
            )
        else:
            sent = await bot.send_document(
                ADMIN_ID, message.document.file_id, caption=admin_text, reply_markup=reply_kb(user.id)
            )
        save_msg_map(sent.message_id, user.id)
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
            f"⚠️ Билет <code>{code}</code> УЖЕ ИСПОЛЬЗОВАН\n👤 {html.escape(t['full_name'])}{friend_tag(t)}"
        )
        return
    with db() as conn:
        conn.execute("UPDATE tickets SET used = 1 WHERE code = ?", (code,))
    await message.answer(f"✅ Билет <code>{code}</code> действителен — пропускаем!\n👤 {html.escape(t['full_name'])}{friend_tag(t)}")


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
        tag = " (друг)" if r["category"] == "друг" else ""
        lines.append(f"{mark} <code>{r['code']}</code> — {html.escape(r['full_name'])}{tag}")
    text = "\n".join(lines)
    for i in range(0, len(text), 4000):
        await message.answer(text[i:i + 4000])


@dp.message(Command("admin"), F.from_user.id.in_(STAFF_IDS))
async def cmd_admin(message: Message):
    await message.answer("Панель организатора 👇", reply_markup=STAFF_KB)


@dp.message(F.text == EXPORT_BTN, F.from_user.id.in_(STAFF_IDS))
async def export_excel(message: Message):
    with db() as conn:
        rows = conn.execute("SELECT * FROM tickets ORDER BY created_at").fetchall()
    if not rows:
        await message.answer("Билетов пока нет — выгружать нечего.")
        return

    wb = Workbook()
    ws = wb.active
    ws.title = "Билеты"
    ws.append(["ID и никнейм", "Билет", "Пометка"])
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for r in rows:
        nick = f"@{r['username']}" if r["username"] else "без ника"
        ws.append([f"{r['user_id']} {nick}", r["code"], "друг" if r["category"] == "друг" else ""])
    ws.column_dimensions["A"].width = 32
    ws.column_dimensions["B"].width = 14
    ws.column_dimensions["C"].width = 12

    buf = BytesIO()
    wb.save(buf)
    filename = f"tickets_{datetime.now().strftime('%Y-%m-%d_%H-%M')}.xlsx"
    await message.answer_document(
        BufferedInputFile(buf.getvalue(), filename=filename),
        caption=f"Всего билетов: {len(rows)}",
    )


# ---------- кодовое слово «друг» ----------
@dp.message(is_codeword)
async def friend_code(message: Message, state: FSMContext):
    await state.clear()
    if get_ticket_by_user(message.from_user.id):
        await message.answer("У вас уже есть билет 🎟 Если нужна помощь — нажмите «❓ Задать вопрос».")
        return
    mark_friend(message.from_user.id)
    await message.answer(FRIEND_TEXT)


# ---------- вопросы от клиентов ----------
@dp.message(F.text == ASK_BTN)
async def ask_start(message: Message, state: FSMContext):
    await state.set_state(Ask.waiting)
    await message.answer("Напишите ваш вопрос одним сообщением — я передам его организатору 👇")


@dp.message(Ask.waiting, F.text)
async def ask_receive(message: Message, state: FSMContext):
    user = message.from_user
    uname = f"@{user.username}" if user.username else "нет username"
    text = (
        "❓ <b>Новый вопрос</b>\n"
        f"👤 {html.escape(user.full_name)} (ID: <code>{user.id}</code>, {html.escape(uname)})\n\n"
        f"{html.escape(message.text)}"
    )
    try:
        sent = await bot.send_message(ADMIN_ID, text, reply_markup=reply_kb(user.id))
        save_msg_map(sent.message_id, user.id)
        await message.answer("✅ Вопрос отправлен! Ответ придёт сюда, в этот чат.")
    except Exception as e:
        logging.error(f"Не удалось передать вопрос: {e}")
        await message.answer("❌ Не получилось отправить вопрос. Попробуйте позже.")
    await state.clear()


# ---------- всё остальное ----------
@dp.message()
async def fallback(message: Message):
    await message.answer(
        "Чтобы получить билет, оплатите по ссылке из /start и пришлите сюда скриншот чека 📎\n"
        "Есть вопрос? Нажмите «❓ Задать вопрос»."
    )


async def main():
    init_db()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
