"""Telegram Bot API client."""
from __future__ import annotations

import logging

import httpx

from src.config import settings

logger = logging.getLogger(__name__)

BASE_URL = "https://api.telegram.org/bot{token}"


class TelegramAPIError(Exception):
    """Raised when the Telegram Bot API returns a non-ok response.

    Lets the polling loop back off (sleep) instead of hammering the API
    in a tight loop when the token is invalid or Telegram is unavailable.
    """


def _url(token: str, method: str) -> str:
    return f"{BASE_URL.format(token=token)}/{method}"


def _safe_text(text: str) -> str:
    # Fast path: already valid UTF-8
    try:
        text.encode("utf-8")
        return text
    except UnicodeEncodeError:
        pass
    # Try to recover surrogate pairs as real emoji via utf-16
    try:
        fixed = text.encode("utf-16", errors="surrogatepass").decode("utf-16")
        fixed.encode("utf-8")
        return fixed
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    # Last resort: strip lone surrogates
    return text.encode("utf-8", errors="ignore").decode("utf-8")


async def _post(
    method: str,
    payload: dict,
    token: str | None = None,
) -> dict:
    bot_token = token or settings.master_bot_token

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(_url(bot_token, method), json=payload)
            response.raise_for_status()
            return response.json()
    except Exception:
        # Jangan log payload / token
        logger.exception("Telegram API call failed: %s", method)
        return {"ok": False}


async def _get(
    method: str,
    params: dict,
    token: str | None = None,
) -> dict:
    bot_token = token or settings.master_bot_token

    try:
        async with httpx.AsyncClient(timeout=40) as client:
            response = await client.get(
                _url(bot_token, method),
                params=params,
            )
            response.raise_for_status()
            return response.json()
    except Exception:
        logger.exception("Telegram API call failed: %s", method)
        return {"ok": False}


async def send_message(
    chat_id: int | str,
    text: str,
    token: str | None = None,
) -> dict:
    return await _post(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": _safe_text(text),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        token,
    )


async def delete_message(
    chat_id: int | str,
    message_id: int,
    token: str | None = None,
) -> bool:
    result = await _post(
        "deleteMessage",
        {"chat_id": chat_id, "message_id": message_id},
        token,
    )
    return bool(result.get("ok"))


async def delete_webhook() -> dict:
    return await _post(
        "deleteWebhook",
        {"drop_pending_updates": False},
    )


async def get_updates(offset: int, timeout: int = 30) -> list[dict]:
    data = await _get(
        "getUpdates",
        {
            "offset": offset,
            "timeout": timeout,
            "allowed_updates": ["message"],
        },
    )

    if not data.get("ok"):
        # Signal the failure so polling_loop sleeps before retrying,
        # rather than spinning in a tight loop against the API.
        raise TelegramAPIError("getUpdates returned a non-ok response")

    return data.get("result", [])
