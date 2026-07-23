Encryption utilities for sensitive data at rest.
from cryptography.fernet import Fernet, InvalidToken

from src.config import settings


def _fernet() -> Fernet:
    return Fernet(settings.fernet_key.encode())


def encrypt(value: str) -> str:
    Encrypt plaintext to a URL-safe Fernet token.
    return _fernet().encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt(value: str) -> str:
    Decrypt a Fernet token to plaintext.
    try:
        return _fernet().decrypt(value.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("Encrypted value is invalid") from exc
