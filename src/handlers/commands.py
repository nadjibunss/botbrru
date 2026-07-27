"""Telegram command handlers and setup state machine."""
from __future__ import annotations

import copy
import html
import json
import logging

from src.config import settings
from src.models.user import DEFAULT_USER_DOC
from src.services.monitor_worker import (
    is_worker_running,
    proxy_pool_size,
    start_worker,
    stop_worker,
)
from src.services.session_service import utc_now, validate_cookie
from src.utils.crypto import decrypt, encrypt
from src.utils.database import get_db
from src.utils.telegram import delete_message, send_message

logger = logging.getLogger(__name__)

STATE_AWAITING_COOKIE = "awaiting_cookie"
STATE_AWAITING_RISKTOKEN = "awaiting_risktoken"
STATE_AWAITING_BOT_TOKEN = "awaiting_bot_token"
STATE_AWAITING_FINGERPRINT = "awaiting_fingerprint"


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
                "\u274c Jalankan perintah kredensial melalui private chat dengan bot.",
            )
        return

    user = await ensure_user(user_id, telegram_username)

    if not user:
        await send_message(chat_id, "\u274c Database tidak tersedia.")
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

        document = copy.deepcopy(DEFAULT_USER_DOC)
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
        # Sengaja tanpa pesan \u2014 input silent
        return

    if command == "/skip":
        await command_skip_risktoken(chat_id, user_id)
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

    if state == STATE_AWAITING_RISKTOKEN:
        await receive_risktoken(chat_id, user_id, text, message_id)
        return

    if state == STATE_AWAITING_BOT_TOKEN:
        await save_custom_bot_token(user_id, text, chat_id, delete_mid=message_id)
        return

    if state == STATE_AWAITING_FINGERPRINT:
        await receive_fingerprint(chat_id, user_id, text, message_id, user)
        return


async def receive_cookie(
    chat_id: int,
    user_id: int,
    cookie: str,
    message_id: int,
) -> None:
    """Validate and save a session cookie."""
    # Hapus pesan cookie dari chat history (best effort)
    await delete_message(chat_id, message_id)

    result = await validate_cookie(cookie)

    if result.requires_captcha:
        # Save pending cookie encrypted, ask for risktoken
        pending_cookie_enc = encrypt(cookie)
        await set_state(
            user_id,
            STATE_AWAITING_FINGERPRINT,
            {"pending_cookie_enc": pending_cookie_enc},
        )
        await send_message(
            chat_id,
            "\u26a1 Fingerprint/CAPTCHA Shopee terdeteksi!\n\n"
            "Untuk melanjutkan, kirim risktoken kamu.\n"
            "Format: dGAOpcjLx9vrTGoRYEbfew==|...|08|1\n\n"
            "Cara dapat risktoken:\n"
            "- Buka shopee.co.id di browser\n"
            "- Login atau refresh halaman\n"
            "- F12 \u2192 Network \u2192 cari request ke /get_profile\n"
            "- Lihat header x-sz-secsdk-token atau cookie RiskSessionID\n\n"
            "Ketik /cancel untuk batal.",
        )
        return

    if not result.valid:
        reason = html.escape(result.reason or "Cookie tidak valid")
        await send_message(
            chat_id,
            f"\u274c {reason}\n\nKirim cookie baru atau ketik /cancel.",
        )
        return

    now = utc_now()
    db = await get_db()
    phone_info = "\ud83d\udcf1 Ada nomor HP" if result.has_phone else "\ud83d\udcf5 No Phone"

    # Step 1 done: cookie valid. Save and ask for risktoken.
    await db.users.update_one(
        {"telegram_id": user_id},
        {
            "$set": {
                "cookie_enc": encrypt(cookie),
                "risktoken_enc": None,
                "cookie_verified_at": now,
                "account_username": result.account_username,
                "account_has_phone": result.has_phone,
                "setup_state": STATE_AWAITING_RISKTOKEN,
                "setup_payload": {},
            }
        },
    )
    await send_message(
        chat_id,
        f"\u2705 Cookie valid! Akun: <b>{html.escape(result.account_username or '-')}</b> {phone_info}\n\n"
        f"\ud83d\udd11 Sekarang kirim <b>risktoken</b> kamu.\n\n"
        f"Risktoken = device fingerprint agar request ke Shopee terlihat seperti browser asli.\n"
        f"Format: <code>base64==|...|08|1</code>\n\n"
        f"Ketik /skip kalau tidak punya (monitoring mungkin kena anti-bot).",
    )


