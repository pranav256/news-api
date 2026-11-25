# news-api

[![Scrape latest data](https://github.com/pranav256/news-api/actions/workflows/main.yml/badge.svg)](https://github.com/pranav256/news-api/actions/workflows/main.yml)

This repository publishes static news JSON files for a Flask application. The
scheduled scraper uses public RSS/Atom feeds and a targeted publisher listing without API keys.
This experimental branch runs the Rust port; Python remains available as the comparison baseline.
It covers five countries and seven categories. The GitHub Actions
workflow requests a refresh every two hours at minute 17 UTC and
commits only when the generated data changes. GitHub may delay or drop scheduled
runs, so this is a target rather than a freshness guarantee.

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

Build and run the Rust experiment with a current stable Rust toolchain:

```bash
cargo build --release --locked
cargo run --release --locked -- --rss-only --output-dir ./preview
```

The `preview` directory receives the same public and rich JSON paths as Python.
Omit `--output-dir` to update the repository feeds. Rust uses four network workers
by default; `--workers 1` runs sequentially and `--dry-run` disables writes.
No Python installation or NewsAPI package is required to run the Rust binary.
Source configuration is embedded from `sources.json` at build time; rebuild after
changing it. The source-parity test keeps this configuration aligned with Python.

Verify the experiment and compare both parsers on identical live responses:

```bash
cargo fmt --check
cargo clippy --locked --all-targets -- -D warnings
cargo test --locked
python -B -m unittest discover -s tests -v
cargo build --release --locked --examples
python -B benchmarks/compare.py
```

The local validation on 2026-11-25 fetched all 86 RSS feeds and the CTV listing,
refreshed all 40 headline feeds, and wrote 116 valid JSON files with no provider
errors in 19.77 seconds. A separate captured-response comparison matched all
article fields and publication times across 4,245 articles. Parsing took
0.5731 seconds in Python (median) and 0.0904 seconds in Rust (mean), about 6.34x
faster. Both timings use five iterations. This measures parsing only; most of
the live run is network time, so it does not establish a 6.34x overall speedup.

Run using Python 3.10 or newer, without installing dependencies:

```bash
python main.py --rss-only
```

To optionally enable NewsAPI locally, install the pinned dependency:

```bash
python -m pip install -r requirements.txt
```

Run with the configured NewsAPI keys:

```bash
python main.py
```

Fetch RSS feeds and print results without writing files:

```bash
python main.py --rss-only --dry-run
```

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

The suite covers malformed provider collections, retained files/timestamps,
recent article history and its limits, combined feeds during outages, total
ingestion failure, and successful empty feeds. Tests use local fixtures;
they do not run live ingestion or consume API
request budgets. GitHub Actions runs them before scraping.
