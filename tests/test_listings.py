import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main


def health_page(stories, section="/health"):
    parameters = json.dumps({"includeSections": section}, separators=(",", ":")).replace('"', r'\"')
    cache = {"story-feed-sections": {parameters: {"data": {"content_elements": stories}}}}
    return '<script id="fusion-metadata">Fusion.globalContent=' + json.dumps({"_id": section}) + ";Fusion.contentCache=" + json.dumps(cache) + ";</script>"


def health_story(**overrides):
    return {
        "type": "story", "owner": {"sponsored": False},
        "headlines": {"basic": "Health &amp; research"},
        "description": {"basic": "<p>Research summary</p>"},
        "display_date": "2026-11-25T00:00:00Z",
        "websites": {"ctvnews": {"website_url": "/health/article/research/"}},
        "promo_items": {"basic": {"url": "https://images.example.com/research.jpg"}},
        **overrides,
    }


class ListingTests(unittest.TestCase):
    def test_embedded_json_produces_deduplicated_newsapi_shaped_articles(self):
        story = health_story()
        articles = main.parse_ctv_health_listing(health_page([story, story]) + "throw new Error('never execute this');")
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0]["url"], "https://www.ctvnews.ca/health/article/research/")
        self.assertEqual(articles[0]["title"], "Health & research")
        self.assertEqual(articles[0]["description"], "Research summary")
        self.assertEqual(articles[0]["publishedAt"], "2026-11-25T00:00:00Z")
        self.assertEqual(articles[0]["source"]["name"], "CTV News")

    def test_broken_or_wrong_listing_is_failure(self):
        for document in ("<html>Unavailable</html>", "Fusion.globalContent=broken", health_page([], section="/sports"), health_page([])):
            with self.subTest(document=document[:50]), self.assertRaises(ValueError):
                main.parse_ctv_health_listing(document)

    def test_invalid_advertised_and_non_article_items_are_ignored(self):
        invalid = [None, "bad", health_story(type="video"), health_story(owner={"sponsored": True}),
                   health_story(display_date=None), health_story(display_date="bad"), health_story(headlines={}),
                   health_story(websites={"ctvnews": {"website_url": "https://evil.example/article/story/"}}),
                   health_story(websites={"ctvnews": {"website_url": "javascript:alert(1)"}})]
        self.assertEqual(len(main.parse_ctv_health_listing(health_page(invalid + [health_story()]))), 1)
        with self.assertRaises(ValueError):
            main.parse_ctv_health_listing(health_page(invalid))

    def test_oversized_response_is_rejected(self):
        with patch.object(main, "MAX_FEED_BYTES", 8), patch.object(main.urllib.request, "urlopen") as request:
            request.return_value.__enter__.return_value.read.return_value = b"x" * 9
            with self.assertRaises(ValueError):
                main.fetch_public_document(main.CTV_HEALTH_URL)

    def test_listing_merges_once_and_failures_preserve_published_feeds(self):
        for listing_ok, rss_ok in ((True, True), (True, False), (False, True), (False, False)):
            with self.subTest(listing_ok=listing_ok, rss_ok=rss_ok), contextlib.ExitStack() as stack:
                root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
                for name, value in {"BASE_DIR": root, "DATA_DIR": root / "data", "COUNTRIES": ("ca",), "CATEGORIES": ("health",), "EVERYTHING_CATEGORIES": ("health",)}.items():
                    stack.enter_context(patch.object(main, name, value))
                old_article = {"url": "https://example.com/old"}
                old = main.data_response(source="top_headlines", country="ca", category="health", fetched_at="2026-11-01T00:00:00Z", articles=[old_article], providers=["rss"])
                main.save_json_atomic(main.root_response([old_article]), root / "ca/health.json")
                main.save_json_atomic(old, root / "data/ca/health_headlines.json")
                main.save_json_atomic(old, root / "data/ca/health_everything.json")
                before = {path: path.read_bytes() for path in root.rglob("*.json")}
                rss = main.RSSResult(articles=[{"url": "https://example.com/rss"}] if rss_ok else [], attempted=1, succeeded=int(rss_ok))
                stack.enter_context(patch.object(main, "fetch_rss_articles", return_value=rss))
                fetch = stack.enter_context(patch.object(main, "fetch_public_document"))
                if listing_ok:
                    fetch.return_value = health_page([health_story()]).encode()
                else:
                    fetch.side_effect = OSError("Unavailable")
                stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                self.assertEqual(main.main(["--rss-only"]), 0 if listing_ok or rss_ok else 1)
                fetch.assert_called_once_with(main.CTV_HEALTH_URL)
                status = json.loads((root / "data/status.json").read_text())
                self.assertEqual(status["listing_pages_attempted"], 1)
                self.assertEqual(status["listing_pages_succeeded"], int(listing_ok))
                self.assertEqual(status["listing_pages_failed"], int(not listing_ok))
                if not listing_ok and not rss_ok:
                    for path, contents in before.items():
                        self.assertEqual(path.read_bytes(), contents)
                else:
                    public = json.loads((root / "ca/health.json").read_text())
                    deep = json.loads((root / "data/ca/health_everything.json").read_text())
                    self.assertEqual(public["articles"], deep["articles"])
                    self.assertEqual(len(public["articles"]), int(listing_ok) + int(rss_ok))
                    self.assertEqual("publisher_listing" in deep["providers"], listing_ok)


if __name__ == "__main__":
    unittest.main()
