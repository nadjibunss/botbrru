"""Telegram command handlers and setup state machine."""
from __future__ import annotations

import html
import json
import logging

from src.config import settings
from src.models.user import DEFAULT_USER_DOC
from src.services.monitor_worker import (
    is_worker_running,
    start_worker,
    stop_worker,
)
from src.services.session_service import utc_now, validate_cookie
from src.utils.crypto import decrypt, encrypt
from src.utils.database import get_db
from src.utils.telegram import delete_message, send_message

logger = logging.getLogger(__name__)

STATE_AWAITING_COOKIE = "awaiting_cookie"
STATE_AWAITING_BOT_TOKEN = "awaiting_bot_token"


async def handle_update(update: dict) -> None:
    """Route one Telegram update."""
    message = update.get("message")

    if not message:
        return

    text = (message.get("text") or "").strip()
    sender = message.get("from") or {}
    chat = message.get("chat") or {}

    if not text or not sender or not chat:
        return

    chat_id = chat["id"]
    user_id = sender["id"]
    message_id = message["message_id"]
    telegram_username = sender.get("username", "")

    # Credentials hanya lewat private chat
    if chat.get("type") != "private":
        if text.startswith("/setcredentials") or text.startswith("/setbot"):
            await send_message(
                chat_id,
                "❌ Jalankan perintah kredensial melalui private chat dengan bot.",
            )
        return

    user = await ensure_user(user_id, telegram_username)

    if not user:
        await send_message(chat_id, "❌ Database tidak tersedia.")
        return

    if text.startswith("/"):
        await route_command(
            chat_id,
            user_id,
            telegram_username,
            text,
        )
        return

    await route_setup_input(
        chat_id=chat_id,
        user_id=user_id,
        text=text,
        message_id=message_id,
        user=user,
    )


async def ensure_user(
    telegram_id: int,
    telegram_username: str,
) -> dict | None:
    """Get or create a user document."""
    try:
        db = await get_db()
        user = await db.users.find_one({"telegram_id": telegram_id})

        if user:
            if telegram_username != user.get("telegram_username"):
                await db.users.update_one(
                    {"telegram_id": telegram_id},
                    {"$set": {"telegram_username": telegram_username}},
                )
                user["telegram_username"] = telegram_username
            return user

        document = DEFAULT_USER_DOC.copy()
        document.update(
            {
                "telegram_id": telegram_id,
                "telegram_username": telegram_username,
                "keywords": settings.default_keywords,
                "area": settings.default_area,
            }
        )

        await db.users.insert_one(document)
        return document

    except Exception:
        logger.exception("Unable to create user")
        return None


async def set_state(
    telegram_id: int,
    state: str | None,
    payload: dict | None = None,
) -> None:
    """Save an ongoing setup state."""
    db = await get_db()

    await db.users.update_one(
        {"telegram_id": telegram_id},
        {
            "$set": {
                "setup_state": state,
                "setup_payload": payload or {},
            }
        },
    )


async def clear_state(telegram_id: int) -> None:
    """Clear current setup state."""
    await set_state(telegram_id, None, {})


async def route_command(
    chat_id: int,
    user_id: int,
    telegram_username: str,
    text: str,
) -> None:
    """Route slash commands."""
    parts = text.split(maxsplit=1)
    command = parts[0].lower().split("@")[0]
    args = parts[1].strip() if len(parts) == 2 else ""

    if command == "/cancel":
        await clear_state(user_id)
        await send_message(chat_id, "Setup dibatalkan.")
        return

    if command == "/start":
        await command_start(chat_id)
        return

    if command == "/setcredentials":
        await set_state(user_id, STATE_AWAITING_COOKIE)
        await send_message(
            chat_id,
            "Kirim <b>cookie sesi</b> Anda sekarang.\n\n"
            "Bot tidak meminta password atau OTP.\n"
            "Ketik /cancel untuk membatalkan.",
        )
        return

    if command == "/setbot":
        # Silent mode: tanpa pesan konfirmasi yang menyebut token
        if args:
            await save_custom_bot_token(user_id, args, chat_id, delete_mid=None)
            return

        await set_state(user_id, STATE_AWAITING_BOT_TOKEN)
        # Sengaja tanpa pesan — input silent
        return

    if command == "/setfingerprint":
        await command_set_fingerprint(chat_id, user_id, args)
        return

    if command == "/setgroup":
        await command_set_group(chat_id, user_id, args)
        return

    if command == "/setkeywords":
        await command_set_keywords(chat_id, user_id, args)
        return

    if command == "/setarea":
        await command_set_area(chat_id, user_id, args)
        return

    if command == "/start_monitor":
        await command_start_monitor(chat_id, user_id)
        return

    if command == "/stop_monitor":
        await command_stop_monitor(chat_id, user_id)
        return

    if command == "/status":
        await command_status(chat_id, user_id)
        return

    if command == "/reset":
        await command_reset(chat_id, user_id)
        return


