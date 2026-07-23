"""MongoDB user document defaults."""

DEFAULT_USER_DOC = {
    "telegram_id": None,
    "telegram_username": None,

    "account_username": None,
    "cookie_enc": None,
    "cookie_verified_at": None,

    "custom_bot_token_enc": None,
    "group_chat_id": None,
    "fingerprint": None,

    "keywords": None,
    "area": None,

    "monitoring_active": False,
    "checked_items": [],

    "setup_state": None,
    "setup_payload": {},
}
