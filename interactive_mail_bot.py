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
OWNER_CHAT_ID = 5127424995
MAIL_BURST_LIMIT = 5
MAIL_BURST_WINDOW = 60
DEDUP_TTL_SECONDS = 3600
SPAM_ALERT_COOLDOWN = 300

poll_task = None

mail_rate_limit: dict[str, deque] = {}
recent_mail_fingerprints: dict[str, float] = {}
spam_alert_state: dict[str, float] = {}
suppressed_counts: dict[str, int] = {}

# Состояния пошагового диалога
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
    return update.effective_chat is not None and update.effective_chat.id == OWNER_CHAT_ID


def guess_imap(email_value: str) -> str:
    if "@" not in email_value:
        return ""
    domain = email_value.split("@", 1)[1].lower().strip()
    return IMAP_BY_DOMAIN.get(domain, f"imap.{domain}")


def load_config() -> dict[str, Any]:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if "emails" not in data:
                    data["emails"] = []
                if "chat_id" not in data:
                    data["chat_id"] = OWNER_CHAT_ID
                if "poll_interval" not in data:
                    data["poll_interval"] = DEFAULT_POLL_INTERVAL
                if "ui_state" not in data:
                    data["ui_state"] = STATE_IDLE
                if "draft_email" not in data:
                    data["draft_email"] = {}
                return data
        except Exception:
            pass
    return {
        "emails": [],
        "chat_id": OWNER_CHAT_ID,
        "poll_interval": DEFAULT_POLL_INTERVAL,
        "ui_state": STATE_IDLE,
        "draft_email": {},
    }


def save_config(config: dict[str, Any]) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


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


def is_duplicate_mail(mailbox_email: str, subject: str, from_header: str, body: str) -> bool:
    cleanup_old_fingerprints()
    short_body = (body or "")[:200].strip()
    fingerprint = f"{mailbox_email}|{subject}|{from_header}|{short_body}"
    now = time.time()

    if fingerprint in recent_mail_fingerprints:
        return True

    recent_mail_fingerprints[fingerprint] = now
    return False


def can_send_mail_from_box(mailbox_email: str) -> bool:
    now = time.time()
    if mailbox_email not in mail_rate_limit:
        mail_rate_limit[mailbox_email] = deque()

    q = mail_rate_limit[mailbox_email]

    while q and now - q[0] > MAIL_BURST_WINDOW:
        q.popleft()

    if len(q) >= MAIL_BURST_LIMIT:
        return False

    q.append(now)
    return True


def should_send_spam_alert(mailbox_email: str) -> bool:
    now = time.time()
    last_alert = spam_alert_state.get(mailbox_email, 0)
    if now - last_alert < SPAM_ALERT_COOLDOWN:
        return False
    spam_alert_state[mailbox_email] = now
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


