"""Application configuration."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    mongo_uri: str = "mongodb://localhost:27017"
    mongo_db: str = "shopee_monitor"

    fernet_key: str
    master_bot_token: str

    host: str = "0.0.0.0"
    port: int = 8000

    log_level: str = "INFO"

    default_keywords: str = "GMP|Rosebrand Kuning|Gulaku Kuning"
    default_area: str = "Kab. Bekasi"
    request_delay_min: int = 10
    request_delay_max: int = 20

    session_validation_url: str = ""

    # ── HTTP client / anti-bot tuning (used by services.shopee_client) ──
    proxy_url: str = ""
    request_timeout: float = 30.0
    max_retries: int = 3
    retry_backoff_base: float = 2.0

    # ── Browser fingerprint / User-Agent (used by utils.headers & shopee_client) ──
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
    sec_ch_ua: str = '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"'
    sec_ch_ua_mobile: str = "?0"
    sec_ch_ua_platform: str = '"Windows"'
    sec_ch_ua_platform_version: str = '"10.0.0"'

    # ── Shopee client hints (used by utils.headers) ──
    shopee_language: str = "id"
    shopee_timezone: str = "Asia/Jakarta"
    shopee_timezone_offset: str = "7"  # UTC+7 (Asia/Jakarta); ubah bila perlu
    shopee_client_version: str = "1.0.0"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )


settings = Settings()
