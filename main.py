"""Build the static news feeds published by this repository.

NewsAPI is useful for the US top-headlines endpoint and for deeper searches,
but its country endpoint does not provide useful results for every country.
RSS feeds are therefore collected for every country/category and merged with
NewsAPI where available.  The root ``{country}/{category}.json`` files keep
the original NewsAPI-compatible contract used by the Flask client, while the
``data/`` files contain fetch metadata and the deeper feeds.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Iterable

try:
    from newsapi import NewsApiClient
except ImportError:  # RSS-only local runs remain possible without the package.
    NewsApiClient = None  # type: ignore[assignment,misc]


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

COUNTRIES = ("in", "us", "gb", "ca", "au")
CATEGORIES = (
    "general",
    "business",
    "health",
    "science",
    "sports",
    "technology",
    "entertainment",
)
EVERYTHING_CATEGORIES = CATEGORIES

EVERYTHING_QUERIES = {
    "general": "breaking OR news OR politics OR world OR national",
    "business": "market OR economy OR finance OR stock OR business",
    "health": "medical OR health OR research OR wellness OR disease",
    "science": "science OR research OR discovery OR space OR astronomy",
    "sports": "sports OR match OR tournament OR score OR cricket OR football",
    "technology": "technology OR AI OR software OR startup OR tech",
    "entertainment": "entertainment OR movie OR cinema OR music OR film",
}

# The Developer plan allows 100 requests/day per key.  RSS requests do not
# count against this budget, so they are used for country/category coverage.
REQUESTS_PER_KEY_PER_DAY = 100
RUNS_PER_DAY = 12  # GitHub Actions runs every two hours.
BUDGET_SAFETY_FACTOR = 0.95
MAX_RETRIES = 2
HEADLINE_PAGE_SIZE = 100
RSS_TIMEOUT_SECONDS = int(os.environ.get("RSS_TIMEOUT_SECONDS", "20"))
MAX_FEED_BYTES = 4 * 1024 * 1024
MAX_ARTICLES_PER_FEED = 100
MAX_ARTICLES_PER_OUTPUT = 100

LEGACY_KEY_ENV_NAMES = (
    "FIRSTAPI",
    "SECONDAPI",
    "THIRDAPI",
    "FOURTHAPI",
    "FIFTHAPI",
    "SIXTHAPI",
    "SEVENTHAPI",
)

# NewsAPI top-headlines are enabled for all supported countries.
# If a category returns empty results from NewsAPI, RSS feeds provide coverage.
NEWSAPI_HEADLINE_COUNTRIES = {"in", "us", "gb", "ca", "au"}

COUNTRY_SOURCES = {
    "in": "the-times-of-india,the-hindu,ndtv,india-today,business-standard",
    "us": "cnn,fox-news,nbc-news,abc-news,cbs-news,usa-today,the-washington-post",
    "gb": "bbc-news,the-guardian,independent,telegraph,daily-mail,reuters",
    "ca": "cbc-news,the-globe-and-mail,financial-post",
    "au": "abc-news-au,news-com-au",
}


# (display name, feed URL).  Configured with multi-publisher redundancy so a
# failure in one source does not drop category coverage.
RSS_FEEDS: dict[str, dict[str, list[tuple[str, str]]]] = {
    "in": {
        "general": [
            ("NDTV", "https://feeds.feedburner.com/ndtvnews-latest"),
            ("The Hindu", "https://www.thehindu.com/news/national/feeder/default.rss"),
            ("Times of India", "https://timesofindia.indiatimes.com/rssfeedstopstories.cms"),
        ],
        "business": [
            ("The Hindu", "https://www.thehindu.com/business/feeder/default.rss"),
            ("Times of India", "https://timesofindia.indiatimes.com/rssfeeds/1898055.cms"),
        ],
        "health": [
            ("The Hindu", "https://www.thehindu.com/sci-tech/health/feeder/default.rss"),
        ],
        "science": [
            ("The Hindu", "https://www.thehindu.com/sci-tech/science/feeder/default.rss"),
        ],
        "sports": [
            ("The Hindu", "https://www.thehindu.com/sport/feeder/default.rss"),
            ("Times of India", "https://timesofindia.indiatimes.com/rssfeeds/4719148.cms"),
            ("NDTV Sports", "https://feeds.feedburner.com/ndtvsports-latest"),
        ],
        "technology": [
            ("The Hindu", "https://www.thehindu.com/sci-tech/technology/feeder/default.rss"),
            ("Times of India", "https://timesofindia.indiatimes.com/rssfeeds/66949542.cms"),
            ("NDTV Gadgets", "https://feeds.feedburner.com/gadgets360-latest"),
        ],
        "entertainment": [
            ("The Hindu", "https://www.thehindu.com/entertainment/feeder/default.rss"),
            ("NDTV Movies", "https://feeds.feedburner.com/ndtvmovies-latest"),
        ],
    },
    "us": {
        "general": [
            ("The New York Times", "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml"),
            ("NPR News", "https://feeds.npr.org/1001/rss.xml"),
        ],
        "business": [
            ("The New York Times", "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml"),
        ],
        "health": [
            ("The New York Times", "https://rss.nytimes.com/services/xml/rss/nyt/Health.xml"),
        ],
        "science": [
            ("The New York Times", "https://rss.nytimes.com/services/xml/rss/nyt/Science.xml"),
        ],
        "sports": [
            ("The New York Times", "https://rss.nytimes.com/services/xml/rss/nyt/Sports.xml"),
            ("CBS Sports", "https://www.cbssports.com/rss/headlines/"),
            ("Yahoo Sports", "https://sports.yahoo.com/rss/"),
        ],
        "technology": [
            ("The New York Times", "https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml"),
            ("Wired", "https://www.wired.com/feed/rss"),
        ],
        "entertainment": [
            ("The New York Times", "https://rss.nytimes.com/services/xml/rss/nyt/Arts.xml"),
        ],
    },
    "gb": {
        "general": [
            ("BBC News", "https://feeds.bbci.co.uk/news/rss.xml"),
            ("The Guardian", "https://www.theguardian.com/uk/rss"),
        ],
        "business": [
            ("BBC News", "https://feeds.bbci.co.uk/news/business/rss.xml"),
            ("The Guardian", "https://www.theguardian.com/business/rss"),
        ],
        "health": [
            ("BBC News", "https://feeds.bbci.co.uk/news/health/rss.xml"),
        ],
        "science": [
            ("BBC News", "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml"),
            ("The Guardian", "https://www.theguardian.com/science/rss"),
        ],
        "sports": [
            ("BBC Sport", "https://feeds.bbci.co.uk/sport/rss.xml"),
            ("The Guardian", "https://www.theguardian.com/sport/rss"),
        ],
        "technology": [
            ("BBC News", "https://feeds.bbci.co.uk/news/technology/rss.xml"),
            ("The Guardian", "https://www.theguardian.com/technology/rss"),
        ],
        "entertainment": [
            ("BBC News", "https://feeds.bbci.co.uk/news/entertainment_and_arts/rss.xml"),
        ],
    },
    "ca": {
        "general": [
            ("Global News", "https://globalnews.ca/feed/"),
            ("Global National", "https://globalnews.ca/national/feed/"),
        ],
        "business": [("Global News", "https://globalnews.ca/money/feed/")],
        "health": [("Global News", "https://globalnews.ca/tag/health/feed/")],
        "science": [("Global News", "https://globalnews.ca/tag/science/feed/")],
        "sports": [
            ("Sportsnet", "https://www.sportsnet.ca/feed/"),
            ("Global News", "https://globalnews.ca/sports/feed/"),
        ],
        "technology": [("Global News", "https://globalnews.ca/tag/technology/feed/")],
        "entertainment": [("Global News", "https://globalnews.ca/entertainment/feed/")],
    },
    "au": {
        "general": [
            ("ABC News Australia", "https://www.abc.net.au/news/feed/51120/rss.xml"),
            ("The Sydney Morning Herald", "https://www.smh.com.au/rss/feed.xml"),
        ],
        "business": [
            ("ABC News Australia", "https://www.abc.net.au/news/feed/51892/rss.xml"),
            ("The Sydney Morning Herald", "https://www.smh.com.au/rss/business.xml"),
        ],
        "health": [("ABC News Australia", "https://www.abc.net.au/news/feed/492/rss.xml")],
        "science": [("ABC News Australia", "https://www.abc.net.au/news/feed/2090/rss.xml")],
        "sports": [
            ("ABC News Australia", "https://www.abc.net.au/news/feed/45924/rss.xml"),
            ("The Sydney Morning Herald", "https://www.smh.com.au/rss/sport.xml"),
        ],
        "technology": [("ABC News Australia", "https://www.abc.net.au/news/feed/2942460/rss.xml")],
        "entertainment": [
            ("The Sydney Morning Herald", "https://www.smh.com.au/rss/lifestyle.xml"),
            ("The Age", "https://www.theage.com.au/rss/culture.xml"),
        ],
    },
}


@dataclass
class RequestBudget:
    """Hard cap on actual NewsAPI HTTP requests made during one run."""

    limit: int
    used: int = 0

    def consume(self) -> bool:
        if self.used >= self.limit:
            return False
        self.used += 1
        return True

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)


@dataclass
class KeyPool:
    keys: list[str]
    next_start: int = 0
    disabled: set[int] = field(default_factory=set)

    def next_indices(self) -> list[int]:
        """Return each key once, rotated between logical requests."""
        if not self.keys:
            return []
        start = self.next_start % len(self.keys)
        self.next_start = (start + 1) % len(self.keys)
        return [(start + offset) % len(self.keys) for offset in range(len(self.keys))]


@dataclass
class RSSResult:
    articles: list[dict[str, Any]] = field(default_factory=list)
    attempted: int = 0
    succeeded: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class ScrapeState:
    key_pool: KeyPool
    budget: RequestBudget
    api_failures: list[str] = field(default_factory=list)
    rss_failures: list[str] = field(default_factory=list)
    files_updated: list[str] = field(default_factory=list)
    feed_outcomes: list[dict[str, Any]] = field(default_factory=list)


def load_api_keys(environ: dict[str, str] | None = None) -> list[str]:
    """Load any configured unique keys, supporting old and new secret names."""
    environ = os.environ if environ is None else environ
    candidates: list[str] = []

    combined = environ.get("NEWSAPI_KEYS", "")
    candidates.extend(part for part in re.split(r"[,\s]+", combined) if part)
    candidates.extend(environ.get(name, "") for name in LEGACY_KEY_ENV_NAMES)

    unique: list[str] = []
    seen: set[str] = set()
    for key in candidates:
        key = key.strip()
        if key and key not in seen:
            seen.add(key)
            unique.append(key)
    return unique


def calculate_run_budget(key_count: int) -> int:
    daily_budget = key_count * REQUESTS_PER_KEY_PER_DAY
    return int(daily_budget * BUDGET_SAFETY_FACTOR // RUNS_PER_DAY)


def save_json_atomic(data: Any, filepath: Path, dry_run: bool = False) -> None:
    """Write JSON atomically so an interrupted job cannot corrupt a feed."""
    if dry_run:
        return
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=filepath.parent, delete=False
    ) as temporary:
        json.dump(data, temporary, ensure_ascii=False, indent=2)
        temporary.write("\n")
        temporary_path = Path(temporary.name)
    temporary_path.replace(filepath)


def _clean_text(value: str | None) -> str | None:
    if not value:
        return None
    value = html.unescape(value)
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value or None


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].split(":")[-1].lower()


def _child_text(element: ET.Element, names: set[str]) -> str | None:
    for child in list(element):
        if _local_name(child.tag) in names:
            return child.text or ""
    return None


def _child_attribute(element: ET.Element, names: set[str], attribute: str) -> str | None:
    for child in list(element):
        if _local_name(child.tag) in names:
            value = child.attrib.get(attribute)
            if value:
                return value
    return None


def _normalise_date(value: str | None, fallback: str) -> str:
    if not value:
        return fallback
    value = value.strip()
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _source_id(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "rss"


def parse_rss(xml_data: str | bytes, source_name: str, fetched_at: str) -> list[dict[str, Any]]:
    """Parse RSS 2.0 or Atom into NewsAPI-shaped article objects."""
    root = ET.fromstring(xml_data)
    entries = [
        element
        for element in root.iter()
        if _local_name(element.tag) in {"item", "entry"}
    ]
    articles: list[dict[str, Any]] = []

    for entry in entries[:MAX_ARTICLES_PER_FEED]:
        title = _clean_text(_child_text(entry, {"title"}))
        link = _child_text(entry, {"link"})
        link = link.strip() if link else None
        link = link or _child_attribute(entry, {"link"}, "href")
        if not title or not link:
            continue

        raw_desc = _child_text(entry, {"description", "summary", "encoded", "content"})
        description = _clean_text(raw_desc)
        image_url = _child_attribute(entry, {"content", "thumbnail", "enclosure"}, "url")
        if not image_url:
            image_url = _child_attribute(entry, {"content", "thumbnail", "enclosure"}, "href")
        if not image_url and raw_desc and "<img" in raw_desc:
            img_match = re.search(r'<img[^>]+src=["\'](https?://[^"\']+)["\']', raw_desc, re.IGNORECASE)
            if img_match:
                image_url = img_match.group(1)
        published = _child_text(entry, {"pubdate", "published", "updated", "date"})

        articles.append(
            {
                "source": {"id": _source_id(source_name), "name": source_name},
                "author": _clean_text(_child_text(entry, {"author", "creator"})),
                "title": title,
                "description": description,
                "url": link,
                "urlToImage": image_url,
                "publishedAt": _normalise_date(published, fetched_at),
                "content": description,
            }
        )
    return articles


def deduplicate(articles: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove duplicate articles by URL, with a title/date fallback."""
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for article in articles:
        url = (article.get("url") or "").strip()
        fallback = "|".join(
            str(article.get(field) or "") for field in ("title", "publishedAt")
        )
        key = url or fallback
        if key and key not in seen:
            seen.add(key)
            unique.append(article)
    unique.sort(key=lambda article: article.get("publishedAt") or "", reverse=True)
    return unique[:MAX_ARTICLES_PER_OUTPUT]