async def receive_risktoken(
    chat_id: int,
    user_id: int,
    text: str,
    message_id: int,
) -> None:
    """Step 2 of setup: receive and save risktoken."""
    await delete_message(chat_id, message_id)

    risktoken = text.strip()
    db = await get_db()
    await db.users.update_one(
        {"telegram_id": user_id},
        {"$set": {
            "risktoken_enc": encrypt(risktoken),
            "setup_state": None,
            "setup_payload": {},
        }},
    )
    await send_message(
        chat_id,
        "\u2705 Setup selesai! Cookie + risktoken tersimpan.\n\n"
        "Gunakan /start_monitor untuk mulai monitoring.",
    )


async def command_skip_risktoken(chat_id: int, user_id: int) -> None:
    """Skip risktoken step, finish setup without it."""
    db = await get_db()
    user = await db.users.find_one({"telegram_id": user_id})
    if not user or user.get("setup_state") != STATE_AWAITING_RISKTOKEN:
        await send_message(chat_id, "\u2139\ufe0f Tidak ada setup yang sedang berjalan.")
        return
    await db.users.update_one(
        {"telegram_id": user_id},
        {"$set": {"setup_state": None, "setup_payload": {}}},
    )
    await send_message(
        chat_id,
        "\u23e9 Risktoken dilewati. Cookie tersimpan tanpa fingerprint.\n\n"
        "Monitoring mungkin kena anti-bot Shopee. Gunakan /start_monitor untuk mulai.",
    )


async def receive_fingerprint(
    chat_id: int,
    user_id: int,
    risktoken: str,
    message_id: int,
    user: dict,
) -> None:
    """Handle risktoken submission from user."""
    await delete_message(chat_id, message_id)

    # Get pending cookie from setup_payload
    setup_payload = user.get("setup_payload") or {}
    pending_cookie_enc = setup_payload.get("pending_cookie_enc")

    if not pending_cookie_enc:
        await send_message(chat_id, "\u274c Sesi setup expired. Jalankan /setcredentials lagi.")
        return

    try:
        cookie = decrypt(pending_cookie_enc)
    except ValueError:
        await send_message(chat_id, "\u274c Data cookie rusak. Jalankan /setcredentials lagi.")
        return

    await send_message(chat_id, "\ud83d\udd0d Memverifikasi fingerprint...")
    result = await validate_cookie(cookie, risktoken=risktoken.strip())

    if result.requires_captcha:
        await send_message(chat_id, "\u274c Fingerprint tidak valid. Coba risktoken lain atau /setcredentials ulang.")
        return

    if not result.valid:
        await send_message(chat_id, f"\u274c {result.reason or 'Cookie tidak valid'}. Jalankan /setcredentials lagi.")
        return

    # Success \u2014 save cookie + clear state
    now = utc_now()
    db = await get_db()
    phone_info = "\ud83d\udcf1 Ada nomor HP" if result.has_phone else "\ud83d\udcf5 No Phone"

    await db.users.update_one(
        {"telegram_id": user_id},
        {"$set": {
            "cookie_enc": pending_cookie_enc,
            "risktoken_enc": encrypt(risktoken.strip()),
            "cookie_verified_at": now,
            "account_username": result.account_username,
            "account_has_phone": result.has_phone,
            "setup_state": None,
            "setup_payload": {},
        }}
    )

    await send_message(
        chat_id,
        f"\u2705 Login Shopee berhasil!\n\n"
        f"\ud83d\udc64 Username: {html.escape(result.account_username or '-')}\n"
        f"{phone_info}\n\n"
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
        # Silent fail \u2014 jangan sebut token di balasan
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
        and 8 <= len(left) <= 16
        and len(right) >= 30
    )


