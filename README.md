# news-api

**A news feed you can fetch with one URL.**

[![Scrape latest data](https://github.com/pranav256/news-api/actions/workflows/main.yml/badge.svg)](https://github.com/pranav256/news-api/actions/workflows/main.yml)

Headlines from public RSS/Atom feeds, collected by Rust and published as static
JSON on GitHub. Five countries, seven categories, and a combined top-stories feed
for each country. Reading the feeds needs no API key, account, or backend server.

```bash
curl https://raw.githubusercontent.com/pranav256/news-api/main/in/all.json
```

- **Pick your coverage.** Read a category or get top stories across categories.
- **Newest first.** Up to 500 articles per output, deduplicated by URL.
- **Keep recent stories.** Successful non-empty refreshes retain up to seven days
  of history within that limit, even after stories leave a publisher's feed.
- **See what's fresh.** A status file tracks successful refreshes and provider failures.

## Start reading

Use either URL pattern:

```text
https://raw.githubusercontent.com/pranav256/news-api/main/{country}/{category}.json
https://raw.githubusercontent.com/pranav256/news-api/main/{country}/all.json
```

| Country | Code | Top stories |
| --- | --- | --- |
| India | `in` | [Read JSON](https://raw.githubusercontent.com/pranav256/news-api/main/in/all.json) |
| United States | `us` | [Read JSON](https://raw.githubusercontent.com/pranav256/news-api/main/us/all.json) |
| United Kingdom | `gb` | [Read JSON](https://raw.githubusercontent.com/pranav256/news-api/main/gb/all.json) |
| Canada | `ca` | [Read JSON](https://raw.githubusercontent.com/pranav256/news-api/main/ca/all.json) |
| Australia | `au` | [Read JSON](https://raw.githubusercontent.com/pranav256/news-api/main/au/all.json) |

Categories: `general`, `business`, `health`, `science`, `sports`, `technology`,
and `entertainment`. Use `all` for top stories across categories.

For example, load US technology headlines in JavaScript:

```js
const response = await fetch(
  "https://raw.githubusercontent.com/pranav256/news-api/main/us/technology.json"
);
if (!response.ok) throw new Error(`Feed request failed: ${response.status}`);
const { articles } = await response.json();
console.log(articles.map(({ title, url }) => ({ title, url })));
```

Public files use a NewsAPI-compatible response shape and preserve the paths used
by the original Flask application:

```json
{
  "status": "ok",
  "totalResults": 0,
  "articles": []
}
```

Articles include `source`, `author`, `title`, `description`, `url`, `urlToImage`,
`publishedAt`, and `content`. Fields can be `null` when a publisher omits them;
these are headline feeds, not a full-article archive.

## From publisher to JSON

Every hour, cron-job.org dispatches a GitHub Actions workflow. The workflow checks
Rust formatting and linting, builds the scraper, fetches sources with four network workers,
and commits the generated feeds when they change. The JSON files are the store:
there is no database to run.

Sources include NPR, The Guardian, The Conversation, BNN Bloomberg, MobileSyrup,
and Indian Express. The scraper reads up to 250 articles per publisher feed.
[sources.json](sources.json) contains the source configuration and is embedded in
the binary at build time.

Canadian health coverage also uses [CTV News's public health listing](https://www.ctvnews.ca/health/).
The scraper reads metadata embedded in the listing once per refresh and reuses
it across outputs. It does not fetch individual article bodies or run page scripts.
The `--rss-only` flag includes this listing and disables NewsAPI requests.

### When a source goes quiet

- Failed providers do not erase the last successful feed. If another provider
  succeeds, the category can still refresh.
- Successful non-empty refreshes merge recent history; current versions win
  when article URLs match. A successful empty refresh clears the feed.
- Invalid payloads and interrupted downloads count as failures, not empty feeds.
- Combined top stories include retained category feeds during partial outages.
- Writes are atomic. If no headline or deep feed refreshes, the run fails.
- Publishing stages only generated feeds and rebases over concurrent updates.
  A conflict stops publication without overwriting remote changes.

### Check freshness

Use [data/status.json](data/status.json) to check the last successful refresh,
rather than treating the latest commit or status generation time as proof that
every feed is current.

| File | What it contains |
| --- | --- |
| `{country}/{category}.json` | Public headline response |
| `{country}/all.json` | Combined top stories |
| `data/{country}/{category}_headlines.json` | Headlines with provider and timestamp metadata |
| `data/{country}/{category}_everything.json` | Category feed and its recent history in RSS-only mode |
| `data/status.json` | Refresh results, provider errors, and request usage |

<details>
<summary>Status fields for feed consumers</summary>

Status schema version 1 includes `generated_at`, `run_started_at`, RSS
attempted/succeeded counts, and `feeds_updated` / `feeds_retained`. Each headline
country/category entry in `feeds` records:

| Field | Meaning |
| --- | --- |
| `status` | `updated` after at least one successful provider, or `retained` when every provider failed |
| `attempted_at` | Time of the current run's attempted refresh |
| `last_successful_refresh` | New `fetched_at` after success, or the previous rich feed's timestamp when retained |
| `providers` | Providers for the published or retained feed |
| `articles_count` | Published or retained article count when available |

A successful empty feed has its true refresh time. Missing previous metadata
stays unavailable; older status files may omit these fields. The original public
response shape is unchanged.

Listing contributions use `publisher_listing` in `providers`. Status includes
`listing_pages_attempted`, `listing_pages_succeeded`, and `listing_pages_failed`;
listing errors appear in `errors` alongside RSS errors.

</details>

## Run your own refresh

Install a current stable Rust toolchain, then run:

```bash
cargo run --release --locked -- --rss-only --output-dir ./preview
```

The `preview` directory receives both public and rich JSON files. Omit
`--output-dir` to update the repository feeds. Rebuild after editing `sources.json`.

| Option | Use it to |
| --- | --- |
| `--rss-only` | Fetch public feeds and the CTV listing without NewsAPI keys |
| `--output-dir DIR` | Write feeds to another directory |
| `--workers 1..16` | Set network concurrency; default is `4`, `1` is sequential |
| `--dry-run` | Fetch and process feeds without writing files |

The scraper and hourly workflow require no Python installation. The former
Python scraper remains on the
[`python` branch](https://github.com/pranav256/news-api/tree/python) for reference.

Run the formatting and lint checks locally:

```bash
cargo fmt --check
cargo clippy --locked --all-targets -- -D warnings
```

<details>
<summary>Optional: use NewsAPI locally</summary>

The published workflow runs with `--rss-only` and does not read API secrets.
For local NewsAPI requests, omit that flag and configure non-empty unique keys
through `NEWSAPI_KEYS` (comma- or whitespace-separated), or `FIRSTAPI`,
`SECONDAPI`, `THIRDAPI`, `FOURTHAPI`, `FIFTHAPI`, `SIXTHAPI`, and `SEVENTHAPI`.

Requests are capped per run with a safety reserve. Keys rotate between retries
without printing their values. RSS requests do not consume the NewsAPI budget.
When keys and request budgets allow, these runs can merge headline and deep-search
results; RSS-only `everything` files do not perform keyword searches.

</details>

## Refresh schedule and maintenance

cron-job.org triggers `workflow_dispatch` hourly at **minute 17 UTC / minute 47
IST**. The built-in GitHub timer is disabled to avoid duplicate runs. Triggering
and runner delays can still occur, so hourly is a target, not a freshness guarantee.
You can also dispatch the workflow manually from GitHub Actions.

The scheduler uses a fine-grained GitHub token restricted to this repository
with Actions read/write permission. **Renew the token in cron-job.org's
Authorization header before December 28, 2026**, when the current token expires.