def fetch_rss_feed(url: str, source_name: str, fetched_at: str) -> list[dict[str, Any]]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 (compatible; news-api-static-feed/2.0; +https://github.com/pranav256/news-api)",
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
        },
    )
    with urllib.request.urlopen(request, timeout=RSS_TIMEOUT_SECONDS) as response:
        xml_data = response.read(MAX_FEED_BYTES)
    return parse_rss(xml_data, source_name, fetched_at)


def fetch_rss_articles(country: str, category: str, fetched_at: str, state: ScrapeState) -> RSSResult:
    feeds = RSS_FEEDS.get(country, {}).get(category, [])
    result = RSSResult()
    for source_name, url in feeds:
        result.attempted += 1
        try:
            result.articles.extend(fetch_rss_feed(url, source_name, fetched_at))
            result.succeeded += 1
        except (ET.ParseError, OSError, urllib.error.URLError, ValueError) as exc:
            message = f"{country}/{category} {source_name}: {exc}"
            result.errors.append(message)
            state.rss_failures.append(message)
            print(f"    RSS failed: {message}")
    result.articles = deduplicate(result.articles)
    return result


def _error_code(result: Any = None, exception: Exception | None = None) -> str:
    if isinstance(result, dict) and result.get("code"):
        return str(result["code"])
    text = str(exception or result or "")
    known_codes = (
        "apiKeyDisabled",
        "apiKeyExhausted",
        "apiKeyInvalid",
        "apiKeyMissing",
        "parameterInvalid",
        "parametersMissing",
        "rateLimited",
        "sourceDoesNotExist",
        "sourcesTooMany",
        "unexpectedError",
    )
    return next((code for code in known_codes if code in text), "")


