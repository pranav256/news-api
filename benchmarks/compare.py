"""Compare Python and Rust parsers using the exact same captured publisher responses.

Build first: cargo build --release --locked --examples
Run: python benchmarks/compare.py
"""
import concurrent.futures
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import main  # noqa: E402


def compare():
    jobs = [
        {"country": country, "category": category, "source": source, "url": url, "listing": False}
        for country, categories in main.RSS_FEEDS.items()
        for category, feeds in categories.items()
        for source, url in feeds
    ]
    jobs.append({"country": "ca", "category": "health", "source": "CTV News", "url": main.CTV_HEALTH_URL, "listing": True})
    executable = ROOT / "target/release/examples" / ("replay.exe" if os.name == "nt" else "replay")
    if not executable.exists():
        raise SystemExit("Build first: cargo build --release --locked --examples")
    with tempfile.TemporaryDirectory(prefix="news-parity-") as directory:
        directory = Path(directory)
        started = time.perf_counter()

        def capture(pair):
            index, job = pair
            document = main.fetch_public_document(job["url"]).decode("utf-8-sig")
            file = f"{index}.txt"
            (directory / file).write_text(document, encoding="utf-8")
            return {**job, "file": file}, document

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            captured = list(pool.map(capture, enumerate(jobs)))
        download_seconds = time.perf_counter() - started
        (directory / "manifest.json").write_text(json.dumps([item for item, _ in captured]), encoding="utf-8")
        timings = []
        for _ in range(5):
            started = time.perf_counter()
            python_articles = [
                main.parse_ctv_health_listing(document) if item["listing"]
                else main.parse_rss(document, item["source"], "2026-11-25T00:00:00Z")
                for item, document in captured
            ]
            timings.append(time.perf_counter() - started)
        rust = json.loads(subprocess.check_output([str(executable), str(directory)], text=True, encoding="utf-8"))
        for (item, _), python, native in zip(captured, python_articles, rust["articles"]):
            expected = {article["url"]: article for article in python}
            actual = {article["url"]: article for article in native}
            assert set(expected) == set(actual), (item["url"], "URL mismatch", set(expected) ^ set(actual))
            for url, article in expected.items():
                for field in ("source", "title", "description", "author", "content", "urlToImage"):
                    assert article[field] == actual[url][field], (url, field, article[field], actual[url][field])
                assert main._normalise_date(article["publishedAt"], "") == main._normalise_date(actual[url]["publishedAt"], ""), (url, "date mismatch")
        result = {
            "responses": len(captured), "articles": sum(map(len, python_articles)),
            "capture_four_workers_seconds": round(download_seconds, 3),
            "python_parse_median_seconds": round(statistics.median(timings), 4),
            "rust_parse_mean_seconds": round(rust["parse_seconds"], 4),
            "parse_speedup": round(statistics.median(timings) / rust["parse_seconds"], 2),
            "parity": "all article fields and publication times match",
        }
        print(json.dumps(result, indent=2))
        return result


if __name__ == "__main__":
    compare()
