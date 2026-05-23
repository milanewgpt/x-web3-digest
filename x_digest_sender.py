import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from telethon import TelegramClient
from telethon.sessions import StringSession

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
_session_str = os.environ.get("TG_SESSION_STRING") or os.environ.get("TG_SESSION_SENDER")
SESSION = StringSession(_session_str) if _session_str and len(_session_str) > 50 else (_session_str or os.environ.get("TG_SESSION", "reader_session"))

DB_PATH = os.environ.get("DB_PATH", "x_digest.sqlite3")
TG_TARGET = os.environ["TG_TARGET"]
TZ_DIGEST = ZoneInfo(os.environ.get("TZ_DIGEST", "Asia/Jerusalem"))
MAX_ITEMS = int(os.environ.get("MAX_ITEMS", "12"))
MAX_CHARS_PER_ITEM = int(os.environ.get("MAX_CHARS_PER_ITEM", "300"))

NOISE_PATTERNS = [
    r"\bgm\b", r"\bgn\b", r"\bgiveaway\b", r"\bretweet\b", r"\blike and repost\b",
    r"\bjoin now\b", r"\bclaim now\b", r"\bwhitelist\b", r"\bairdrop spam\b",
]

SIGNAL_KEYWORDS = {
    "prediction": ["polymarket", "kalshi", "odds", "forecast", "probability", "resolution", "market", "liquidity", "spread"],
    "trading": ["funding", "basis", "open interest", "liquidation", "perp", "arb", "arbitrage", "momentum"],
    "defi": ["yield", "vault", "tvl", "revenue", "fees", "incentives", "points", "airdrop", "snapshot"],
    "ai_agents": ["agent", "codex", "claude", "grok", "cursor", "mcp", "automation", "workflow"],
    "time_sensitive": ["deadline", "ends", "today", "tomorrow", "launch", "released", "live", "maintenance", "listing"],
}


def open_db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def init_state(con):
    con.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    con.commit()


def get_state(con, key, default=""):
    row = con.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_state(con, key, value):
    con.execute(
        "INSERT INTO state(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    con.commit()


def parse_dt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def now_utc():
    return datetime.now(timezone.utc)


def clean_text(t):
    t = re.sub(r"https?://\S+", "", t or "")
    t = re.sub(r"\s+", " ", t).strip()
    return t


def is_noise(t):
    low = t.lower()
    if len(low) < 20:
        return True
    return any(re.search(p, low) for p in NOISE_PATTERNS)


def score_signal(row):
    text = row["text"].lower()
    score = 0
    matched = []
    for group, words in SIGNAL_KEYWORDS.items():
        if any(w in text for w in words):
            score += 3 if group in {"prediction", "trading", "time_sensitive"} else 2
            matched.append(group)
    if row["priority"] == "high":
        score += 2
    if row["views_count"] >= 50000:
        score += 2
    if row["favorite_count"] >= 100:
        score += 1
    if re.search(r"\b\d+(\.\d+)?%?\b", text):
        score += 1
    return score, matched


def dedupe(items):
    seen = set()
    out = []
    for it in items:
        key = re.sub(r"\W+", " ", it["text"].lower()).strip()[:160]
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def format_item(it, idx):
    text = it["text"]
    if len(text) > MAX_CHARS_PER_ITEM:
        text = text[: MAX_CHARS_PER_ITEM - 1] + "…"
    why = ", ".join(it["matched"]) if it["matched"] else "watchlist"
    metrics = []
    if it["favorite_count"]:
        metrics.append(f"❤ {it['favorite_count']}")
    if it["retweet_count"]:
        metrics.append(f"🔁 {it['retweet_count']}")
    if it["views_count"]:
        metrics.append(f"👁 {it['views_count']}")
    metrics_s = " | " + " ".join(metrics) if metrics else ""
    return f"{idx}. @{it['handle']} — {text}\nПочему: {why}{metrics_s}\nИсточник: {it['url']}"


def build_digest(rows, start_utc, end_utc, errors=""):
    items = []
    for row in rows:
        text = clean_text(row["text"])
        if not text or is_noise(text):
            continue
        item = dict(row)
        item["text"] = text
        item["date"] = parse_dt(row["created_at_utc"])
        item["score"], item["matched"] = score_signal(item)
        if item["score"] <= 0:
            continue
        items.append(item)

    items = dedupe(items)
    items.sort(key=lambda x: (x["score"], x["date"]), reverse=True)
    selected = items[:MAX_ITEMS]

    start_local = start_utc.astimezone(TZ_DIGEST).strftime("%H:%M")
    end_local = end_utc.astimezone(TZ_DIGEST).strftime("%H:%M")
    header = f"X Web3 Digest ({start_local}–{end_local} Israel)"

    if not selected:
        body = "Нет новых сигналов."
    else:
        body = "\n\n".join(format_item(it, i + 1) for i, it in enumerate(selected))
        body += f"\n\nОтфильтровано: {max(0, len(rows) - len(selected))} постов"

    if errors:
        body += "\n\nОшибки сбора:\n" + errors[:1000]
    return header + "\n\n" + body


async def resolve_target_by_name(client, target_name):
    async for dialog in client.iter_dialogs():
        if dialog.name == target_name:
            return dialog.entity
    return await client.get_entity(target_name)


async def run():
    con = open_db()
    init_state(con)
    end = now_utc()
    last_sent_iso = get_state(con, "digest:last_sent_utc", "")
    if last_sent_iso:
        start = parse_dt(last_sent_iso)
        if start > end or end - start > timedelta(days=2):
            start = end - timedelta(hours=12)
    else:
        start = end - timedelta(hours=6)

    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT * FROM tweets
        WHERE created_at_utc > ? AND created_at_utc <= ?
        ORDER BY created_at_utc ASC
        """,
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    errors = get_state(con, "collector:last_errors", "")
    digest = build_digest(rows, start, end, errors)

    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.start()
    target_entity = await resolve_target_by_name(client, TG_TARGET)
    await client.send_message(target_entity, digest)
    await client.disconnect()

    set_state(con, "digest:last_sent_utc", end.isoformat())
    con.close()


if __name__ == "__main__":
    import asyncio
    asyncio.run(run())