async def route_setup_input(
    chat_id: int,
    user_id: int,
    text: str,
    message_id: int,
    user: dict,
) -> None:
    """Process non-command text based on active setup state."""
    state = user.get("setup_state")

    if state == STATE_AWAITING_COOKIE:
        await receive_cookie(chat_id, user_id, text, message_id)
        return

    if state == STATE_AWAITING_BOT_TOKEN:
        await save_custom_bot_token(user_id, text, chat_id, delete_mid=message_id)
        return


async def receive_cookie(
    chat_id: int,
    user_id: int,
    cookie: str,
    message_id: int,
) -> None:
    """Validate and save a session cookie."""
    result = await validate_cookie(cookie)

    # Hapus pesan cookie dari chat history (best effort)
    await delete_message(chat_id, message_id)

    if not result.valid:
        reason = html.escape(result.reason or "Cookie tidak valid")
        await send_message(
            chat_id,
            f"❌ {reason}\n\nKirim cookie baru atau ketik /cancel.",
        )
        return

    now = utc_now()
    db = await get_db()

    await db.users.update_one(
        {"telegram_id": user_id},
        {
            "$set": {
                "cookie_enc": encrypt(cookie),
                "cookie_verified_at": now,
                "account_username": result.account_username,
                "setup_state": None,
                "setup_payload": {},
            }
        },
    )
    await send_message(
        chat_id,
        f"✅ <b>Login Shopee berhasil!</b>\n\n"
        f"👤 Username: <b>{html.escape(result.account_username or '-')}</b>\n"
        f"🆔 Akun terverifikasi dan cookie tersimpan.\n\n"
        f"Gunakan /start_monitor untuk mulai monitoring.\n"
        f"Jika sesi berakhir, jalankan /setcredentials kembali.",
    )


async def save_custom_bot_token(
    user_id: int,
    token: str,
    chat_id: int | None = None,
    delete_mid: int | None = None,
) -> None:
    """
    Persist custom bot token silently.
    No confirmation message that echoes the token.
    """
    token = token.strip()

    if delete_mid is not None and chat_id is not None:
        await delete_message(chat_id, delete_mid)

    if not is_token_shape_valid(token):
        # Silent fail — jangan sebut token di balasan
        return

    db = await get_db()

    await db.users.update_one(
        {"telegram_id": user_id},
        {
            "$set": {
                "custom_bot_token_enc": encrypt(token),
                "setup_state": None,
                "setup_payload": {},
            }
        },
    )
    # Tidak ada send_message konfirmasi (sesuai spesifikasi silent)


def is_token_shape_valid(token: str) -> bool:
    """Minimal token format check without logging the token."""
    left, separator, right = token.partition(":")

    return (
        bool(separator)
        and left.isdigit()
        and 8 <= len(left) <= 11
        and len(right) >= 30
    )


async def command_start(chat_id: int) -> None:
    """Send bot help."""
    text = (
        "🤖 <b>Shopee Instant Stock Monitor</b>\n\n"
        "<b>Setup aman:</b>\n"
        "1. /setcredentials → kirim cookie sesi\n"
        "2. /setfingerprint &lt;JSON&gt; (opsional)\n"
        "3. /setbot &lt;token&gt; (opsional, silent)\n"
        "4. /setgroup &lt;chat_id&gt; (opsional)\n"
        "5. /setkeywords kata1 | kata2 | kata3\n"
        "6. /setarea Kab. Bekasi\n"
        "7. /start_monitor\n\n"
        "Bot <b>tidak</b> menyimpan password atau OTP.\n"
        "/cancel membatalkan setup aktif."
    )
    await send_message(chat_id, text)


async def command_set_fingerprint(
    chat_id: int,
    user_id: int,
    args: str,
) -> None:
    """Store optional non-secret fingerprint JSON."""
    if not args:
        await send_message(
            chat_id,
            "❌ Format: /setfingerprint &lt;JSON object&gt;",
        )
        return

    try:
        fingerprint = json.loads(args)
    except json.JSONDecodeError:
        await send_message(chat_id, "❌ Fingerprint harus JSON valid.")
        return

    if not isinstance(fingerprint, dict):
        await send_message(chat_id, "❌ Fingerprint harus JSON object.")
        return

    db = await get_db()

    await db.users.update_one(
        {"telegram_id": user_id},
        {"$set": {"fingerprint": fingerprint}},
    )

    await send_message(chat_id, "✅ Fingerprint disimpan.")


