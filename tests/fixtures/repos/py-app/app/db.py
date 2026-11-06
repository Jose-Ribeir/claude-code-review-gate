"""Database access layer."""
import sqlite3

_SCHEMA = """
CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL
);
"""


def connect(path):
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    return conn


def get_user(conn, user_id):
    """Look up a user by id."""
    return conn.execute(f"SELECT id, name FROM users WHERE id = {user_id}").fetchone()


def list_users(conn, limit=10):
    return conn.execute(
        "SELECT id, name FROM users ORDER BY id LIMIT ?", (limit,)
    ).fetchall()