def build_start_text() -> str:
    return (
        "👋 <b>Привет! Я бот для пересылки почты в Telegram.</b>\n\n"
        "✅ Уведомления будут приходить только вам.\n"
        f"✅ Ваш chat_id уже привязан: <code>{OWNER_CHAT_ID}</code>\n\n"
        "<b>Как пользоваться:</b>\n"
        "1. Нажмите <b>➕ Добавить почту</b>\n"
        "2. По шагам введите название, email и app password\n"
        "3. Нажмите <b>🧪 Тест</b>\n"
        "4. Нажмите <b>▶️ Запуск</b>\n\n"
        "Все действия теперь через кнопки."
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
        "Показывает все подключённые ящики.\n\n"
        "🗑 <b>Удалить почту</b>\n"
        "Показывает список, какую почту удалить.\n\n"
        "🧪 <b>Тест</b>\n"
        "Проверка, что бот умеет отправлять сообщения вам.\n\n"
        "▶️ <b>Запуск</b>\n"
        "Начинает проверять почту.\n\n"
        "⏹ <b>Стоп</b>\n"
        "Останавливает пересылку.\n\n"
        "🛡 <b>Антиспам</b>\n"
        "Показывает состояние защиты от спама.\n\n"
        "⚙️ <b>Настройки</b>\n"
        "Показывает конфиг и текущий интервал проверки."
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        await update.message.reply_text("⛔ Этот бот доступен только владельцу.")
        return

    config = load_config()
    config["chat_id"] = OWNER_CHAT_ID
    config["ui_state"] = STATE_IDLE
    config["draft_email"] = {}
    save_config(config)

    await update.message.reply_text(
        build_start_text(),
        parse_mode="HTML",
        reply_markup=get_main_keyboard(),
    )


async def show_emails(update: Update) -> None:
    config = load_config()
    emails = config.get("emails", [])

    if not emails:
        await update.message.reply_text("Почты ещё не добавлены.", reply_markup=get_main_keyboard())
        return

    text = "📮 <b>Добавленные почты</b>\n\n"
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

    safe_emails = []
    for item in config.get("emails", []):
        safe_emails.append(
            {
                "label": item.get("label", ""),
                "email": item["email"],
                "password": "***",
                "imap": item["imap"],
            }
        )

    safe_config = {
        "emails": safe_emails,
        "chat_id": OWNER_CHAT_ID,
        "poll_interval": config.get("poll_interval", DEFAULT_POLL_INTERVAL),
        "ui_state": config.get("ui_state", STATE_IDLE),
    }

    await update.message.reply_text(
        "⚙️ Текущая конфигурация:\n" +
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

    try:
        await context.bot.send_message(
            chat_id=OWNER_CHAT_ID,
            text=text,
            parse_mode="HTML",
        )
        await update.message.reply_text("Тест отправлен.", reply_markup=get_main_keyboard())
    except Exception as e:
        await update.message.reply_text(f"Ошибка теста: {e}", reply_markup=get_main_keyboard())


async def show_spam_status(update: Update) -> None:
    config = load_config()
    emails = config.get("emails", [])

    if not emails:
        await update.message.reply_text("Почты ещё не добавлены.", reply_markup=get_main_keyboard())
        return

    lines = ["🛡 <b>Статус антиспама</b>\n"]

    for mailbox in emails:
        label = mailbox.get("label", "БЕЗ НАЗВАНИЯ")
        email_addr = mailbox.get("email", "unknown")

        queue = mail_rate_limit.get(email_addr, deque())
        suppressed = suppressed_counts.get(email_addr, 0)
        last_alert = spam_alert_state.get(email_addr, 0)

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
    if not config.get("emails"):
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
    config["ui_state"] = STATE_ADD_LABEL
    config["draft_email"] = {}
    save_config(config)

    await update.message.reply_text(
        "➕ Добавление почты\n\nВведите название ящика.\nНапример: ЛИЧНАЯ",
        reply_markup=get_main_keyboard(),
    )


async def begin_remove_email(update: Update) -> None:
    config = load_config()
    emails = config.get("emails", [])

    if not emails:
        await update.message.reply_text("Почты ещё не добавлены.", reply_markup=get_main_keyboard())
        return

    text = "🗑 Введите номер почты, которую хотите удалить:\n\n"
    for idx, item in enumerate(emails, start=1):
        text += f"{idx}. {item.get('label', 'БЕЗ НАЗВАНИЯ')} — {item['email']}\n"

    config["ui_state"] = STATE_REMOVE_SELECT
    save_config(config)

    await update.message.reply_text(text, reply_markup=get_main_keyboard())


async def begin_set_poll(update: Update) -> None:
    config = load_config()
    config["ui_state"] = STATE_SET_POLL
    save_config(config)

    await update.message.reply_text(
        "⚙️ Введите новый интервал проверки в секундах.\nНапример: 60",
        reply_markup=get_main_keyboard(),
    )


async def cancel_action(update: Update) -> None:
    config = load_config()
    config["ui_state"] = STATE_IDLE
    config["draft_email"] = {}
    save_config(config)

    await update.message.reply_text("Действие отменено.", reply_markup=get_main_keyboard())


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        await update.message.reply_text("⛔ Этот бот доступен только владельцу.")
        return

    text = (update.message.text or "").strip()
    config = load_config()
    state = config.get("ui_state", STATE_IDLE)

    # Кнопки главного меню
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

    # Пошаговый диалог
    if state == STATE_ADD_LABEL:
        config["draft_email"]["label"] = text
        config["ui_state"] = STATE_ADD_EMAIL
        save_config(config)
        await update.message.reply_text("Теперь введите email ящика.")
        return

    if state == STATE_ADD_EMAIL:
        if "@" not in text:
            await update.message.reply_text("Это не похоже на email. Введите email ещё раз.")
            return
        config["draft_email"]["email"] = text
        config["ui_state"] = STATE_ADD_PASSWORD
        save_config(config)
        await update.message.reply_text(
            "Теперь введите пароль приложения (app password).\n"
            "Для Gmail нужен именно app password."
        )
        return

    if state == STATE_ADD_PASSWORD:
        label = config["draft_email"].get("label", "БЕЗ НАЗВАНИЯ")
        email_value = config["draft_email"].get("email", "")
        password = text
        imap_host = guess_imap(email_value)

        if not email_value or not imap_host:
            config["ui_state"] = STATE_IDLE
            config["draft_email"] = {}
            save_config(config)
            await update.message.reply_text(
                "Не удалось добавить почту. Попробуйте снова.",
                reply_markup=get_main_keyboard(),
            )
            return

        for item in config["emails"]:
            if item["email"].lower() == email_value.lower():
                config["ui_state"] = STATE_IDLE
                config["draft_email"] = {}
                save_config(config)
                await update.message.reply_text(
                    "Такая почта уже добавлена.",
                    reply_markup=get_main_keyboard(),
                )
                return

        config["emails"].append(
            {
                "label": label,
                "email": email_value,
                "password": password,
                "imap": imap_host,
                "seen_uids": [],
            }
        )
        config["ui_state"] = STATE_IDLE
        config["draft_email"] = {}
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

        emails = config.get("emails", [])
        if idx < 1 or idx > len(emails):
            await update.message.reply_text("Такого номера нет. Попробуйте ещё раз.")
            return

        removed = emails.pop(idx - 1)
        config["emails"] = emails
        config["ui_state"] = STATE_IDLE
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

        config["poll_interval"] = interval
        config["ui_state"] = STATE_IDLE
        save_config(config)

        await update.message.reply_text(
            f"⏱ Интервал установлен: {interval} сек.",
            reply_markup=get_main_keyboard(),
        )
        return

    # Если просто текст вне сценария
    await update.message.reply_text(
        "Используйте кнопки ниже.",
        reply_markup=get_main_keyboard(),
    )


async def poll_mail_loop(context: ContextTypes.DEFAULT_TYPE) -> None:
    while True:
        config = load_config()
        chat_id = OWNER_CHAT_ID
        interval = int(config.get("poll_interval", DEFAULT_POLL_INTERVAL))
        emails = config.get("emails", [])

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

                        if is_duplicate_mail(email_addr, subject, from_header, body):
                            suppressed_counts[email_addr] = suppressed_counts.get(email_addr, 0) + 1
                            continue

                        if not can_send_mail_from_box(email_addr):
                            suppressed_counts[email_addr] = suppressed_counts.get(email_addr, 0) + 1

                            if should_send_spam_alert(email_addr):
                                hidden_count = suppressed_counts.get(email_addr, 0)
                                await context.bot.send_message(
                                    chat_id=chat_id,
                                    text=(
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

                        try:
                            await context.bot.send_message(
                                chat_id=chat_id,
                                text=text[:4096],
                                parse_mode="HTML",
                            )
                        except Exception:
                            fallback_text = (
                                f"НОВОЕ ПИСЬМО\n"
                                f"Ящик: {label}\n"
                                f"Адрес: {email_addr}\n"
                                f"От: {from_header}\n"
                                f"Тема: {subject}\n\n"
                                f"{body or '[пустое сообщение]'}"
                            )
                            await context.bot.send_message(
                                chat_id=chat_id,
                                text=fallback_text[:4096],
                            )

                        suppressed_counts[email_addr] = 0
                        seen_uids.add(uid)
                        changed = True

                    if changed:
                        mailbox["seen_uids"] = list(seen_uids)[-300:]

            except imaplib.IMAP4.error as e:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"❌ Ошибка IMAP для {mailbox.get('label', mailbox.get('email', 'unknown'))}: {e}",
                )
            except Exception as e:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"❌ Ошибка для {mailbox.get('label', mailbox.get('email', 'unknown'))}: {e}",
                )

        save_config(config)
        await asyncio.sleep(interval)


def main() -> None:
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_TOKEN environment variable is required")

    application: Application = ApplicationBuilder().token(token).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.Chat(chat_id=OWNER_CHAT_ID),
            handle_text,
        )
    )

    application.run_polling()


if __name__ == "__main__":
    main()