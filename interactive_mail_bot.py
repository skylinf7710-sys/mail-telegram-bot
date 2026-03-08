import asyncio
import email
import html
import imaplib
import json
import os
import time
from collections import deque
from email.header import decode_header
from typing import Any

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    Application,
    MessageHandler,
    filters,
)

CONFIG_FILE = "config.json"
DEFAULT_POLL_INTERVAL = 60
OWNER_IDS = [5127424995, 5408774698]
ACCESS_DENIED_TEXT = "Этот бот доступен только @skylinf"

MAIL_BURST_LIMIT = 5
MAIL_BURST_WINDOW = 60
DEDUP_TTL_SECONDS = 3600
SPAM_ALERT_COOLDOWN = 300

poll_task = None

mail_rate_limit: dict[str, deque] = {}
recent_mail_fingerprints: dict[str, float] = {}
spam_alert_state: dict[str, float] = {}
suppressed_counts: dict[str, int] = {}

STATE_IDLE = "idle"
STATE_ADD_LABEL = "add_label"
STATE_ADD_EMAIL = "add_email"
STATE_ADD_PASSWORD = "add_password"
STATE_REMOVE_SELECT = "remove_select"
STATE_SET_POLL = "set_poll"

IMAP_BY_DOMAIN = {
    "gmail.com": "imap.gmail.com",
    "googlemail.com": "imap.gmail.com",
    "mail.ru": "imap.mail.ru",
    "inbox.ru": "imap.mail.ru",
    "list.ru": "imap.mail.ru",
    "bk.ru": "imap.mail.ru",
    "yandex.ru": "imap.yandex.ru",
    "ya.ru": "imap.yandex.ru",
    "yandex.com": "imap.yandex.com",
    "outlook.com": "outlook.office365.com",
    "hotmail.com": "outlook.office365.com",
    "live.com": "outlook.office365.com",
    "msn.com": "outlook.office365.com",
    "icloud.com": "imap.mail.me.com",
    "me.com": "imap.mail.me.com",
    "mac.com": "imap.mail.me.com",
    "zoho.com": "imap.zoho.com",
    "aol.com": "imap.aol.com",
    "yahoo.com": "imap.mail.yahoo.com",
    "yahoo.de": "imap.mail.yahoo.com",
    "yahoo.co.uk": "imap.mail.yahoo.com",
}


def is_owner(update) -> bool:
    return update.effective_chat is not None and update.effective_chat.id in OWNER_IDS


def get_user_id(update) -> str:
    return str(update.effective_chat.id)


def guess_imap(email_value: str) -> str:
    if "@" not in email_value:
        return ""
    domain = email_value.split("@", 1)[1].lower().strip()
    return IMAP_BY_DOMAIN.get(domain, f"imap.{domain}")


def make_mail_key(user_id: str, email_addr: str) -> str:
    return f"{user_id}:{email_addr.lower()}"


def load_config() -> dict[str, Any]:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    else:
        data = {}

    if "users" not in data:
        data["users"] = {}

    for owner_id in OWNER_IDS:
        owner_key = str(owner_id)
        if owner_key not in data["users"]:
            data["users"][owner_key] = {
                "emails": [],
                "poll_interval": DEFAULT_POLL_INTERVAL,
                "ui_state": STATE_IDLE,
                "draft_email": {},
            }
        else:
            user_data = data["users"][owner_key]
            if "emails" not in user_data:
                user_data["emails"] = []
            if "poll_interval" not in user_data:
                user_data["poll_interval"] = DEFAULT_POLL_INTERVAL
            if "ui_state" not in user_data:
                user_data["ui_state"] = STATE_IDLE
            if "draft_email" not in user_data:
                user_data["draft_email"] = {}

    return data


def save_config(config: dict[str, Any]) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


def ensure_user(config: dict[str, Any], user_id: str) -> None:
    if "users" not in config:
        config["users"] = {}
    if user_id not in config["users"]:
        config["users"][user_id] = {
            "emails": [],
            "poll_interval": DEFAULT_POLL_INTERVAL,
            "ui_state": STATE_IDLE,
            "draft_email": {},
        }


def get_user_data(config: dict[str, Any], user_id: str) -> dict[str, Any]:
    ensure_user(config, user_id)
    return config["users"][user_id]


