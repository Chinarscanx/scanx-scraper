#!/usr/bin/env python3
"""
Syncs ScanX news from the (private, session-authenticated) getLiveNews
API and appends any items newer than what's already saved into
data/news-YYYY-MM-DD.jsonl (dated by the article's real publish date,
in IST).

Unlike the old HTML-scraping approach, this doesn't need to run every
30 minutes. It's designed to be triggered on demand (manually, or on
whatever schedule you like): each run figures out the newest article
timestamp already saved, then pages backward through the API from
"now" until it reaches that point - so it doesn't matter if it's been
4 hours or 4 days since the last run, one sync call catches up the
whole gap.

Requires two secrets, read from the environment:
  SCANX_AUTH_TOKEN  - the "Auth" header value (a short-lived JWT,
                       expires ~5.5h after issue - grab a fresh one
                       from your browser's DevTools -> Network tab
                       whenever you run this)
  SCANX_ENTITY_ID   - the "entity_id" field from the request payload
                       (tied to your session/account)
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

API_URL = "https://news-live.dhan.co/v3/news/getLiveNews"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
STATUS_FILE = Path(__file__).resolve().parent.parent / "status.json"
IST = timezone(timedelta(hours=5, minutes=30))
PAGE_LIMIT = 50
MAX_PAGES = 200  # safety cap so a bug can't loop forever


class AuthExpiredError(Exception):
    pass


def write_status(status: str, message: str, new_items: int = 0) -> None:
    """Always-written heartbeat file so the viewer webpage can show a
    banner when the token needs refreshing, instead of you having to
    check the GitHub Actions tab."""
    payload = {
        "status": status,  # "ok" | "auth_expired" | "error"
        "message": message,
        "new_items": new_items,
        "last_attempt_utc": datetime.now(timezone.utc).isoformat(),
    }
    STATUS_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def get_env_or_die(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        print(f"ERROR: required environment variable {name} is not set.", file=sys.stderr)
        sys.exit(1)
    return val


def make_id(entry: dict) -> str:
    article_id = entry.get("article_id")
    if article_id:
        return str(article_id)
    # Fallback for entries with article_id == 0: their publish_date is
    # in milliseconds and effectively unique per (symbol, title).
    raw = f"{entry.get('sm_symbol','')}|{entry.get('news_object',{}).get('title','')}|{entry.get('publish_date','')}"
    import hashlib
    return "h" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def normalize(entry: dict) -> dict:
    nobj = entry.get("news_object", {}) or {}
    publish_ms = entry.get("publish_date")
    publish_iso = None
    if publish_ms:
        publish_iso = datetime.fromtimestamp(publish_ms / 1000, tz=timezone.utc).isoformat()

    return {
        "id": make_id(entry),
        "symbol": entry.get("sm_symbol") or entry.get("display_symbol"),
        "stock_name": entry.get("stock_name") or entry.get("display_symbol"),
        "headline": nobj.get("title"),
        "summary": nobj.get("text"),
        "sentiment": nobj.get("overall_sentiment"),
        "category": entry.get("category"),
        "sub_category": entry.get("sub_category"),
        "publish_date_ms": publish_ms,
        "publish_date_iso": publish_iso,
        "article_id": entry.get("article_id") or None,
        "seo_symbol": entry.get("seo_symbol") or None,
    }


def day_file_for(publish_ms: int) -> Path:
    dt_ist = datetime.fromtimestamp(publish_ms / 1000, tz=IST)
    return DATA_DIR / f"news-{dt_ist:%Y-%m-%d}.jsonl"


def load_all_existing_ids_and_max_ts() -> tuple[set[str], int]:
    """Scan every existing data file to build the full set of already-saved
    ids (for dedup) and find the newest publish_date_ms already stored
    (the sync checkpoint)."""
    ids: set[str] = set()
    max_ts = 0
    if not DATA_DIR.exists():
        return ids, max_ts
    for f in DATA_DIR.glob("news-*.jsonl"):
        with f.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "id" in rec:
                    ids.add(rec["id"])
                ts = rec.get("publish_date_ms") or 0
                if ts > max_ts:
                    max_ts = ts
    return ids, max_ts


def fetch_page(auth_token: str, entity_id: str, first_ts: int, last_ts: int, page_no: int) -> dict:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "excludecache": "true",
        "Auth": auth_token,
        "Origin": "https://scanx.trade",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
    }
    payload = {
        "categories": ["ALL"],
        "page_no": page_no,
        "limit": PAGE_LIMIT,
        "first_news_timeStamp": first_ts,
        "last_news_timeStamp": last_ts,
        "news_feed_type": "live",
        "stock_list": [],
        "entity_id": entity_id,
    }
    resp = requests.post(API_URL, headers=headers, json=payload, timeout=30)
    if resp.status_code in (401, 403):
        raise AuthExpiredError(
            f"Auth rejected (HTTP {resp.status_code}) - token has likely expired."
        )
    resp.raise_for_status()
    return resp.json()


def run_sync() -> int:
    auth_token = get_env_or_die("SCANX_AUTH_TOKEN")
    entity_id = get_env_or_die("SCANX_ENTITY_ID")

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    existing_ids, checkpoint_ts = load_all_existing_ids_and_max_ts()
    print(
        f"Checkpoint: newest article already saved is "
        f"{datetime.fromtimestamp(checkpoint_ts/1000, tz=IST) if checkpoint_ts else 'none (first run)'}"
    )

    now_ms = int(time.time() * 1000)
    first_ts = now_ms
    last_ts = now_ms

    all_new_records = []
    page_no = 1
    reached_checkpoint = False

    while page_no <= MAX_PAGES:
        result = fetch_page(auth_token, entity_id, first_ts, last_ts, page_no)
        data = result.get("data", {})
        batch = (data.get("latest_news") or []) + (data.get("next_news") or [])

        if not batch:
            print(f"Page {page_no}: empty response, stopping.")
            break

        oldest_in_batch = min(e.get("publish_date", now_ms) for e in batch)

        for entry in batch:
            rec = normalize(entry)
            if rec["id"] in existing_ids:
                continue
            if (rec["publish_date_ms"] or 0) <= checkpoint_ts:
                continue
            all_new_records.append(rec)
            existing_ids.add(rec["id"])  # avoid re-adding if it reappears in next page

        print(f"Page {page_no}: {len(batch)} items, oldest = "
              f"{datetime.fromtimestamp(oldest_in_batch/1000, tz=IST)}")

        if oldest_in_batch <= checkpoint_ts:
            reached_checkpoint = True
            break

        # Page further back in time.
        last_ts = oldest_in_batch - 1
        page_no += 1

    if not reached_checkpoint and page_no > MAX_PAGES:
        print(f"WARNING: hit the {MAX_PAGES}-page safety cap before reaching "
              "the checkpoint. Some older gap may remain - run again to continue.",
              file=sys.stderr)

    if not all_new_records:
        print("No new items to save.")
        write_status("ok", "Synced - no new items.", new_items=0)
        return 0

    # Group by (IST) calendar day and append to the right file.
    by_file: dict[Path, list[dict]] = {}
    for rec in all_new_records:
        f = day_file_for(rec["publish_date_ms"])
        by_file.setdefault(f, []).append(rec)

    for f, records in by_file.items():
        records.sort(key=lambda r: r["publish_date_ms"])
        with f.open("a", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"Wrote {len(records)} new item(s) to {f.name}")

    print(f"Done. {len(all_new_records)} new item(s) total across {len(by_file)} day file(s).")
    write_status("ok", f"Synced {len(all_new_records)} new item(s).", new_items=len(all_new_records))
    return 0


def main() -> int:
    try:
        return run_sync()
    except AuthExpiredError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        write_status(
            "auth_expired",
            "SCANX_AUTH_TOKEN has expired - grab a fresh one from DevTools "
            "and update the GitHub secret.",
        )
        return 2
    except Exception as e:  # noqa: BLE001 - want to record *any* failure
        print(f"ERROR: unexpected failure: {e}", file=sys.stderr)
        write_status("error", f"Unexpected error: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