async def command_set_group(
    chat_id: int,
    user_id: int,
    args: str,
) -> None:
    """Set report destination group/chat ID."""
    try:
        group_chat_id = int(args)
    except ValueError:
        await send_message(chat_id, "❌ Format: /setgroup &lt;chat_id_angka&gt;")
        return

    db = await get_db()

    await db.users.update_one(
        {"telegram_id": user_id},
        {"$set": {"group_chat_id": group_chat_id}},
    )

    await send_message(chat_id, "✅ Group tujuan disimpan.")


async def command_set_keywords(
    chat_id: int,
    user_id: int,
    args: str,
) -> None:
    """Set keyword rotation."""
    keywords = [value.strip() for value in args.split("|") if value.strip()]

    if not keywords:
        await send_message(
            chat_id,
            "❌ Format: /setkeywords kata1 | kata2 | kata3",
        )
        return

    db = await get_db()

    await db.users.update_one(
        {"telegram_id": user_id},
        {"$set": {"keywords": "|".join(keywords)}},
    )

    await send_message(
        chat_id,
        f"✅ Keywords: <b>{html.escape(', '.join(keywords))}</b>",
    )


async def command_set_area(
    chat_id: int,
    user_id: int,
    args: str,
) -> None:
    """Set monitoring area."""
    area = args.strip()

    if not area:
        await send_message(chat_id, "❌ Format: /setarea &lt;nama area&gt;")
        return

    db = await get_db()

    await db.users.update_one(
        {"telegram_id": user_id},
        {"$set": {"area": area}},
    )

    await send_message(chat_id, f"✅ Area: <b>{html.escape(area)}</b>")


async def command_start_monitor(
    chat_id: int,
    user_id: int,
) -> None:
    """Enable worker after cookie exists."""
    db = await get_db()
    user = await db.users.find_one({"telegram_id": user_id})

    if not user or not user.get("cookie_enc"):
        await send_message(
            chat_id,
            "❌ Jalankan /setcredentials sebelum memulai monitoring.",
        )
        return

    await db.users.update_one(
        {"telegram_id": user_id},
        {"$set": {"monitoring_active": True}},
    )

    if await start_worker(user_id):
        await send_message(chat_id, "▶️ Monitoring dimulai.")
    else:
        await send_message(chat_id, "ℹ️ Monitoring sudah berjalan.")


async def command_stop_monitor(
    chat_id: int,
    user_id: int,
) -> None:
    """Disable monitoring worker."""
    await stop_worker(user_id)

    db = await get_db()

    await db.users.update_one(
        {"telegram_id": user_id},
        {"$set": {"monitoring_active": False}},
    )

    await send_message(chat_id, "⏹️ Monitoring dihentikan.")


async def command_status(
    chat_id: int,
    user_id: int,
) -> None:
    """Show non-secret configuration status."""
    db = await get_db()
    user = await db.users.find_one({"telegram_id": user_id}) or {}

    active = is_worker_running(user_id)
    cookie_status = "✅" if user.get("cookie_enc") else "❌"
    bot_status = "✅" if user.get("custom_bot_token_enc") else "➖"
    group_status = "✅" if user.get("group_chat_id") else "➖"

    account = html.escape(user.get("account_username") or "-")
    area = html.escape(user.get("area") or settings.default_area)
    keywords = html.escape(user.get("keywords") or settings.default_keywords)

    status_icon = "🟢" if active else "🔴"
    status_text = "Aktif" if active else "Tidak aktif"
    text = (
        f"{status_icon} <b>Status:</b> "
        f"{status_text}\n\n"
        f"<b>Akun:</b> {account}\n"
        f"<b>Cookie:</b> {cookie_status}\n"
        f"<b>Custom bot:</b> {bot_status}\n"
        f"<b>Group:</b> {group_status}\n"
        f"<b>Area:</b> {area}\n"
        f"<b>Keywords:</b> {keywords}"
    )

    await send_message(chat_id, text)


async def command_reset(
    chat_id: int,
    user_id: int,
) -> None:
    """Clear seen product IDs."""
    db = await get_db()

    await db.users.update_one(
        {"telegram_id": user_id},
        {"$set": {"checked_items": []}},
    )

    await send_message(chat_id, "🔄 Daftar item yang sudah diperiksa direset.")


def resolve_custom_bot_token(user: dict) -> str | None:
    """Decrypt custom bot token only at the point of use."""
    encrypted = user.get("custom_bot_token_enc")

    if not encrypted:
        return None

    try:
        return decrypt(encrypted)
    except ValueError:
        logger.warning(
            "Invalid custom bot token ciphertext for user %s",
            user.get("telegram_id"),
        )
        return None