def _is_permanent_request_error(code: str, exception: Exception | None) -> bool:
    if code in {"parameterInvalid", "parametersMissing", "sourceDoesNotExist", "sourcesTooMany"}:
        return True
    if isinstance(exception, (TypeError, ValueError)):
        return True
    return False


def fetch_with_retry(
    fetch_fn: Callable[[Any], dict[str, Any]],
    label: str,
    state: ScrapeState,
) -> dict[str, Any] | None:
    """Try distinct configured keys while counting every real API request."""
    if not state.key_pool.keys:
        return None
    if NewsApiClient is None:
        state.api_failures.append(f"{label}: newsapi-python is not installed")
        return None

    attempts = 0
    for key_index in state.key_pool.next_indices():
        if attempts >= MAX_RETRIES + 1:
            break
        if key_index in state.key_pool.disabled:
            continue
        if not state.budget.consume():
            print(f"    {label}: request budget limit reached ({state.budget.limit})")
            return None

        attempts += 1
        try:
            result = fetch_fn(NewsApiClient(api_key=state.key_pool.keys[key_index]))
            if result and result.get("status") == "ok":
                return result

            code = _error_code(result)
            if code in {"apiKeyDisabled", "apiKeyExhausted", "apiKeyInvalid", "apiKeyMissing", "rateLimited"}:
                state.key_pool.disabled.add(key_index)
            message = result.get("message", "unknown NewsAPI error") if result else "empty response"
            print(f"    {label} key#{key_index + 1} attempt {attempts}: {message}")
            if _is_permanent_request_error(code, None):
                break
        except Exception as exc:  # NewsAPIException varies between package versions.
            code = _error_code(exception=exc)
            if code in {"apiKeyDisabled", "apiKeyExhausted", "apiKeyInvalid", "apiKeyMissing", "rateLimited"}:
                state.key_pool.disabled.add(key_index)
            message = f"{type(exc).__name__}: {exc}"
            print(f"    {label} key#{key_index + 1} attempt {attempts}: {message}")
            if _is_permanent_request_error(code, exc):
                break

        if attempts <= MAX_RETRIES:
            time.sleep(0.25)

    state.api_failures.append(f"{label}: all attempts failed")
    return None


