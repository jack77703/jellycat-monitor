#!/usr/bin/env python3
"""Poll FareHarbor for Jellycat Diner Experience openings and alert via Telegram.

Runs as one long GitHub Actions job (see .github/workflows/monitor.yml) that loops
internally every LOOP_INTERVAL_SECONDS for up to LOOP_DURATION_SECONDS, so the actual
check cadence isn't limited by GitHub's 5-minute cron floor. State (which slots we've
already alerted on) lives in seen_open_slots.json; whenever it changes, this script
commits and pushes it itself so state survives even if the job is killed mid-run.
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

COMPANY = "faoschwarz"
ITEM = "592655"
DATES = ["2026-09-24", "2026-09-25", "2026-09-26", "2026-09-27"]

LOOP_INTERVAL_SECONDS = 120
LOOP_DURATION_SECONDS = 295 * 60  # leaves headroom under GitHub's 6h job limit

# One heartbeat per calendar day (local to HEARTBEAT_TZ), only if a check cycle
# happens to run during this window, so you're never pinged overnight.
HEARTBEAT_TZ = ZoneInfo("America/New_York")
HEARTBEAT_START_HOUR = 10  # 10am
HEARTBEAT_END_HOUR = 1  # 1am (wraps past midnight)

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

BASE_DIR = Path(__file__).resolve().parent
SEEN_PATH = BASE_DIR / "seen_open_slots.json"
HEARTBEAT_STATE_PATH = BASE_DIR / "last_heartbeat.json"

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) jellycat-monitor/1.0"


def log(msg: str, file=None) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    print(f"[{ts} UTC] {msg}", file=file or sys.stdout)


def fetch_availabilities(date: str) -> list:
    url = (
        f"https://fareharbor.com/api/v1/companies/{COMPANY}/items/{ITEM}"
        f"/availabilities/date/{date}/"
    )
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.load(resp)
    return data.get("availabilities", [])


def find_open_slots(date: str) -> list:
    open_slots = []
    for slot in fetch_availabilities(date):
        if slot.get("is_bookable") and not slot.get("is_sold_out"):
            open_slots.append(
                (slot["pk"], slot["start_at"], slot.get("approximate_available_capacity"))
            )
    return open_slots


def send_telegram(text: str) -> None:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = json.dumps({"chat_id": CHAT_ID, "text": text}).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()


def load_seen() -> set:
    if SEEN_PATH.exists():
        return set(json.loads(SEEN_PATH.read_text()))
    return set()


def save_seen(seen: set) -> None:
    SEEN_PATH.write_text(json.dumps(sorted(seen)))


def commit_and_push(paths: list, message: str) -> None:
    """Best-effort commit of the given state files so they survive a mid-run kill."""
    try:
        subprocess.run(["git", "add", *[str(p) for p in paths]], cwd=BASE_DIR, check=True)
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=BASE_DIR)
        if diff.returncode == 0:
            return  # nothing changed
        subprocess.run(["git", "commit", "-m", message], cwd=BASE_DIR, check=True)
        subprocess.run(["git", "push"], cwd=BASE_DIR, check=True)
    except subprocess.CalledProcessError as e:
        log(f"WARNING: failed to commit/push state: {e}", file=sys.stderr)


def _in_heartbeat_window(now: datetime) -> bool:
    hour = now.hour
    if HEARTBEAT_START_HOUR <= HEARTBEAT_END_HOUR:
        return HEARTBEAT_START_HOUR <= hour < HEARTBEAT_END_HOUR
    return hour >= HEARTBEAT_START_HOUR or hour < HEARTBEAT_END_HOUR


def maybe_send_heartbeat() -> None:
    """Send at most one heartbeat per local day, only during the allowed window."""
    now = datetime.now(HEARTBEAT_TZ)
    if not _in_heartbeat_window(now):
        return

    today = now.date().isoformat()
    last = None
    if HEARTBEAT_STATE_PATH.exists():
        last = json.loads(HEARTBEAT_STATE_PATH.read_text()).get("date")
    if last == today:
        return

    dates_str = ", ".join(DATES)
    send_telegram(
        f"Jellycat monitor heartbeat: still running, watching {dates_str}. "
        f"No open slots yet."
    )
    log("Sent daily heartbeat")
    HEARTBEAT_STATE_PATH.write_text(json.dumps({"date": today}))
    commit_and_push([HEARTBEAT_STATE_PATH], "Update heartbeat state")


def run_check_cycle(dates=None) -> bool:
    """Check given dates (default DATES). Returns True if any new open slot was found."""
    dates = dates or DATES
    seen = load_seen()
    found_new = False
    for date in dates:
        try:
            open_slots = find_open_slots(date)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            log(f"ERROR checking {date}: {e}", file=sys.stderr)
            continue

        if not open_slots:
            log(f"{date}: fully booked")
            continue

        new_slots = [s for s in open_slots if str(s[0]) not in seen]
        log(f"{date}: {len(open_slots)} open slot(s), {len(new_slots)} new")
        if new_slots:
            lines = [f"  {s[1]} (cap {s[2]})" for s in new_slots]
            msg = (
                f"Jellycat Diner opened on {date}!\n"
                + "\n".join(lines)
                + "\nBook now: https://faoschwarz.com/pages/reservations"
            )
            send_telegram(msg)
            log(f"Sent Telegram alert for {date}")
            found_new = True
        for s in new_slots:
            seen.add(str(s[0]))

    save_seen(seen)
    return found_new


def loop() -> None:
    start = time.monotonic()
    n = 0
    while time.monotonic() - start < LOOP_DURATION_SECONDS:
        n += 1
        log(f"--- check #{n} ---")
        if run_check_cycle():
            commit_and_push([SEEN_PATH], "Update seen open slots")
        maybe_send_heartbeat()
        time.sleep(LOOP_INTERVAL_SECONDS)
    log(f"Loop finished after {n} checks.")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--once":
        run_check_cycle()
    else:
        loop()