def mask_email(email_value: str) -> str:
    if "@" not in email_value:
        return email_value
    name, domain = email_value.split("@", 1)
    if len(name) <= 2:
        return "*" * len(name) + "@" + domain
    return name[:2] + "*" * (len(name) - 2) + "@" + domain


def escape_html(text: str) -> str:
    return html.escape(text or "")


def get_main_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton("➕ Добавить почту"), KeyboardButton("📋 Почты")],
        [KeyboardButton("🗑 Удалить почту"), KeyboardButton("▶️ Запуск")],
        [KeyboardButton("⏹ Стоп"), KeyboardButton("🧪 Тест")],
        [KeyboardButton("🛡 Антиспам"), KeyboardButton("⚙️ Настройки")],
        [KeyboardButton("❓ Помощь"), KeyboardButton("❌ Отмена")],
    ]
    return ReplyKeyboardMarkup(
        keyboard,
        resize_keyboard=True,
        one_time_keyboard=False,
        selective=True,
    )


def extract_plain_text(message: email.message.Message) -> str:
    if message.is_multipart():
        for part in message.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition"))
            if content_type == "text/plain" and "attachment" not in content_disposition:
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    try:
                        return payload.decode(
                            part.get_content_charset() or "utf-8",
                            errors="ignore",
                        ).strip()
                    except Exception:
                        continue
    else:
        payload = message.get_payload(decode=True)
        if isinstance(payload, bytes):
            try:
                return payload.decode(
                    message.get_content_charset() or "utf-8",
                    errors="ignore",
                ).strip()
            except Exception:
                pass
    return ""


def decode_mime_header(value: str) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    result = []
    for part, encoding in parts:
        if isinstance(part, bytes):
            try:
                result.append(part.decode(encoding or "utf-8", errors="ignore"))
            except Exception:
                result.append(part.decode("utf-8", errors="ignore"))
        else:
            result.append(part)
    return "".join(result)


def cleanup_old_fingerprints() -> None:
    now = time.time()
    expired = [
        key for key, ts in recent_mail_fingerprints.items()
        if now - ts > DEDUP_TTL_SECONDS
    ]
    for key in expired:
        del recent_mail_fingerprints[key]


def is_duplicate_mail(user_id: str, mailbox_email: str, subject: str, from_header: str, body: str) -> bool:
    cleanup_old_fingerprints()
    short_body = (body or "")[:200].strip()
    fingerprint = f"{user_id}|{mailbox_email}|{subject}|{from_header}|{short_body}"
    now = time.time()

    if fingerprint in recent_mail_fingerprints:
        return True

    recent_mail_fingerprints[fingerprint] = now
    return False


def can_send_mail_from_box(user_id: str, mailbox_email: str) -> bool:
    now = time.time()
    key = make_mail_key(user_id, mailbox_email)

    if key not in mail_rate_limit:
        mail_rate_limit[key] = deque()

    q = mail_rate_limit[key]

    while q and now - q[0] > MAIL_BURST_WINDOW:
        q.popleft()

    if len(q) >= MAIL_BURST_LIMIT:
        return False

    q.append(now)
    return True


def should_send_spam_alert(user_id: str, mailbox_email: str) -> bool:
    now = time.time()
    key = make_mail_key(user_id, mailbox_email)
    last_alert = spam_alert_state.get(key, 0)
    if now - last_alert < SPAM_ALERT_COOLDOWN:
        return False
    spam_alert_state[key] = now
    return True


def format_email_message(
    label: str,
    mailbox_email: str,
    from_header: str,
    subject: str,
    body: str,
) -> str:
    safe_label = escape_html(label or "БЕЗ НАЗВАНИЯ")
    safe_mailbox = escape_html(mailbox_email)
    safe_from = escape_html(from_header or "Неизвестно")
    safe_subject = escape_html(subject or "Без темы")

    clean_body = (body or "").strip()
    if len(clean_body) > 1400:
        clean_body = clean_body[:1400].rstrip() + "\n\n...[обрезано]"

    safe_body = escape_html(clean_body or "[пустое сообщение]")

    return (
        "📬 <b>НОВОЕ ПИСЬМО</b>\n"
        "══════════════════\n"
        f"📮 <b>Ящик:</b> <code>{safe_label}</code>\n"
        f"📧 <b>Адрес:</b> <code>{safe_mailbox}</code>\n"
        f"👤 <b>От:</b> {safe_from}\n"
        f"📝 <b>Тема:</b> {safe_subject}\n"
        "══════════════════\n"
        f"{safe_body}"
    )


