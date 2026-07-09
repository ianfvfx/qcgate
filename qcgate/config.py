"""
config.py — Read and write configuration values from the database.

All admin-configurable settings live in the config table.
Use get() to read a value, set() to update one.
"""

from typing import Optional
from qcgate.database import get_connection


def get(key: str) -> Optional[str]:
    """
    Retrieve a config value by key.
    Returns None if the key does not exist.
    """
    conn = get_connection()
    row = conn.execute(
        "SELECT value FROM config WHERE key = ?", (key,)
    ).fetchone()
    conn.close()
    return row["value"] if row else None


def set(key: str, value: str) -> None:
    """
    Update a config value. The key must already exist
    (defaults are created at initialisation time).
    Raises KeyError if the key is not found.
    """
    conn = get_connection()
    result = conn.execute(
        "UPDATE config SET value = ? WHERE key = ?", (value, key)
    )
    conn.commit()
    conn.close()

    if result.rowcount == 0:
        raise KeyError(f"Config key '{key}' not found.")


def get_all() -> dict:
    """
    Return all config values as a dictionary.
    Useful for rendering the admin settings page.
    """
    conn = get_connection()
    rows = conn.execute(
        "SELECT key, value, description FROM config"
    ).fetchall()
    conn.close()
    return {row["key"]: {"value": row["value"], "description": row["description"]} for row in rows}
