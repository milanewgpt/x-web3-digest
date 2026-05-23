import os
import sqlite3

DB_PATH = os.environ.get("DB_PATH", "x_digest.sqlite3")


def open_db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def init_db():
    con = open_db()
    cur = con.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS tweets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            handle TEXT NOT NULL,
            label TEXT NOT NULL,
            category TEXT,
            priority TEXT,
            tweet_id TEXT NOT NULL,
            created_at_utc TEXT NOT NULL,
            text TEXT NOT NULL,
            url TEXT NOT NULL,
            is_reply INTEGER NOT NULL DEFAULT 0,
            is_retweet INTEGER NOT NULL DEFAULT 0,
            is_quote INTEGER NOT NULL DEFAULT 0,
            favorite_count INTEGER DEFAULT 0,
            retweet_count INTEGER DEFAULT 0,
            reply_count INTEGER DEFAULT 0,
            quote_count INTEGER DEFAULT 0,
            views_count INTEGER DEFAULT 0,
            raw_json TEXT,
            inserted_at_utc TEXT NOT NULL,
            UNIQUE(handle, tweet_id)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    con.commit()
    con.close()


if __name__ == "__main__":
    init_db()
    print(f"Initialized {DB_PATH}")