async def command_start(chat_id: int) -> None:
    """Send bot help."""
    text = (
        "\ud83e\udd16 <b>Shopee Instant Stock Monitor</b>\n\n"
        "<b>Setup (lewat private chat):</b>\n"
        "1. /setcredentials \u2192 kirim cookie sesi Shopee\n"
        "2. /setfingerprint &lt;risktoken&gt; \u2192 opsional, agar lolos anti-bot\n"
        "   (atau /skip saat diminta risktoken)\n"
        "3. /setbot &lt;token&gt; \u2192 opsional, notif lewat bot sendiri (silent)\n"
        "4. /setgroup &lt;chat_id&gt; \u2192 opsional, kirim notif ke grup\n"
        "5. /setkeywords kata1 | kata2 | kata3\n"
        "6. /setarea &lt;nama&gt; \u2192 opsional, catatan area (belum memfilter hasil)\n"
        "7. /start_monitor \u2192 mulai memantau\n\n"
        "<b>Kontrol:</b>\n"
        "/status \u2192 lihat status &amp; konfigurasi\n"
        "/stop_monitor \u2192 berhenti memantau\n"
        "/reset \u2192 kosongkan daftar item yang sudah dicek\n"
        "/cancel \u2192 batalkan setup yang sedang berjalan\n\n"
        "Bot <b>tidak</b> menyimpan password atau OTP."
    )
    await send_message(chat_id, text)


async def command_set_fingerprint(
    chat_id: int,
    user_id: int,
    args: str,
) -> None:
    """Store risktoken for Shopee requests (pipe-separated format)."""
    risktoken = args.strip()
    if not risktoken:
        await send_message(
            chat_id,
            "❌ Format: /setfingerprint &lt;risktoken&gt;\n\n"
            "Contoh: /setfingerprint dGAOpcjLx9vrTGoRY...|08|1\n\n"
            "Risktoken ini dipakai di semua request ke Shopee untuk bypass anti-bot.",
        )
        return

    db = await get_db()
    await db.users.update_one(
        {"telegram_id": user_id},
        {"$set": {"risktoken_enc": encrypt(risktoken)}},
    )
    await send_message(chat_id, "✅ Risktoken disimpan.")

async def command_set_group(
    chat_id: int,
    user_id: int,
    args: str,
) -> None:
    """Set report destination group/chat ID."""
    try:
        group_chat_id = int(args)
    except ValueError:
        await send_message(chat_id, "\u274c Format: /setgroup &lt;chat_id_angka&gt;")
        return

    db = await get_db()

    await db.users.update_one(
        {"telegram_id": user_id},
        {"$set": {"group_chat_id": group_chat_id}},
    )

    await send_message(chat_id, "\u2705 Group tujuan disimpan.")


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
            "\u274c Format: /setkeywords kata1 | kata2 | kata3",
        )
        return

    db = await get_db()

    await db.users.update_one(
        {"telegram_id": user_id},
        {"$set": {"keywords": "|".join(keywords)}},
    )

    await send_message(
        chat_id,
        f"\u2705 Keywords: <b>{html.escape(', '.join(keywords))}</b>",
    )


async def command_set_area(
    chat_id: int,
    user_id: int,
    args: str,
) -> None:
    """Set monitoring area."""
    area = args.strip()

    if not area:
        await send_message(chat_id, "\u274c Format: /setarea &lt;nama area&gt;")
        return

    db = await get_db()

    await db.users.update_one(
        {"telegram_id": user_id},
        {"$set": {"area": area}},
    )

    await send_message(chat_id, f"\u2705 Area: <b>{html.escape(area)}</b>")


