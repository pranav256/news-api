# news-api

[![Scrape latest data](https://github.com/pranav256/news-api/actions/workflows/main.yml/badge.svg)](https://github.com/pranav256/news-api/actions/workflows/main.yml)

This repository publishes static news JSON files for a Flask application. The
GitHub Actions workflow requests a refresh every two hours at minute 17 UTC and
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

The headline files combine NewsAPI and RSS articles. NewsAPI is queried for
top headlines across all supported countries and deeper searches across all
categories when the request budget allows it. Multi-publisher RSS feeds provide resilient
fallback coverage and merge additional articles for comprehensive reporting.

The `everything` files contain NewsAPI deep-search results with flexible `OR` boolean queries
across all 7 categories, falling back to merged RSS category feeds when needed.

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

The GitHub Actions workflow passes the legacy secret names for compatibility.
Only configured keys are used. NewsAPI requests are capped per run using the
100-requests-per-day Developer-plan budget and a safety reserve. RSS requests
do not consume the NewsAPI budget.

## Run locally

Install the pinned dependency:

```bash
python -m pip install -r requirements.txt
```

Run with the configured NewsAPI keys:

```bash
python main.py
```

Run without NewsAPI, using only RSS feeds:

```bash
python main.py --rss-only
```

Fetch and print results without writing files:

```bash
python main.py --dry-run
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
combined feeds during outages, total ingestion failure, and successful empty
feeds. Tests use local fixtures; they do not run live ingestion or consume API
request budgets. GitHub Actions runs them before scraping.
