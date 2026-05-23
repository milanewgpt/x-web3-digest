import json
import os
import re
import sqlite3
import urllib.parse
import urllib.request
from pathlib import Path
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

DB_PATH = os.environ.get("DB_PATH", "x_digest.sqlite3")
TG_TARGET = os.environ.get("TG_TARGET", "")
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()
TG_MESSAGE_THREAD_ID = os.environ.get("TG_MESSAGE_THREAD_ID", "").strip()
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
        category_order = ["Base", "Solana", "Venice", "Polymarket", "Perps", "AI"]
        grouped = {cat: [] for cat in category_order}
        grouped["Other"] = []
        for it in selected:
            cat = it.get("category") or "Other"
            grouped.setdefault(cat, []).append(it)

        sections = []
        for cat in category_order + [c for c in grouped if c not in category_order and c != "Other"] + ["Other"]:
            cat_items = grouped.get(cat) or []
            if not cat_items:
                continue
            formatted = "\n\n".join(format_item(it, i + 1) for i, it in enumerate(cat_items))
            sections.append(f"## {cat}\n\n{formatted}")
        body = "\n\n".join(sections)
        body += f"\n\nОтфильтровано: {max(0, len(rows) - len(selected))} постов"

    if errors:
        body += "\n\nОшибки сбора:\n" + errors[:1000]
    return header + "\n\n" + body


def load_secret(path):
    p = Path(path).expanduser()
    return p.read_text().strip() if p.exists() else ""


def get_tg_bot_token():
    return TG_BOT_TOKEN or load_secret("~/.hermes/secrets/x_digest_tg_bot_token")


def get_tg_chat_id():
    return TG_CHAT_ID or load_secret("~/.hermes/secrets/x_digest_tg_chat_id")


def get_tg_message_thread_id():
    return TG_MESSAGE_THREAD_ID or load_secret("~/.hermes/secrets/x_digest_tg_message_thread_id")


def send_via_bot_api(text):
    token = get_tg_bot_token()
    chat_id = get_tg_chat_id()
    thread_id = get_tg_message_thread_id()
    if not token or not chat_id:
        raise RuntimeError("TG_BOT_TOKEN and TG_CHAT_ID are required for Bot API delivery")

    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if thread_id:
        payload["message_thread_id"] = int(thread_id)

    data = urllib.parse.urlencode(payload).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read().decode())
    if not result.get("ok"):
        raise RuntimeError(f"Telegram sendMessage failed: {result}")
    return result


async def send_via_telethon(text):
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    api_id = int(os.environ["TG_API_ID"])
    api_hash = os.environ["TG_API_HASH"]
    session_str = os.environ.get("TG_SESSION_STRING") or os.environ.get("TG_SESSION_SENDER")
    session = StringSession(session_str) if session_str and len(session_str) > 50 else (
        session_str or os.environ.get("TG_SESSION", "reader_session")
    )
    if not TG_TARGET:
        raise RuntimeError("TG_TARGET is required for Telethon delivery")
    client = TelegramClient(session, api_id, api_hash)
    await client.start()
    try:
        target_entity = await resolve_target_by_name(client, TG_TARGET)
        await client.send_message(target_entity, text)
    finally:
        await client.disconnect()


async def send_digest(text):
    if get_tg_bot_token() and get_tg_chat_id():
        send_via_bot_api(text)
    else:
        await send_via_telethon(text)


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

    await send_digest(digest)

    set_state(con, "digest:last_sent_utc", end.isoformat())
    con.close()


if __name__ == "__main__":
    import asyncio
    asyncio.run(run())
