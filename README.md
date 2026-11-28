# news-api

[![Scrape latest data](https://github.com/pranav256/news-api/actions/workflows/main.yml/badge.svg)](https://github.com/pranav256/news-api/actions/workflows/main.yml)

This repository publishes static news JSON files for a Flask application. The
scheduled scraper is written in Rust and uses public RSS/Atom feeds plus a
targeted publisher listing, without NewsAPI requests or API keys. The previous
Python scraper is preserved on the [`python` branch](https://github.com/pranav256/news-api/tree/python).
It covers five countries and seven categories. cron-job.org triggers the GitHub
Actions workflow hourly at minute 17 UTC (minute 47 IST) using `workflow_dispatch`.
The workflow commits only when the generated data changes. The built-in GitHub
schedule is disabled to avoid duplicate runs. Triggering and runner delays can
still occur, so this is a target rather than a freshness guarantee.

The scheduler uses a fine-grained GitHub token restricted to this repository with
Actions read/write permission. Renew its Authorization header in cron-job.org
before the token expires on December 28, 2026.

## Public API

The original Flask-compatible paths are preserved, alongside a combined top-stories feed:

```text
https://raw.githubusercontent.com/pranav256/news-api/main/{country}/{category}.json
https://raw.githubusercontent.com/pranav256/news-api/main/{country}/all.json
```

Countries:

| Country | Code |
| --- | --- |
| India | `in` |
| United States | `us` |
| United Kingdom | `gb` |
| Canada | `ca` |
| Australia | `au` |

Categories:

```text
all             # Top stories combined across all categories
general
business
health
science
sports
technology
entertainment
```

Examples:

```text
https://raw.githubusercontent.com/pranav256/news-api/main/us/general.json
https://raw.githubusercontent.com/pranav256/news-api/main/in/all.json
```

The root files keep a NewsAPI-compatible response shape:

```json
{
  "status": "ok",
  "totalResults": 0,
  "articles": []
}
```

## Rich data files

The `data/` directory includes provider and timestamp metadata:

```text
data/{country}/{category}_headlines.json
data/{country}/{category}_everything.json
data/status.json
```

The headline files merge multiple RSS/Atom publishers per category, including
Indian Express, NPR, The Guardian, The Conversation, BNN Bloomberg and MobileSyrup.
Each output contains up to 500 unique articles, newest first. Each publisher feed
is read up to 250 articles; the combined `all.json` includes category stories up
to the same 500-article limit.

Successful non-empty refreshes retain previously published stories from the last
seven days, so articles remain available after publishers remove them from their
current feed. Current versions win when URLs match. An explicitly empty successful
refresh clears the feed; total provider failure preserves the last successful file.
Existing files store this history, so no database is needed.

In RSS-only mode, `everything` files contain the category feeds and their own
recent history. They do not perform keyword searches. Optional local NewsAPI runs
can merge headline and deep-search results when keys and request budgets allow it.

## Targeted publisher scraping

Canadian health coverage also includes [CTV News's public health listing](https://www.ctvnews.ca/health/).
The scraper reads headline metadata already embedded in that page: title,
publication date, summary, image and article link. It requests the listing once
per refresh and reuses those results for headlines and `everything` files.
It does not request individual article bodies or run page scripts.

The `--rss-only` option means no NewsAPI requests; it also includes this public
listing. Malformed pages, missing dates and unrelated sections are rejected.
If the page changes or cannot be fetched, RSS coverage still publishes, and a
total provider outage preserves the previous feed. Listing contributions appear
as `publisher_listing` in `providers`. `data/status.json` reports
`listing_pages_attempted`, `listing_pages_succeeded` and `listing_pages_failed`
alongside RSS statistics, with listing errors included in `errors`.

## API keys

The scraper automatically detects any non-empty unique keys from:

```text
NEWSAPI_KEYS       # optional comma- or whitespace-separated list
FIRSTAPI
SECONDAPI
THIRDAPI
FOURTHAPI
FIFTHAPI
SIXTHAPI
SEVENTHAPI
```

GitHub Actions uses `--rss-only` and does not read API secrets. Optional local
NewsAPI runs use only configured keys. Requests are capped per run using the
100-requests-per-day Developer-plan budget and a safety reserve. RSS requests
do not consume the NewsAPI budget.

## Run locally

Build and run the Rust scraper with a current stable Rust toolchain:

```bash
cargo build --release --locked
cargo run --release --locked -- --rss-only --output-dir ./preview
```

The `preview` directory receives the public and rich JSON feeds. Omit
`--output-dir` to update the repository feeds. Rust uses four network workers
by default; `--workers 1` runs sequentially and `--dry-run` disables writes.
No Python installation or NewsAPI package is required to run the Rust binary.
Source configuration is embedded from `sources.json` at build time; rebuild after
changing it. `sources.json` is the source configuration for the Rust scraper.

Run the Rust checks and publishing-script regression test:

```bash
cargo fmt --check
cargo clippy --locked --all-targets -- -D warnings
cargo test --locked
python -B -m unittest discover -s tests -v
```

On 2026-11-25, RSS-only dry runs against the same 86 RSS feeds and CTV listing
took 17.0 seconds in Rust and 35.44 seconds in Python, about 2.1x faster. Rust
uses four network workers by default; Python fetches feeds sequentially. This was
one live sample, so network conditions affect the result. A recorded captured-
response comparison matched all article fields and publication times across
4,245 articles. Parsing took 0.5731 seconds in Python (median) and 0.0904 seconds
in Rust (mean), about 6.34x faster; both parser timings use five iterations.
The warm-cache GitHub Actions run completed in 57 seconds, refreshed all 40
feeds, and succeeded on all 86 RSS feeds plus the CTV listing.

The former Python scraper, its optional NewsAPI integration and its tests are
preserved on the `python` branch. Python is used on `main` only for the
publishing-script regression test; scheduled scraping runs the Rust binary.

## Data behavior

- Articles are deduplicated by URL.
- Files are written atomically.
- A provider failure does not erase the last successful file.
- Invalid provider payloads and interrupted downloads count as failures rather
  than successful empty feeds.
- Combined top stories include retained category feeds during partial outages.
- If no headline or deep feed refreshes, the scraper exits with an error so
  GitHub Actions reports failure and leaves the published data intact.
- API keys are rotated between retries without printing their values.
- `data/status.json` records request usage and provider failures.
- Publishing commits only generated feeds and rebases over concurrent branch
  updates. Conflicting changes stop publication without overwriting remote data.

## Freshness and retained feeds

Status schema version 1 adds `generated_at` (status production time),
`run_started_at`, RSS attempted/succeeded counts, and `feeds_updated` /
`feeds_retained`. The `feeds` array records each headline country/category:

| Field | Meaning |
| --- | --- |
| `status` | `updated` after at least one successful provider, or `retained` when every provider failed |
| `attempted_at` | Time of the current run's attempted refresh |
| `last_successful_refresh` | Successful new `fetched_at`, or the previous rich feed's timestamp when retained |
| `providers` | Providers for the published or retained feed |
| `articles_count` | Published/retained article count when available |

An unsuccessful refresh leaves both public feed files intact. A successful empty
feed is published as empty with its true refresh time. Missing previous metadata
stays unavailable; status production time is not a successful feed timestamp.
Older status files may not have these fields, and consumers should tolerate that.
These changes do not replace the original NewsAPI-compatible root response.

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

Rust tests cover RSS/Atom parsing, provider failures, retained history, atomic
writes and API-key budgets. The Python standard-library test runs the publishing
script against local Git repositories; neither test suite fetches live feeds or
consumes API requests. GitHub Actions runs both before scraping.