def fetch_newsapi_headlines(
    country: str, category: str, state: ScrapeState
) -> dict[str, Any] | None:
    if country not in NEWSAPI_HEADLINE_COUNTRIES:
        return None

    def request(api: Any) -> dict[str, Any]:
        return api.get_top_headlines(
            category=category,
            country=country,
            page_size=HEADLINE_PAGE_SIZE,
            page=1,
        )

    return fetch_with_retry(request, f"{country}/{category}/headlines", state)


def fetch_newsapi_everything(
    country: str, category: str, query: str, state: ScrapeState
) -> dict[str, Any] | None:
    sources = COUNTRY_SOURCES.get(country, "")

    def request(api: Any) -> dict[str, Any]:
        return api.get_everything(
            q=query,
            sources=sources or None,
            language="en",
            page_size=HEADLINE_PAGE_SIZE,
            page=1,
            sort_by="publishedAt",
        )

    return fetch_with_retry(request, f"{country}/{category}/everything", state)


def response_articles(response: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not response or response.get("status") != "ok":
        return []
    articles = response.get("articles")
    if not isinstance(articles, list):
        return []
    return [article for article in articles if isinstance(article, dict) and article.get("url")]


def root_response(articles: list[dict[str, Any]]) -> dict[str, Any]:
    """Keep the original response shape expected by the Flask application."""
    return {
        "status": "ok",
        "totalResults": len(articles),
        "articles": articles,
    }


def data_response(
    *,
    source: str,
    country: str,
    category: str,
    fetched_at: str,
    articles: list[dict[str, Any]],
    providers: list[str],
    query: str | None = None,
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "source": source,
        "country": country,
        "category": category,
        "providers": providers,
        "fetched_at": fetched_at,
        "articles_count": len(articles),
        "articles": articles,
    }
    if query is not None:
        response["query"] = query
    return response


def write_feed_pair(
    *,
    country: str,
    category: str,
    articles: list[dict[str, Any]],
    data_payload: dict[str, Any],
    provider_succeeded: bool,
    state: ScrapeState,
    dry_run: bool,
) -> None:
    """Write a feed only when at least one provider responded successfully."""
    root_path = BASE_DIR / country / f"{category}.json"
    data_path = DATA_DIR / country / f"{category}_headlines.json"
    previous: dict[str, Any] = {}
    if not provider_succeeded:
        try:
            payload = json.loads(data_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                previous = payload
        except (OSError, ValueError):
            pass
    state.feed_outcomes.append({
        "country": country,
        "category": category,
        "status": "updated" if provider_succeeded else "retained",
        "attempted_at": data_payload["fetched_at"],
        "last_successful_refresh": data_payload["fetched_at"] if provider_succeeded else previous.get("fetched_at"),
        "providers": data_payload["providers"] if provider_succeeded else previous.get("providers", []),
        "articles_count": len(articles) if provider_succeeded else previous.get("articles_count"),
    })
    if not provider_succeeded:
        print(f"    keeping previous {country}/{category} files: all providers failed")
        return

    save_json_atomic(root_response(articles), root_path, dry_run)
    save_json_atomic(data_payload, data_path, dry_run)
    if not dry_run:
        state.files_updated.extend([str(root_path.relative_to(BASE_DIR)), str(data_path.relative_to(BASE_DIR))])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch and publish static news feeds")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch and report results without changing JSON files",
    )
    parser.add_argument(
        "--rss-only",
        action="store_true",
        help="skip NewsAPI and use RSS feeds only",
    )
    args = parser.parse_args(argv)

    fetched_at = datetime.now(timezone.utc).isoformat()
    keys = [] if args.rss_only else load_api_keys()
    run_budget = calculate_run_budget(len(keys))
    state = ScrapeState(KeyPool(keys), RequestBudget(run_budget))

    print("=" * 64)
    print("  NEWS SCRAPER — NEWSAPI + RSS")
    print("=" * 64)
    print(f"  API keys detected : {len(keys)}")
    print(f"  NewsAPI budget    : {run_budget} requests this run")
    print(f"  Schedule target   : {RUNS_PER_DAY} runs/day")
    print(f"  RSS countries     : {list(COUNTRIES)}")
    if not keys:
        print("  NewsAPI status    : RSS-only (no keys configured)")
    elif NewsApiClient is None:
        print("  NewsAPI status    : unavailable; install requirements.txt")
    print("=" * 64)

    rss_cache: dict[tuple[str, str], RSSResult] = {}
    print("\nPHASE 1: RSS feeds and headline-compatible files")
    print("-" * 64)
    for country in COUNTRIES:
        country_headlines: list[dict[str, Any]] = []
        for category in CATEGORIES:
            print(f"  [{country}/{category}]", end="", flush=True)
            rss_result = fetch_rss_articles(country, category, fetched_at, state)
            rss_cache[(country, category)] = rss_result

            if state.budget.remaining == 0 and state.key_pool.keys:
                print(" (budget limit reached, using RSS)", end="", flush=True)
                newsapi_response = None
            else:
                newsapi_response = fetch_newsapi_headlines(country, category, state)

            api_articles = response_articles(newsapi_response)
            articles = deduplicate(api_articles + rss_result.articles)
            country_headlines.extend(articles[:10])

            providers: list[str] = []
            if newsapi_response is not None and api_articles:
                providers.append("newsapi")
            if rss_result.succeeded:
                providers.append("rss")
            provider_succeeded = bool(providers)
            print(
                f" {len(articles)} articles "
                f"(NewsAPI: {len(api_articles)}, RSS: {len(rss_result.articles)})"
            )

            write_feed_pair(
                country=country,
                category=category,
                articles=articles,
                data_payload=data_response(
                    source="top_headlines",
                    country=country,
                    category=category,
                    fetched_at=fetched_at,
                    articles=articles,
                    providers=providers,
                ),
                provider_succeeded=provider_succeeded,
                state=state,
                dry_run=args.dry_run,
            )

        # Generate combined top stories across categories for this country
        combined_articles = deduplicate(country_headlines)
        if combined_articles:
            print(f"  [{country}/all] {len(combined_articles)} combined top stories")
            write_feed_pair(
                country=country,
                category="all",
                articles=combined_articles,
                data_payload=data_response(
                    source="top_headlines_all",
                    country=country,
                    category="all",
                    fetched_at=fetched_at,
                    articles=combined_articles,
                    providers=["combined"],
                ),
                provider_succeeded=True,
                state=state,
                dry_run=args.dry_run,
            )

    print("\nPHASE 2: Deep category feeds")
    print("-" * 64)
    for country in COUNTRIES:
        for category in EVERYTHING_CATEGORIES:
            query = EVERYTHING_QUERIES[category]
            print(f"  [{country}/{category}] q='{query}'", end="", flush=True)
            rss_result = rss_cache.get((country, category)) or RSSResult()
            if state.budget.remaining == 0 and state.key_pool.keys:
                print(" (budget limit reached, using RSS)", end="", flush=True)
                newsapi_response = None
            else:
                newsapi_response = fetch_newsapi_everything(country, category, query, state)
            api_articles = response_articles(newsapi_response)
            articles = deduplicate(api_articles + rss_result.articles)
            providers: list[str] = []
            if newsapi_response is not None and api_articles:
                providers.append("newsapi")
            if rss_result.succeeded:
                providers.append("rss")
            if newsapi_response is not None and api_articles:
                source = "everything"
            else:
                source = "rss_fallback"
            provider_succeeded = bool(providers)
            print(f" {len(articles)} articles (NewsAPI: {len(api_articles)}, RSS: {len(rss_result.articles)})")

            if not provider_succeeded:
                print(f"    keeping previous {country}/{category}_everything.json: all providers failed")
                continue
            payload = data_response(
                source=source,
                country=country,
                category=category,
                fetched_at=fetched_at,
                articles=articles,
                providers=providers,
                query=query,
            )
            path = DATA_DIR / country / f"{category}_everything.json"
            save_json_atomic(payload, path, args.dry_run)
            if not args.dry_run:
                state.files_updated.append(str(path.relative_to(BASE_DIR)))

    status = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_started_at": fetched_at,
        "status": "ok" if not state.api_failures and not state.rss_failures else "partial",
        "api_keys_detected": len(keys),
        "newsapi_requests_used": state.budget.used,
        "newsapi_requests_allowed": state.budget.limit,
        "rss_feeds_failed": len(state.rss_failures),
        "api_requests_failed": len(state.api_failures),
        "rss_feeds_attempted": sum(result.attempted for result in rss_cache.values()),
        "rss_feeds_succeeded": sum(result.succeeded for result in rss_cache.values()),
        "feeds_updated": sum(feed["status"] == "updated" for feed in state.feed_outcomes),
        "feeds_retained": sum(feed["status"] == "retained" for feed in state.feed_outcomes),
        "feeds": state.feed_outcomes,
        "files_updated": state.files_updated,
        "errors": state.api_failures + state.rss_failures,
    }
    save_json_atomic(status, DATA_DIR / "status.json", args.dry_run)

    print("\n" + "=" * 64)
    print(f"  DONE — {state.budget.used}/{state.budget.limit} NewsAPI requests used")
    print(f"  RSS failures: {len(state.rss_failures)}")
    print(f"  API failures: {len(state.api_failures)}")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