def build_start_text(chat_id: int) -> str:
    return (
        "👋 <b>Привет! Я бот для пересылки почты в Telegram.</b>\n\n"
        f"✅ Ваш chat_id: <code>{chat_id}</code>\n"
        "✅ У вас будут только ваши почты и только ваши уведомления.\n\n"
        "<b>Как пользоваться:</b>\n"
        "1. Нажмите <b>➕ Добавить почту</b>\n"
        "2. По шагам введите название, email и app password\n"
        "3. Нажмите <b>🧪 Тест</b>\n"
        "4. Нажмите <b>▶️ Запуск</b>\n\n"
        "Все действия выполняются через кнопки."
    )


def build_help_text() -> str:
    return (
        "📚 <b>Как пользоваться ботом</b>\n\n"
        "➕ <b>Добавить почту</b>\n"
        "Бот по шагам спросит:\n"
        "• название ящика\n"
        "• email\n"
        "• пароль приложения\n\n"
        "📋 <b>Почты</b>\n"
        "Показывает только ваши подключённые ящики.\n\n"
        "🗑 <b>Удалить почту</b>\n"
        "Показывает только ваши почты.\n\n"
        "🧪 <b>Тест</b>\n"
        "Проверка, что бот умеет отправлять сообщения вам.\n\n"
        "▶️ <b>Запуск</b>\n"
        "Начинает проверять только ваши почты.\n\n"
        "⏹ <b>Стоп</b>\n"
        "Останавливает пересылку.\n\n"
        "🛡 <b>Антиспам</b>\n"
        "Показывает ваш статус антиспама.\n\n"
        "⚙️ <b>Настройки</b>\n"
        "Позволяет поменять ваш интервал проверки."
    )


async def send_to_user(context: ContextTypes.DEFAULT_TYPE, user_id: int | str, text: str, parse_mode: str | None = None) -> None:
    await context.bot.send_message(chat_id=int(user_id), text=text, parse_mode=parse_mode)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        await update.message.reply_text(ACCESS_DENIED_TEXT)
        return

    config = load_config()
    user_id = get_user_id(update)
    user_data = get_user_data(config, user_id)
    user_data["ui_state"] = STATE_IDLE
    user_data["draft_email"] = {}
    save_config(config)

    await update.message.reply_text(
        build_start_text(update.effective_chat.id),
        parse_mode="HTML",
        reply_markup=get_main_keyboard(),
    )


async def show_emails(update: Update) -> None:
    config = load_config()
    user_id = get_user_id(update)
    user_data = get_user_data(config, user_id)
    emails = user_data.get("emails", [])

    if not emails:
        await update.message.reply_text("Почты ещё не добавлены.", reply_markup=get_main_keyboard())
        return

    text = "📮 <b>Ваши почты</b>\n\n"
    for idx, item in enumerate(emails, start=1):
        label = escape_html(item.get("label", "БЕЗ НАЗВАНИЯ"))
        addr = escape_html(mask_email(item["email"]))
        imap_host = escape_html(item["imap"])
        text += (
            f"{idx}. <b>{label}</b>\n"
            f"   📧 {addr}\n"
            f"   🌐 {imap_host}\n\n"
        )

    await update.message.reply_text(text, parse_mode="HTML", reply_markup=get_main_keyboard())


async def show_config(update: Update) -> None:
    config = load_config()
    user_id = get_user_id(update)
    user_data = get_user_data(config, user_id)

    safe_emails = []
    for item in user_data.get("emails", []):
        safe_emails.append(
            {
                "label": item.get("label", ""),
                "email": item["email"],
                "password": "***",
                "imap": item["imap"],
            }
        )

    safe_config = {
        "user_id": user_id,
        "emails": safe_emails,
        "poll_interval": user_data.get("poll_interval", DEFAULT_POLL_INTERVAL),
        "ui_state": user_data.get("ui_state", STATE_IDLE),
    }

    await update.message.reply_text(
        "⚙️ Ваш конфиг:\n" +
        json.dumps(safe_config, indent=2, ensure_ascii=False),
        reply_markup=get_main_keyboard(),
    )


