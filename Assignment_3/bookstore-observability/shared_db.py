import sqlite3
import os
import json
import time
from typing import Optional, List, Dict, Any

DEFAULT_DB_PATH = os.environ.get("BOOKSTORE_DB_PATH", os.path.join(os.path.dirname(__file__), "bookstore.db"))

# In-memory cache for per-replica delay settings so read path has near-zero overhead
_delay_cache: Dict[str, tuple[int, float]] = {}
_DELAY_CACHE_TTL = 0.5  # seconds
_local_delay_override_ms: Optional[int] = None


class DuplicateBookError(Exception):
    """Raised when attempting to add a book with identical title and author."""
    pass


class BookNotFoundError(Exception):
    """Raised when a book with the requested ID does not exist."""
    pass


def get_connection(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=15.0)
    conn.row_factory = sqlite3.Row
    # Enable WAL mode for high concurrency between processes/containers
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def init_db(db_path: str = DEFAULT_DB_PATH):
    """Initializes SQLite schema and guarantees Book ID=1 and initial seed books exist."""
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS books (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                author TEXT NOT NULL,
                price REAL NOT NULL CHECK(price > 0),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(title, author)
            );
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS idempotency_keys (
                key TEXT PRIMARY KEY,
                endpoint TEXT NOT NULL,
                status_code INTEGER NOT NULL,
                response_json TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS replica_settings (
                replica TEXT PRIMARY KEY,
                delay_ms INTEGER NOT NULL DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.commit()

        # Guarantee Book ID=1 exists so GET /books/1 and GetBook(id=1) always succeed
        cursor.execute("SELECT id FROM books WHERE id = 1;")
        if cursor.fetchone() is None:
            cursor.execute(
                "INSERT OR IGNORE INTO books (id, title, author, price) VALUES (1, ?, ?, ?);",
                ("The Great Gatsby", "F. Scott Fitzgerald", 10.99),
            )
            conn.commit()

        # Seed additional initial data if catalog has fewer than 3 books
        cursor.execute("SELECT COUNT(*) AS cnt FROM books;")
        if cursor.fetchone()["cnt"] < 3:
            initial_books = [
                ("The Great Gatsby", "F. Scott Fitzgerald", 10.99),
                ("1984", "George Orwell", 8.99),
                ("To Kill a Mockingbird", "Harper Lee", 12.50),
            ]
            cursor.executemany(
                "INSERT OR IGNORE INTO books (title, author, price) VALUES (?, ?, ?);",
                initial_books,
            )
            conn.commit()

        # Ensure default 0ms delay rows exist for A, B, C
        for rep in ("A", "B", "C", "D"):
            cursor.execute(
                "INSERT OR IGNORE INTO replica_settings (replica, delay_ms) VALUES (?, 0);",
                (rep,),
            )
        conn.commit()


def set_replica_delay_ms(replica: str, delay_ms: int, local_only: bool = False, db_path: str = DEFAULT_DB_PATH):
    """Sets fixed artificial delay (in ms) for a target replica (e.g., 'B')."""
    global _local_delay_override_ms
    delay_ms = max(0, int(delay_ms))
    if local_only:
        _local_delay_override_ms = delay_ms
        return
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO replica_settings (replica, delay_ms, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(replica) DO UPDATE SET
                delay_ms = excluded.delay_ms,
                updated_at = CURRENT_TIMESTAMP;
            """,
            (replica.upper(), delay_ms),
        )
        conn.commit()
    _delay_cache[replica.upper()] = (delay_ms, time.time())


def get_replica_delay_ms(replica: str, db_path: str = DEFAULT_DB_PATH) -> int:
    """Returns configured artificial delay (in ms) for the given replica with 0.5s TTL cache."""
    if _local_delay_override_ms is not None:
        return _local_delay_override_ms

    rep = replica.upper()
    now = time.time()
    cached = _delay_cache.get(rep)
    if cached and (now - cached[1]) < _DELAY_CACHE_TTL:
        return cached[0]

    try:
        with get_connection(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT delay_ms FROM replica_settings WHERE replica = ?;", (rep,))
            row = cursor.fetchone()
            delay_ms = int(row["delay_ms"]) if row else 0
            _delay_cache[rep] = (delay_ms, now)
            return delay_ms
    except Exception:
        return cached[0] if cached else 0


def get_all_replica_delays(db_path: str = DEFAULT_DB_PATH) -> Dict[str, int]:
    """Returns a dictionary of configured delays for all replicas."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT replica, delay_ms FROM replica_settings ORDER BY replica ASC;")
        return {row["replica"]: int(row["delay_ms"]) for row in cursor.fetchall()}


def get_all_books(db_path: str = DEFAULT_DB_PATH) -> List[Dict[str, Any]]:
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, title, author, price, created_at FROM books ORDER BY id ASC;")
        rows = cursor.fetchall()
        return [dict(row) for row in rows]


def get_book_by_id(book_id: int, db_path: str = DEFAULT_DB_PATH) -> Optional[Dict[str, Any]]:
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, title, author, price, created_at FROM books WHERE id = ?;", (book_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


def create_book(title: str, author: str, price: float, db_path: str = DEFAULT_DB_PATH) -> Dict[str, Any]:
    if not title or not title.strip():
        raise ValueError("Title must not be empty.")
    if not author or not author.strip():
        raise ValueError("Author must not be empty.")
    if price <= 0:
        raise ValueError("Price must be greater than zero.")

    title = title.strip()
    author = author.strip()

    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(
                "INSERT INTO books (title, author, price) VALUES (?, ?, ?);",
                (title, author, round(price, 2)),
            )
            book_id = cursor.lastrowid
            conn.commit()
        except sqlite3.IntegrityError as e:
            if "UNIQUE" in str(e).upper():
                raise DuplicateBookError(f"A book with title '{title}' by '{author}' already exists.")
            raise e

        cursor.execute("SELECT id, title, author, price, created_at FROM books WHERE id = ?;", (book_id,))
        return dict(cursor.fetchone())


def get_inventory_count(db_path: str = DEFAULT_DB_PATH) -> int:
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) AS cnt FROM books;")
        return cursor.fetchone()["cnt"]


def get_idempotency_record(key: str, db_path: str = DEFAULT_DB_PATH) -> Optional[Dict[str, Any]]:
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT key, endpoint, status_code, response_json, created_at FROM idempotency_keys WHERE key = ?;",
            (key,),
        )
        row = cursor.fetchone()
        if not row:
            return None
        res = dict(row)
        res["response_data"] = json.loads(res["response_json"])
        return res


def save_idempotency_record(
    key: str,
    endpoint: str,
    status_code: int,
    response_data: Dict[str, Any],
    db_path: str = DEFAULT_DB_PATH,
):
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT OR REPLACE INTO idempotency_keys (key, endpoint, status_code, response_json)
            VALUES (?, ?, ?, ?);
            """,
            (key, endpoint, status_code, json.dumps(response_data)),
        )
        conn.commit()
