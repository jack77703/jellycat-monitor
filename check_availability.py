#!/usr/bin/env python3
"""Poll FareHarbor for Jellycat Diner Experience openings and alert via Telegram.

State (which slots we've already alerted on) is persisted in seen_open_slots.json,
which the GitHub Actions workflow restores/saves via actions/cache between runs.
"""
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

COMPANY = "faoschwarz"
ITEM = "592655"
DATES = ["2026-09-24", "2026-09-25", "2026-09-26", "2026-09-27"]

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

BASE_DIR = Path(__file__).resolve().parent
SEEN_PATH = BASE_DIR / "seen_open_slots.json"

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) jellycat-monitor/1.0"


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


def main() -> int:
    seen = load_seen()
    had_error = False
    for date in DATES:
        try:
            open_slots = find_open_slots(date)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            print(f"ERROR checking {date}: {e}", file=sys.stderr)
            had_error = True
            continue

        if not open_slots:
            print(f"{date}: fully booked")
            continue

        new_slots = [s for s in open_slots if str(s[0]) not in seen]
        print(f"{date}: {len(open_slots)} open slot(s), {len(new_slots)} new")
        if new_slots:
            lines = [f"  {s[1]} (cap {s[2]})" for s in new_slots]
            msg = (
                f"Jellycat Diner opened on {date}!\n"
                + "\n".join(lines)
                + "\nBook now: https://faoschwarz.com/pages/reservations"
            )
            send_telegram(msg)
            print(f"Sent Telegram alert for {date}")
        for s in new_slots:
            seen.add(str(s[0]))

    save_seen(seen)
    return 1 if had_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