async def test_send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "✅ <b>Тестовое сообщение</b>\n"
        "══════════════════\n"
        "Бот умеет отправлять сообщения в ваш чат.\n\n"
        "Теперь можно нажать <b>▶️ Запуск</b>."
    )

    await send_to_user(context, update.effective_chat.id, text, parse_mode="HTML")
    await update.message.reply_text("Тест отправлен.", reply_markup=get_main_keyboard())


async def show_spam_status(update: Update) -> None:
    config = load_config()
    user_id = get_user_id(update)
    user_data = get_user_data(config, user_id)
    emails = user_data.get("emails", [])

    if not emails:
        await update.message.reply_text("Почты ещё не добавлены.", reply_markup=get_main_keyboard())
        return

    lines = ["🛡 <b>Ваш статус антиспама</b>\n"]

    for mailbox in emails:
        label = mailbox.get("label", "БЕЗ НАЗВАНИЯ")
        email_addr = mailbox.get("email", "unknown")
        key = make_mail_key(user_id, email_addr)

        queue = mail_rate_limit.get(key, deque())
        suppressed = suppressed_counts.get(key, 0)
        last_alert = spam_alert_state.get(key, 0)

        lines.append(
            f"📮 <b>{escape_html(label)}</b>\n"
            f"📧 <code>{escape_html(email_addr)}</code>\n"
            f"• отправок в окне: {len(queue)}/{MAIL_BURST_LIMIT}\n"
            f"• скрыто писем: {suppressed}\n"
            f"• антиспам-уведомление: {'было' if last_alert else 'не было'}\n"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="HTML", reply_markup=get_main_keyboard())


