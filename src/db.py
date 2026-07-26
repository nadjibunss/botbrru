"""Backward-compat shim. Real module lives at src.utils.database."""
from src.utils.database import close_db, get_db, is_db_available  # noqa: F401
