"""Application configuration."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    mongo_uri: str = "mongodb://localhost:27017"
    mongo_db: str = "shopee_monitor"

    fernet_key: str
    master_bot_token: str

    host: str = "0.0.0.0"
    port: int = 8000

    default_keywords: str = "GMP|Rosebrand Kuning|Gulaku Kuning"
    default_area: str = "Kab. Bekasi"
    request_delay_min: int = 10
    request_delay_max: int = 20

    session_validation_url: str = ""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )


settings = Settings()