async def start_polling(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global poll_task

    config = load_config()
    user_id = get_user_id(update)
    user_data = get_user_data(config, user_id)

    if not user_data.get("emails"):
        await update.message.reply_text(
            "Сначала добавьте хотя бы одну почту через кнопку ➕ Добавить почту.",
            reply_markup=get_main_keyboard(),
        )
        return

    if poll_task and not poll_task.done():
        await update.message.reply_text("Пересылка уже запущена.", reply_markup=get_main_keyboard())
        return

    poll_task = context.application.create_task(poll_mail_loop(context))
    await update.message.reply_text("🚀 Запускаю пересылку писем...", reply_markup=get_main_keyboard())


async def stop_polling(update: Update) -> None:
    global poll_task

    if poll_task and not poll_task.done():
        poll_task.cancel()
        await update.message.reply_text("⛔ Пересылка остановлена.", reply_markup=get_main_keyboard())
    else:
        await update.message.reply_text("Пересылка не была запущена.", reply_markup=get_main_keyboard())


async def begin_add_email(update: Update) -> None:
    config = load_config()
    user_id = get_user_id(update)
    user_data = get_user_data(config, user_id)
    user_data["ui_state"] = STATE_ADD_LABEL
    user_data["draft_email"] = {}
    save_config(config)

    await update.message.reply_text(
        "➕ Добавление почты\n\nВведите название ящика.\nНапример: ЛИЧНАЯ",
        reply_markup=get_main_keyboard(),
    )


async def begin_remove_email(update: Update) -> None:
    config = load_config()
    user_id = get_user_id(update)
    user_data = get_user_data(config, user_id)
    emails = user_data.get("emails", [])

    if not emails:
        await update.message.reply_text("Почты ещё не добавлены.", reply_markup=get_main_keyboard())
        return

    text = "🗑 Введите номер почты, которую хотите удалить:\n\n"
    for idx, item in enumerate(emails, start=1):
        text += f"{idx}. {item.get('label', 'БЕЗ НАЗВАНИЯ')} — {item['email']}\n"

    user_data["ui_state"] = STATE_REMOVE_SELECT
    save_config(config)

    await update.message.reply_text(text, reply_markup=get_main_keyboard())


async def begin_set_poll(update: Update) -> None:
    config = load_config()
    user_id = get_user_id(update)
    user_data = get_user_data(config, user_id)
    user_data["ui_state"] = STATE_SET_POLL
    save_config(config)

    await update.message.reply_text(
        "⚙️ Введите новый интервал проверки в секундах.\nНапример: 60",
        reply_markup=get_main_keyboard(),
    )


async def cancel_action(update: Update) -> None:
    config = load_config()
    user_id = get_user_id(update)
    user_data = get_user_data(config, user_id)
    user_data["ui_state"] = STATE_IDLE
    user_data["draft_email"] = {}
    save_config(config)

    await update.message.reply_text("Действие отменено.", reply_markup=get_main_keyboard())


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        await update.message.reply_text(ACCESS_DENIED_TEXT)
        return

    text = (update.message.text or "").strip()
    config = load_config()
    user_id = get_user_id(update)
    user_data = get_user_data(config, user_id)
    state = user_data.get("ui_state", STATE_IDLE)

    if text == "➕ Добавить почту":
        await begin_add_email(update)
        return
    if text == "📋 Почты":
        await show_emails(update)
        return
    if text == "🗑 Удалить почту":
        await begin_remove_email(update)
        return
    if text == "▶️ Запуск":
        await start_polling(update, context)
        return
    if text == "⏹ Стоп":
        await stop_polling(update)
        return
    if text == "🧪 Тест":
        await test_send(update, context)
        return
    if text == "🛡 Антиспам":
        await show_spam_status(update)
        return
    if text == "⚙️ Настройки":
        await begin_set_poll(update)
        return
    if text == "❓ Помощь":
        await update.message.reply_text(
            build_help_text(),
            parse_mode="HTML",
            reply_markup=get_main_keyboard(),
        )
        return
    if text == "❌ Отмена":
        await cancel_action(update)
        return

    if state == STATE_ADD_LABEL:
        user_data["draft_email"]["label"] = text
        user_data["ui_state"] = STATE_ADD_EMAIL
        save_config(config)
        await update.message.reply_text("Теперь введите email ящика.")
        return

    if state == STATE_ADD_EMAIL:
        if "@" not in text:
            await update.message.reply_text("Это не похоже на email. Введите email ещё раз.")
            return
        user_data["draft_email"]["email"] = text
        user_data["ui_state"] = STATE_ADD_PASSWORD
        save_config(config)
        await update.message.reply_text(
            "Теперь введите пароль приложения (app password).\n"
            "Для Gmail нужен именно app password."
        )
        return

    if state == STATE_ADD_PASSWORD:
        label = user_data["draft_email"].get("label", "БЕЗ НАЗВАНИЯ")
        email_value = user_data["draft_email"].get("email", "")
        password = text
        imap_host = guess_imap(email_value)

        if not email_value or not imap_host:
            user_data["ui_state"] = STATE_IDLE
            user_data["draft_email"] = {}
            save_config(config)
            await update.message.reply_text(
                "Не удалось добавить почту. Попробуйте снова.",
                reply_markup=get_main_keyboard(),
            )
            return

        for item in user_data["emails"]:
            if item["email"].lower() == email_value.lower():
                user_data["ui_state"] = STATE_IDLE
                user_data["draft_email"] = {}
                save_config(config)
                await update.message.reply_text(
                    "Такая почта уже добавлена.",
                    reply_markup=get_main_keyboard(),
                )
                return

        user_data["emails"].append(
            {
                "label": label,
                "email": email_value,
                "password": password,
                "imap": imap_host,
                "seen_uids": [],
            }
        )
        user_data["ui_state"] = STATE_IDLE
        user_data["draft_email"] = {}
        save_config(config)

        await update.message.reply_text(
            f"✅ Почта добавлена.\n"
            f"Название: {label}\n"
            f"Адрес: {email_value}\n"
            f"IMAP: {imap_host}",
            reply_markup=get_main_keyboard(),
        )
        return

    if state == STATE_REMOVE_SELECT:
        try:
            idx = int(text)
        except ValueError:
            await update.message.reply_text("Введите именно номер почты из списка.")
            return

        emails = user_data.get("emails", [])
        if idx < 1 or idx > len(emails):
            await update.message.reply_text("Такого номера нет. Попробуйте ещё раз.")
            return

        removed = emails.pop(idx - 1)
        user_data["emails"] = emails
        user_data["ui_state"] = STATE_IDLE
        save_config(config)

        await update.message.reply_text(
            f"🗑 Почта удалена: {removed.get('label', removed.get('email', 'unknown'))}",
            reply_markup=get_main_keyboard(),
        )
        return

    if state == STATE_SET_POLL:
        try:
            interval = int(text)
            if interval < 10:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Введите число не меньше 10.")
            return

        user_data["poll_interval"] = interval
        user_data["ui_state"] = STATE_IDLE
        save_config(config)

        await update.message.reply_text(
            f"⏱ Интервал установлен: {interval} сек.",
            reply_markup=get_main_keyboard(),
        )
        return

    await update.message.reply_text(
        "Используйте кнопки ниже.",
        reply_markup=get_main_keyboard(),
    )


async def poll_mail_loop(context: ContextTypes.DEFAULT_TYPE) -> None:
    while True:
        config = load_config()

        for owner_id in OWNER_IDS:
            user_id = str(owner_id)
            user_data = get_user_data(config, user_id)
            interval = int(user_data.get("poll_interval", DEFAULT_POLL_INTERVAL))
            emails = user_data.get("emails", [])

            for mailbox in emails:
                try:
                    label = mailbox.get("label", "БЕЗ НАЗВАНИЯ")
                    email_addr = mailbox["email"]
                    password = mailbox["password"]
                    imap_host = mailbox["imap"]
                    seen_uids = set(mailbox.get("seen_uids", []))

                    with imaplib.IMAP4_SSL(imap_host) as imap:
                        imap.login(email_addr, password)
                        imap.select("INBOX")

                        status, data = imap.search(None, "UNSEEN")
                        if status != "OK":
                            continue

                        changed = False

                        for num in data[0].split():
                            uid = num.decode()

                            if uid in seen_uids:
                                continue

                            status, msg_data = imap.fetch(num, "(RFC822)")
                            if status != "OK" or not msg_data:
                                continue

                            raw_email = msg_data[0][1]
                            message = email.message_from_bytes(raw_email)

                            subject = decode_mime_header(message.get("Subject", "Без темы"))
                            from_header = decode_mime_header(message.get("From", "Неизвестно"))
                            body = extract_plain_text(message)

                            if is_duplicate_mail(user_id, email_addr, subject, from_header, body):
                                key = make_mail_key(user_id, email_addr)
                                suppressed_counts[key] = suppressed_counts.get(key, 0) + 1
                                continue

                            if not can_send_mail_from_box(user_id, email_addr):
                                key = make_mail_key(user_id, email_addr)
                                suppressed_counts[key] = suppressed_counts.get(key, 0) + 1

                                if should_send_spam_alert(user_id, email_addr):
                                    hidden_count = suppressed_counts.get(key, 0)
                                    await send_to_user(
                                        context,
                                        owner_id,
                                        (
                                            f"⚠️ Слишком много писем с ящика "
                                            f"{label}, часть сообщений скрыта.\n"
                                            f"Скрыто писем: {hidden_count}"
                                        ),
                                    )
                                continue

                            text = format_email_message(
                                label=label,
                                mailbox_email=email_addr,
                                from_header=from_header,
                                subject=subject,
                                body=body,
                            )

                            await send_to_user(context, owner_id, text[:4096], parse_mode="HTML")

                            key = make_mail_key(user_id, email_addr)
                            suppressed_counts[key] = 0
                            seen_uids.add(uid)
                            changed = True

                        if changed:
                            mailbox["seen_uids"] = list(seen_uids)[-300:]

                except imaplib.IMAP4.error as e:
                    await send_to_user(
                        context,
                        owner_id,
                        f"❌ Ошибка IMAP для {mailbox.get('label', mailbox.get('email', 'unknown'))}: {e}",
                    )
                except Exception as e:
                    await send_to_user(
                        context,
                        owner_id,
                        f"❌ Ошибка для {mailbox.get('label', mailbox.get('email', 'unknown'))}: {e}",
                    )

        save_config(config)

        # Берём минимальный интервал среди пользователей
        intervals = [
            int(get_user_data(config, str(owner_id)).get("poll_interval", DEFAULT_POLL_INTERVAL))
            for owner_id in OWNER_IDS
        ]
        await asyncio.sleep(min(intervals) if intervals else DEFAULT_POLL_INTERVAL)


def main() -> None:
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_TOKEN environment variable is required")

    application: Application = ApplicationBuilder().token(token).build()

    application.add_handler(CommandHandler("start", start))
    allowed_chat_filters = filters.Chat(chat_id=OWNER_IDS[0]) | filters.Chat(chat_id=OWNER_IDS[1])

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & allowed_chat_filters,
            handle_text,
        )
    )

    application.run_polling()


if __name__ == "__main__":
    main()