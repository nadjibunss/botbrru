"""Backward-compat shim. Real module lives at src.utils.database."""
from src.utils.database import create_indexes, get_client, get_db  # noqa: F401
