import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import requests
import yaml

DB_PATH = os.environ.get("DB_PATH", "x_digest.sqlite3")
ACCOUNTS_FILE = os.environ.get("ACCOUNTS_FILE", "accounts.yaml")
EXCLUDE_REPLIES = os.environ.get("EXCLUDE_REPLIES", "true").lower() in {"1", "true", "yes"}
EXCLUDE_RETWEETS = os.environ.get("EXCLUDE_RETWEETS", "true").lower() in {"1", "true", "yes"}
PAGES_PER_ACCOUNT = int(os.environ.get("PAGES_PER_ACCOUNT", "1"))
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "25"))
SOCIALDATA_BASE = "https://api.socialdata.tools/twitter/search"


def now_utc_iso():
    return datetime.now(timezone.utc).isoformat()


def open_db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def init_db():
    import db_init
    db_init.init_db()


def get_state(con, key, default=""):
    cur = con.cursor()
    cur.execute("SELECT value FROM state WHERE key = ?", (key,))
    row = cur.fetchone()
    return row[0] if row else default


def set_state(con, key, value):
    cur = con.cursor()
    cur.execute(
        "INSERT INTO state(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    con.commit()


def load_socialdata_key():
    key = os.environ.get("SOCIALDATA_KEY", "").strip()
    if key:
        return key
    for p in ["~/.openclaw/secrets/socialdata_api_key", "~/.hermes/secrets/socialdata_api_key"]:
        path = Path(p).expanduser()
        if path.exists():
            return path.read_text().strip()
    raise RuntimeError("SOCIALDATA_KEY is missing")


def load_accounts():
    path = Path(ACCOUNTS_FILE)
    if path.exists():
        data = yaml.safe_load(path.read_text()) or {}
        accounts = data.get("accounts") or []
        return [
            {
                "handle": str(a["handle"]).lstrip("@"),
                "label": str(a.get("label") or a["handle"]).lstrip("@"),
                "category": str(a.get("category") or ""),
                "priority": str(a.get("priority") or "normal"),
            }
            for a in accounts
            if a.get("handle")
        ]
    raw = os.environ.get("X_ACCOUNTS", "")
    return [
        {"handle": h.strip().lstrip("@"), "label": h.strip().lstrip("@"), "category": "", "priority": "normal"}
        for h in raw.split(",") if h.strip()
    ]


def parse_dt(value):
    if not value:
        return now_utc_iso()
    value = value.replace("Z", "+00:00")
    return datetime.fromisoformat(value).astimezone(timezone.utc).isoformat()


def tweet_text(tw):
    return (tw.get("full_text") or tw.get("text") or "").strip()


def tweet_url(handle, tweet_id):
    return f"https://x.com/{handle}/status/{tweet_id}"


def socialdata_search(api_key, query, cursor=""):
    params = {"query": query, "type": "Latest"}
    if cursor:
        params["cursor"] = cursor
    url = SOCIALDATA_BASE + "?" + urlencode(params)
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        timeout=REQUEST_TIMEOUT,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"SocialData error {resp.status_code}: {resp.text[:500]}")
    return resp.json()


def save_tweet(con, account, tw):
    text = tweet_text(tw)
    tweet_id = str(tw.get("id_str") or tw.get("id") or "")
    if not text or not tweet_id:
        return False

    is_reply = 1 if tw.get("in_reply_to_status_id_str") else 0
    is_retweet = 1 if tw.get("retweeted_status") else 0
    if EXCLUDE_REPLIES and is_reply:
        return False
    if EXCLUDE_RETWEETS and is_retweet:
        return False

    handle = account["handle"]
    cur = con.cursor()
    cur.execute(
        """
        INSERT OR IGNORE INTO tweets(
            handle, label, category, priority, tweet_id, created_at_utc, text, url,
            is_reply, is_retweet, is_quote,
            favorite_count, retweet_count, reply_count, quote_count, views_count,
            raw_json, inserted_at_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            handle,
            account["label"],
            account["category"],
            account["priority"],
            tweet_id,
            parse_dt(tw.get("tweet_created_at") or tw.get("created_at")),
            text,
            tweet_url(handle, tweet_id),
            is_reply,
            is_retweet,
            1 if tw.get("is_quote_status") else 0,
            int(tw.get("favorite_count") or 0),
            int(tw.get("retweet_count") or 0),
            int(tw.get("reply_count") or 0),
            int(tw.get("quote_count") or 0),
            int(tw.get("views_count") or 0),
            json.dumps(tw, ensure_ascii=False),
            now_utc_iso(),
        ),
    )
    return cur.rowcount > 0


def collect_account(con, api_key, account):
    handle = account["handle"]
    last_id = get_state(con, f"last_seen_id:{handle}", "")
    query = f"from:{handle} -filter:replies"
    if not EXCLUDE_REPLIES:
        query = f"from:{handle}"
    if last_id:
        query += f" since_id:{last_id}"

    inserted = 0
    max_seen = int(last_id) if last_id.isdigit() else 0
    cursor = ""
    for page in range(PAGES_PER_ACCOUNT):
        data = socialdata_search(api_key, query, cursor)
        tweets = data.get("tweets") or []
        for tw in tweets:
            tid = str(tw.get("id_str") or tw.get("id") or "")
            if tid.isdigit():
                max_seen = max(max_seen, int(tid))
            if save_tweet(con, account, tw):
                inserted += 1
        con.commit()
        cursor = data.get("next_cursor") or ""
        if not cursor or not tweets:
            break
        time.sleep(1)

    if max_seen:
        set_state(con, f"last_seen_id:{handle}", str(max_seen))
    set_state(con, f"last_seen_at:{handle}", now_utc_iso())
    return inserted


def run():
    init_db()
    api_key = load_socialdata_key()
    accounts = load_accounts()
    if not accounts:
        raise RuntimeError("No X accounts configured")

    con = open_db()
    total = 0
    errors = []
    try:
        for account in accounts:
            try:
                n = collect_account(con, api_key, account)
                total += n
                print(f"@{account['handle']}: +{n}")
            except Exception as e:
                msg = f"@{account['handle']}: {e}"
                errors.append(msg)
                print("ERROR", msg)
        set_state(con, "collector:last_run_utc", now_utc_iso())
        if errors:
            set_state(con, "collector:last_errors", "\n".join(errors[-20:]))
    finally:
        con.close()
    print(f"Inserted: {total}")


if __name__ == "__main__":
    run()
