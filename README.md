# ScanX News Flash scraper

Automatically pulls the "News Flash" feed from
https://scanx.trade/stock-market-news/news-feeds every 30 minutes and
saves new items to `data/news-YYYY-MM-DD.jsonl` (one JSON object per
line, one file per day). Already-seen items are skipped, so files
only grow with genuinely new news.

## How it works
- `scraper/scrape_scanx_news.py` fetches the page's HTML directly
  (it's server-rendered, so no browser automation is needed) and
  parses out each news card: symbol, headline, summary, sentiment,
  price/change, and link.
- `.github/workflows/scanx-news.yml` runs that script on a schedule
  using GitHub Actions (free) and commits any new rows back to this
  repo.
- The site only shows a rolling window of recent news (no historical
  archive), so data only starts accumulating from whenever you turn
  this on - it can't backfill the past.

## Setup
1. Create a new GitHub repository and push this folder to it.
   - **Public repo** = GitHub Actions minutes are unlimited/free.
   - **Private repo** = free up to 2,000 minutes/month on GitHub's
     Free plan (running every 30 min uses roughly 1,400-1,500 min/month,
     comfortably inside that).
2. In the repo, go to **Settings > Actions > General > Workflow
   permissions** and make sure "Read and write permissions" is
   selected (needed so the workflow can commit new data).
3. That's it - the workflow will start running automatically every 30
   minutes. You can also trigger it manually from the **Actions** tab
   ("Run workflow") to test it right away instead of waiting.

## A note on the first run
I built the parser based on the page's visible structure, but I
wasn't able to test it against the live HTML from this environment
(no network access to the site from here). The first run or two
might need small tweaks to the parsing logic in
`scrape_scanx_news.py` if ScanX's markup differs from what I assumed
- check the Action's run log (it prints how many items it found) and
let me know what you see if it looks off.

## Reading the data later
Each line in a `data/news-*.jsonl` file is one JSON object, e.g.:

```json
{"id": "50694086", "symbol": "RPSGVENT", "headline": "RPSG Ventures AGM: Key Resolutions Approved", "summary": "...", "sentiment": "neutral", "price": "0.0", "change": "0.0", "pct_change": "0.0", "time_ago_at_scrape": "39 mins ago", "link": "https://scanx.trade/...", "scraped_at": "2026-09-12T08:30:00+00:00"}
```

This is easy to load into pandas/Excel later if you want to browse or
filter it (e.g. `pd.read_json("data/news-2026-09-12.jsonl", lines=True)`).
