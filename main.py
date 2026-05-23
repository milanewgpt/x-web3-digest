import os
import subprocess
import time
from datetime import datetime
from zoneinfo import ZoneInfo

POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "3600"))
SEND_HOURS = {int(h.strip()) for h in os.environ.get("SEND_HOURS", "8,11,14,19").split(",") if h.strip()}
TZ_DIGEST = ZoneInfo(os.environ.get("TZ_DIGEST", "Asia/Jerusalem"))

last_sent_key = None


def run_cmd(cmd):
    print("RUN", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)


while True:
    run_cmd(["python", "x_digest_collector.py"])
    now = datetime.now(TZ_DIGEST)
    key = now.strftime("%Y-%m-%d-%H")
    if now.hour in SEND_HOURS and key != last_sent_key:
        run_cmd(["python", "x_digest_sender.py"])
        last_sent_key = key
    time.sleep(POLL_SECONDS)