async def command_start_monitor(
    chat_id: int,
    user_id: int,
) -> None:
    """Enable worker after cookie exists and session is valid."""
    db = await get_db()
    user = await db.users.find_one({"telegram_id": user_id})

    if not user or not user.get("cookie_enc"):
        await send_message(
            chat_id,
            "\u274c Jalankan /setcredentials sebelum memulai monitoring.",
        )
        return

    # Validasi cookie ke Shopee sebelum mulai \u2014 pastikan sudah login
    try:
        cookie = decrypt(user["cookie_enc"])
    except ValueError:
        await send_message(
            chat_id,
            "\u274c Cookie tidak dapat dibaca. Jalankan /setcredentials untuk kirim cookie baru.",
        )
        return

    await send_message(chat_id, "\ud83d\udd0d Memeriksa sesi Shopee...")

    # Sertakan risktoken tersimpan (bila ada) agar pre-check konsisten
    # dengan yang dipakai worker saat monitoring.
    risktoken = None
    risktoken_enc = user.get("risktoken_enc")
    if risktoken_enc:
        try:
            risktoken = decrypt(risktoken_enc)
        except ValueError:
            risktoken = None

    session = await validate_cookie(cookie, risktoken=risktoken)

    if not session.valid:
        await send_message(
            chat_id,
            f"\u274c Sesi Shopee tidak valid: {session.reason}\n\n"
            "Jalankan /setcredentials untuk kirim cookie baru.",
        )
        return

    username_info = f" (@{session.account_username})" if session.account_username else ""
    await db.users.update_one(
        {"telegram_id": user_id},
        {"$set": {"monitoring_active": True}},
    )

    if await start_worker(user_id):
        await send_message(chat_id, f"\u25b6\ufe0f Monitoring dimulai{username_info}.")
    else:
        await send_message(chat_id, "\u2139\ufe0f Monitoring sudah berjalan.")


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

    await send_message(chat_id, "\u23f9\ufe0f Monitoring dihentikan.")


async def command_status(
    chat_id: int,
    user_id: int,
) -> None:
    """Show non-secret configuration status."""
    db = await get_db()
    user = await db.users.find_one({"telegram_id": user_id}) or {}

    active = is_worker_running(user_id)
    cookie_status = "\u2705" if user.get("cookie_enc") else "\u274c"
    bot_status = "\u2705" if user.get("custom_bot_token_enc") else "\u2796"
    group_status = "\u2705" if user.get("group_chat_id") else "\u2796"

    account = html.escape(user.get("account_username") or "-")
    area = html.escape(user.get("area") or settings.default_area)
    keywords = html.escape(user.get("keywords") or settings.default_keywords)

    # Phone status
    has_phone = user.get("account_has_phone")
    if has_phone is True:
        phone_status = "\ud83d\udcf1 Ada HP"
    elif has_phone is False:
        phone_status = "\ud83d\udcf5 No Phone"
    else:
        phone_status = "\u2796"

    n_proxies = proxy_pool_size()
    proxy_status = f"\ud83c\udf10 {n_proxies} proxy" if n_proxies else "\u2796 langsung (tanpa proxy)"

    status_icon = "\ud83d\udfe2" if active else "\ud83d\udd34"
    status_text = "Aktif" if active else "Tidak aktif"
    text = (
        f"{status_icon} <b>Status:</b> "
        f"{status_text}\n\n"
        f"<b>Akun:</b> {account}\n"
        f"<b>Phone:</b> {phone_status}\n"
        f"<b>Cookie:</b> {cookie_status}\n"
        f"<b>Custom bot:</b> {bot_status}\n"
        f"<b>Group:</b> {group_status}\n"
        f"<b>Proxy:</b> {proxy_status}\n"
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

    await send_message(chat_id, "\ud83d\udd04 Daftar item yang sudah diperiksa direset.")


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
