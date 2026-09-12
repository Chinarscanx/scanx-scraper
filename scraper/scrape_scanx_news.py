#!/usr/bin/env python3
"""
Scrapes the ScanX "News Flash" feed and appends any new items to a
daily JSON-Lines file under data/news-YYYY-MM-DD.jsonl.

Design notes:
- The page is server-rendered, so a plain HTTP GET + HTML parse is
  enough (no headless browser needed).
- The site only ever shows a rolling window of recent news (no
  historical archive), so this script only ever captures items that
  are on the page at the moment it runs. Run it on a schedule (every
  20-30 min) so nothing scrolls off the list between runs.
- Each news item is deduplicated by a stable ID (derived from its
  detail-page link when present, otherwise a hash of its symbol +
  headline) so re-running the script doesn't create duplicate rows.
"""

import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

URL = "https://scanx.trade/stock-market-news/news-feeds"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

TIME_AGO_RE = re.compile(r"\b\d+\s+(?:min|mins|hr|hrs|day|days)\s+ago\b", re.I)
PRICE_RE = re.compile(
    r"([\d,]+\.\d+)\s+([+-]?[\d,]+\.\d+)\s*\(([+-]?[\d,]+\.\d+)%\)"
)
SENTIMENT_MAP = {
    "feedpositive": "positive",
    "feedneutral": "neutral",
    "feednegative": "negative",
}


def fetch_html() -> str:
    resp = requests.get(URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def make_id(link: str | None, symbol: str, headline: str) -> str:
    """Stable ID: numeric suffix of the detail link if we have one,
    otherwise a hash of symbol+headline (headline text is stable even
    though the 'x mins ago' text next to it keeps changing)."""
    if link:
        m = re.search(r"/(\d+)/?$", link)
        if m:
            return m.group(1)
    raw = f"{symbol}|{headline}".strip().lower()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def parse_items(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    items = []

    # Anchor on each company logo image - one per news card.
    logo_imgs = soup.find_all("img", src=re.compile(r"images\.dhan\.co/symbol/"))

    for img in logo_imgs:
        # Walk up until we find a container that also has a sentiment
        # icon and a price line - that's the news card boundary.
        card = img
        sentiment = None
        for _ in range(8):
            if card.parent is None:
                break
            card = card.parent
            sent_img = card.find(
                "img", src=re.compile(r"feed(Positive|Neutral|Negative)\.svg", re.I)
            )
            if sent_img:
                m = re.search(r"feed(positive|neutral|negative)", sent_img["src"], re.I)
                if m:
                    sentiment = SENTIMENT_MAP.get(m.group(0).lower())
                if PRICE_RE.search(card.get_text(" ", strip=True)):
                    break

        if card is None:
            continue

        text = card.get_text(" ", strip=True)

        # Headline: prefer a link to a company news detail page.
        headline_link = card.find("a", href=re.compile(r"/stock-market-news/companies/"))
        if headline_link:
            headline = headline_link.get_text(strip=True)
            link = headline_link["href"]
            if link.startswith("/"):
                link = "https://scanx.trade" + link
        else:
            # No link - headline is usually the text right after the logo,
            # before the "x mins/hrs ago" marker.
            link = None
            m = TIME_AGO_RE.search(text)
            headline = text[: m.start()].strip() if m else None

        if not headline:
            continue

        # Strip a leading single-letter/logo-alt artifact if present.
        headline = re.sub(r"^[A-Z]\s+", "", headline).strip()

        # Symbol: link to the company profile page, e.g. /company/xxx-ltd
        symbol_link = card.find("a", href=re.compile(r"/company/"))
        symbol = symbol_link.get_text(strip=True) if symbol_link else None

        time_ago_match = TIME_AGO_RE.search(text)
        time_ago = time_ago_match.group(0) if time_ago_match else None

        price_match = PRICE_RE.search(text)
        price = change = pct_change = None
        if price_match:
            price, change, pct_change = price_match.groups()

        # Summary: longest sentence-like chunk of text in the card that
        # isn't the headline/time/price noise.
        candidates = [
            s.strip()
            for s in re.split(r"(?<=[.!?])\s+", text)
            if len(s.strip()) > 40
        ]
        summary = max(candidates, key=len) if candidates else None

        item_id = make_id(link, symbol or "", headline)

        items.append(
            {
                "id": item_id,
                "symbol": symbol,
                "headline": headline,
                "summary": summary,
                "sentiment": sentiment,
                "price": price,
                "change": change,
                "pct_change": pct_change,
                "time_ago_at_scrape": time_ago,
                "link": link,
            }
        )

    return items


def load_existing_ids(day_file: Path) -> set[str]:
    if not day_file.exists():
        return set()
    ids = set()
    with day_file.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ids.add(json.loads(line)["id"])
            except (json.JSONDecodeError, KeyError):
                continue
    return ids


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    day_file = DATA_DIR / f"news-{now:%Y-%m-%d}.jsonl"

    html = fetch_html()
    items = parse_items(html)

    if not items:
        print("No items parsed - the site's markup may have changed.", file=sys.stderr)
        return 1

    # Collapse duplicates found within THIS run (the site's markup can
    # render the same news card more than once - e.g. across hidden
    # tabs/breakpoints), keeping the first occurrence of each id.
    deduped_items = list({it["id"]: it for it in items}.values())

    existing_ids = load_existing_ids(day_file)
    new_items = [it for it in deduped_items if it["id"] not in existing_ids]

    if new_items:
        with day_file.open("a", encoding="utf-8") as f:
            for it in new_items:
                it["scraped_at"] = now.isoformat()
                f.write(json.dumps(it, ensure_ascii=False) + "\n")

    print(
        f"Parsed {len(items)} raw items on page ({len(deduped_items)} unique), "
        f"{len(new_items)} new, written to {day_file.name}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
