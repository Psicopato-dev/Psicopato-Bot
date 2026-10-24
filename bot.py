"""Личный Telegram-бот v0.1: пароли, заметки, контакты."""
from __future__ import annotations

import asyncio
import html
import logging
import os
import secrets
import string
import time
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

import db
import vault

VERSION = "0.1"
LOCK_TIMEOUT = 300   # авто-блокировка паролей после 5 минут без активности
SECRET_TTL = 30      # через сколько секунд удалять сообщение с паролем
LIST_LIMIT = 50      # сколько записей показывать в списке
MAX_FAILS = 5        # попыток ввода мастер-пароля до паузы
BLOCK_SECONDS = 60   # длина паузы


# ---------- настройки ----------

def load_env() -> None:
    """Мини-загрузчик .env (чтобы не тянуть лишние зависимости)."""
    path = Path(__file__).parent / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


load_env()
TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_ID = int(os.environ.get("OWNER_ID", "0") or 0)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
# httpx на уровне INFO печатает URL запросов вместе с токеном бота!
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("mybot")

TABLES = {"nt": "notes", "ct": "contacts", "pw": "passwords"}
SECRET_STATES = {"pw_master_new", "pw_master_in", "pw_login", "pw_password"}


# ---------- вспомогательное ----------

def esc(text: str) -> str:
    return html.escape(text)


def kb(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(t, callback_data=d) for t, d in row] for row in rows]
    )


MENU = kb([("🔐 Пароли", "pw")], [("📝 Заметки", "nt")], [("👤 Контакты", "ct")])
CANCEL = kb([("✖️ Отмена", "cancel")])

_tasks: set[asyncio.Task] = set()


def spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def show(update: Update, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    """Редактирует сообщение с кнопкой или отправляет новое."""
    try:
        if update.callback_query:
            await update.callback_query.edit_message_text(
                text, reply_markup=markup, parse_mode=ParseMode.HTML
            )
        else:
            await update.effective_chat.send_message(
                text, reply_markup=markup, parse_mode=ParseMode.HTML
            )
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


async def safe_delete(message) -> None:
    try:
        await message.delete()
    except TelegramError:
        pass


async def self_destruct(message, delay: int) -> None:
    await asyncio.sleep(delay)
    await safe_delete(message)


async def send_secret(update: Update, text: str) -> None:
    """Отправляет секрет новым сообщением и удаляет его через SECRET_TTL секунд."""
    msg = await update.effective_chat.send_message(
        f"{text}\n\n⏱ Удалю через {SECRET_TTL} с.", parse_mode=ParseMode.HTML
    )
    spawn(self_destruct(msg, SECRET_TTL))


def gen_password(length: int = 16) -> str:
    symbols = "!@#$%&*-_+="
    alphabet = string.ascii_letters + string.digits + symbols
    while True:
        p = "".join(secrets.choice(alphabet) for _ in range(length))
        if (
            any(c.islower() for c in p)
            and any(c.isupper() for c in p)
            and any(c.isdigit() for c in p)
            and any(c in symbols for c in p)
        ):
            return p


async def show_list(update, key, title, rows, label, notice="", extra=()) -> None:
    buttons = [[(label(r)[:60], f"{key}:v:{r['id']}")] for r in rows]
    buttons.append([("➕ Добавить", f"{key}:add")])
    buttons.extend(extra)
    buttons.append([("⬅️ Меню", "menu")])
    head = f"{notice}\n\n" if notice else ""
    head += f"<b>{title}</b>"
    if not rows:
        head += "\nПока пусто."
    elif len(rows) >= LIST_LIMIT:
        head += f"\nПоказаны последние {LIST_LIMIT}."
    await show(update, head, kb(*buttons))


def item_kb(key: str, item_id: int, extra=()) -> InlineKeyboardMarkup:
    rows = list(extra)
    rows.append([("🗑 Удалить", f"{key}:d:{item_id}")])
    rows.append([("⬅️ Назад", key)])
    return kb(*rows)


# ---------- сессия менеджера паролей ----------

def unlocked(context: ContextTypes.DEFAULT_TYPE) -> bool:
    d = context.application.bot_data
    return d.get("fernet") is not None and time.monotonic() < d.get("until", 0)


def touch(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.application.bot_data["until"] = time.monotonic() + LOCK_TIMEOUT


def start_session(context: ContextTypes.DEFAULT_TYPE, fernet) -> None:
    context.application.bot_data["fernet"] = fernet
    touch(context)


def lock(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.application.bot_data.pop("fernet", None)
    context.application.bot_data.pop("until", None)


# ---------- главное меню ----------

async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, notice: str = "") -> None:
    context.user_data.clear()
    head = f"{notice}\n\n" if notice else ""
    await show(update, f"{head}<b>Личный бот v{VERSION}</b>\nВыбери раздел:", MENU)


async def lock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lock(context)
    await show_menu(update, context, "🔒 Менеджер паролей заблокирован.")


# ---------- заметки ----------

async def notes_list(update, context, notice: str = "") -> None:
    context.user_data.clear()
    rows = db.query("SELECT id, title FROM notes ORDER BY id DESC LIMIT ?", (LIST_LIMIT,))
    await show_list(update, "nt", "📝 Заметки", rows, lambda r: f"📝 {r['title']}", notice)


async def note_view(update, context, note_id: int) -> None:
    r = db.one("SELECT * FROM notes WHERE id = ?", (note_id,))
    if not r:
        return await notes_list(update, context, "Заметка не найдена.")
    body = r["body"]
    text = f"<b>{esc(r['title'])}</b>\n\n{esc(body[:3500])}"
    if len(body) > 3500:
        text += "…"
    await show(update, text, item_kb("nt", note_id))


# ---------- контакты ----------

async def contacts_list(update, context, notice: str = "") -> None:
    context.user_data.clear()
    rows = sorted(db.query("SELECT id, name FROM contacts"), key=lambda r: r["name"].casefold())
    await show_list(
        update, "ct", "👤 Контакты", rows[:LIST_LIMIT], lambda r: f"👤 {r['name']}", notice
    )


async def contact_view(update, context, contact_id: int) -> None:
    r = db.one("SELECT * FROM contacts WHERE id = ?", (contact_id,))
    if not r:
        return await contacts_list(update, context, "Контакт не найден.")
    text = f"👤 <b>{esc(r['name'])}</b>"
    if r["phone"]:
        text += f"\n📞 {esc(r['phone'])}"
    if r["info"]:
        text += f"\n\n{esc(r['info'][:3000])}"
    await show(update, text, item_kb("ct", contact_id))


# ---------- пароли ----------

async def pw_home(update, context, notice: str = "") -> None:
    if not vault.is_set():
        context.user_data.clear()
        context.user_data["state"] = "pw_master_new"
        return await show(
            update,
            "🔐 <b>Первый запуск менеджера паролей</b>\n\n"
            "Придумай <b>мастер-пароль</b> (минимум 8 символов). "
            "Он шифрует все пароли и нигде не хранится — если забудешь, "
            "данные восстановить будет невозможно.\n\n"
            "Сообщение с ним я сразу удалю из чата.",
            CANCEL,
        )
    if not unlocked(context):
        lock(context)
        context.user_data.clear()
        context.user_data["state"] = "pw_master_in"
        return await show(update, "🔒 Введи мастер-пароль:", CANCEL)

    touch(context)
    context.user_data.clear()
    rows = sorted(db.query("SELECT id, service FROM passwords"), key=lambda r: r["service"].casefold())
    await show_list(
        update, "pw", "🔐 Пароли", rows[:LIST_LIMIT], lambda r: f"🔑 {r['service']}", notice,
        extra=[[("🔒 Заблокировать", "pw:lock")]],
    )


async def pw_view(update, context, pw_id: int) -> None:
    r = db.one("SELECT id, service FROM passwords WHERE id = ?", (pw_id,))
    if not r:
        return await pw_home(update, context, "Запись не найдена.")
    touch(context)
    await show(
        update,
        f"🔑 <b>{esc(r['service'])}</b>",
        item_kb("pw", pw_id, extra=[[("👁 Показать данные", f"pw:s:{pw_id}")]]),
    )


async def pw_reveal(update, context, pw_id: int) -> None:
    r = db.one("SELECT * FROM passwords WHERE id = ?", (pw_id,))
    if not r:
        return await pw_home(update, context, "Запись не найдена.")
    fernet = context.application.bot_data["fernet"]
    login = fernet.decrypt(r["login_enc"]).decode("utf-8")
    password = fernet.decrypt(r["password_enc"]).decode("utf-8")
    touch(context)
    await send_secret(
        update,
        f"🔑 <b>{esc(r['service'])}</b>\n"
        f"👤 <code>{esc(login)}</code>\n"
        f"🔐 <code>{esc(password)}</code>",
    )


async def pw_save(update, context, password: str, reveal: bool = False) -> None:
    draft = context.user_data.get("draft", {})
    if not unlocked(context) or "service" not in draft:
        return await pw_home(update, context)
    fernet = context.application.bot_data["fernet"]
    db.execute(
        "INSERT INTO passwords (service, login_enc, password_enc) VALUES (?, ?, ?)",
        (
            draft["service"],
            fernet.encrypt(draft["login"].encode("utf-8")),
            fernet.encrypt(password.encode("utf-8")),
        ),
    )
    if reveal:
        await send_secret(
            update,
            f"🎲 Пароль для <b>{esc(draft['service'])}</b>:\n<code>{esc(password)}</code>",
        )
    await pw_home(update, context, f"✅ «{esc(draft['service'])}» сохранён.")


# ---------- обработчики ----------

LISTS = {"nt": notes_list, "ct": contacts_list, "pw": pw_home}
VIEWS = {"nt": note_view, "ct": contact_view, "pw": pw_view}
ADD_PROMPTS = {
    "nt": ("nt_add", "📝 Отправь текст заметки. Первая строка станет заголовком."),
    "ct": ("ct_name", "👤 Имя контакта:"),
    "pw": ("pw_service", "🌐 Название сервиса (например, Gmail):"),
}


async def guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Бот приватный: всё от чужих пользователей молча отбрасываем."""
    user = update.effective_user
    if user is None or user.id != OWNER_ID:
        log.warning("Отклонён чужой пользователь: %s", user.id if user else None)
        raise ApplicationHandlerStop


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    key = parts[0]
    action = parts[1] if len(parts) > 1 else ""
    arg = int(parts[2]) if len(parts) > 2 else 0

    if key in ("menu", "cancel"):
        return await show_menu(update, context)
    if key not in TABLES:
        return

    if key == "pw":
        if action == "lock":
            lock(context)
            return await show_menu(update, context, "🔒 Менеджер паролей заблокирован.")
        if action not in ("", "gen") and not unlocked(context):
            return await pw_home(update, context)

    if action == "":
        return await LISTS[key](update, context)
    if action == "add":
        state, prompt = ADD_PROMPTS[key]
        context.user_data.clear()
        context.user_data["state"] = state
        return await show(update, prompt, CANCEL)
    if action == "v":
        return await VIEWS[key](update, context, arg)
    if action == "d":
        return await show(
            update,
            "Удалить? Это действие нельзя отменить.",
            kb([("🗑 Да, удалить", f"{key}:dd:{arg}")], [("↩️ Нет", key)]),
        )
    if action == "dd":
        db.execute(f"DELETE FROM {TABLES[key]} WHERE id = ?", (arg,))
        return await LISTS[key](update, context, "🗑 Удалено.")
    if key == "pw" and action == "s":
        return await pw_reveal(update, context, arg)
    if key == "pw" and action == "gen" and context.user_data.get("state") == "pw_password":
        return await pw_save(update, context, gen_password(), reveal=True)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    text = (msg.text or "").strip()
    ud = context.user_data
    state = ud.get("state")

    if state in SECRET_STATES:
        await safe_delete(msg)  # убираем секрет из истории чата
    if not text:
        return

    # --- заметки ---
    if state == "nt_add":
        title = text.splitlines()[0][:40]
        db.execute("INSERT INTO notes (title, body) VALUES (?, ?)", (title, text))
        return await notes_list(update, context, "✅ Заметка сохранена.")

    # --- контакты ---
    if state == "ct_name":
        ud["draft"] = {"name": text[:100]}
        ud["state"] = "ct_phone"
        return await update.effective_chat.send_message("📞 Телефон (или «-», чтобы пропустить):", reply_markup=CANCEL)
    if state == "ct_phone":
        ud["draft"]["phone"] = "" if text == "-" else text[:50]
        ud["state"] = "ct_info"
        return await update.effective_chat.send_message(
            "ℹ️ Доп. информация — email, адрес, день рождения… (или «-»):", reply_markup=CANCEL
        )
    if state == "ct_info":
        d = ud["draft"]
        info = "" if text == "-" else text
        db.execute(
            "INSERT INTO contacts (name, phone, info) VALUES (?, ?, ?)",
            (d["name"], d["phone"], info),
        )
        return await contacts_list(update, context, f"✅ Контакт «{esc(d['name'])}» сохранён.")

    # --- пароли ---
    if state == "pw_master_new":
        if len(text) < 8:
            return await update.effective_chat.send_message(
                "Слишком короткий, нужно минимум 8 символов. Ещё раз:", reply_markup=CANCEL
            )
        fernet = await asyncio.to_thread(vault.setup, text)
        start_session(context, fernet)
        return await pw_home(update, context, "✅ Мастер-пароль установлен. Не забудь его!")

    if state == "pw_master_in":
        d = context.application.bot_data
        wait = d.get("block_until", 0) - time.monotonic()
        if wait > 0:
            return await update.effective_chat.send_message(
                f"⏳ Слишком много попыток. Подожди {int(wait) + 1} с.", reply_markup=CANCEL
            )
        fernet = await asyncio.to_thread(vault.unlock, text)
        if fernet is None:
            d["fails"] = d.get("fails", 0) + 1
            if d["fails"] >= MAX_FAILS:
                d["fails"] = 0
                d["block_until"] = time.monotonic() + BLOCK_SECONDS
            return await update.effective_chat.send_message("❌ Неверный мастер-пароль. Ещё раз:", reply_markup=CANCEL)
        d["fails"] = 0
        start_session(context, fernet)
        return await pw_home(update, context)

    if state == "pw_service":
        ud["draft"] = {"service": text[:60]}
        ud["state"] = "pw_login"
        return await update.effective_chat.send_message("👤 Логин или email:", reply_markup=CANCEL)
    if state == "pw_login":
        ud["draft"]["login"] = text
        ud["state"] = "pw_password"
        return await update.effective_chat.send_message(
            "🔑 Отправь пароль или сгенерируй:",
            reply_markup=kb([("🎲 Сгенерировать", "pw:gen")], [("✖️ Отмена", "cancel")]),
        )
    if state == "pw_password":
        return await pw_save(update, context, text)

    # нет активного сценария
    await show_menu(update, context)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Ошибка в обработчике", exc_info=context.error)


async def post_init(app: Application) -> None:
    await app.bot.set_my_commands(
        [("menu", "Главное меню"), ("lock", "Заблокировать пароли"), ("cancel", "Отмена")]
    )


def main() -> None:
    if not TOKEN or not OWNER_ID:
        raise SystemExit("Заполни BOT_TOKEN и OWNER_ID в файле .env (см. .env.example)")
    db.init()
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(TypeHandler(Update, guard), group=-1)
    app.add_handler(CommandHandler(["start", "menu", "cancel"], show_menu))
    app.add_handler(CommandHandler("lock", lock_cmd))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    log.info("Бот v%s запущен", VERSION)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
