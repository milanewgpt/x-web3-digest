# X Web3 Digest

Semi-passive X/Twitter digest for Web3, prediction markets, DeFi and AI-agent accounts.

Built as a separate project based on `tg-web3-digest`, but with X collection via Bird/SocialData-style sources. AISA is intentionally not used.

## Architecture

- `x_digest_collector.py`
  - reads a configured list of X accounts
  - uses SocialData search endpoint: `from:handle since_id:last_seen`
  - saves normalized tweets to SQLite
  - stores `last_seen_id:<handle>` state

- `x_digest_sender.py`
  - reads tweets since `digest:last_sent_utc`
  - filters noise/replies/retweets
  - scores Web3/prediction-market/trading/AI-agent signals
  - sends a compact Russian-friendly digest to Telegram

- `main.py`
  - Railway-friendly loop: collect every `POLL_SECONDS`, send at `SEND_HOURS` in `TZ_DIGEST`

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
cp accounts.example.yaml accounts.yaml
python db_init.py
```

Required env:

```env
TG_API_ID=...
TG_API_HASH=...
TG_SESSION_STRING=...
TG_TARGET=Digest_mi
SOCIALDATA_KEY=...
DB_PATH=/data/x_digest.sqlite3
```

If `SOCIALDATA_KEY` is not set, collector checks:

- `~/.openclaw/secrets/socialdata_api_key`
- `~/.hermes/secrets/socialdata_api_key`

## Accounts

Edit `accounts.yaml`:

```yaml
accounts:
  - handle: polymarket
    label: Polymarket
    category: prediction_markets
    priority: high
```

## Run manually

```bash
python x_digest_collector.py
python x_digest_sender.py
```

## Railway

Use a persistent volume mounted at `/data` and set:

```env
DB_PATH=/data/x_digest.sqlite3
POLL_SECONDS=3600
SEND_HOURS=8,11,14,19
TZ_DIGEST=Asia/Jerusalem
```

Start command:

```bash
python main.py
```

## Source policy

Priority:

1. SocialData API key via Bird-style workflow.
2. Cookie/X-account fallback can be added later if SocialData is insufficient or too limited.
3. AISA is not part of this project.

## Risk notes

- No-login X monitoring is inherently brittle unless using a third-party data provider.
- SocialData costs per fetched tweet/user; keep account list tight and `PAGES_PER_ACCOUNT=1` by default.
- Replies and retweets are excluded by default to reduce operational noise.
