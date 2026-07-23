"""Telegram Bot API client."""
from __future__ import annotations

import logging

import httpx

from src.config import settings

logger = logging.getLogger(__name__)

BASE_URL = "https://api.telegram.org/bot{token}"


def _url(token: str, method: str) -> str:
    return f"{BASE_URL.format(token=token)}/{method}"


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


def _safe_text(text: str) -> str:
    """Strip surrogate chars that cannot be UTF-8 encoded (e.g. from Shopee API)."""
    return text.encode("utf-8", errors="ignore").decode("utf-8")


async def send_message(
    chat_id: int | str,
    text: str,
    token: str | None = None,
) -> dict:
    \"\"\"Send HTML-formatted Telegram message.\"\"\"
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
