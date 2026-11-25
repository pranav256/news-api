import contextlib
import http.client
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main


class FeedUtilityTests(unittest.TestCase):
    def test_failed_atomic_write_keeps_original_and_removes_temporary_file(self):
        for failure in ("serialization", "replacement"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / "feed.json"
                target.write_text('{"original": true}', encoding="utf-8")
                if failure == "serialization":
                    with self.assertRaises(TypeError):
                        main.save_json_atomic({"bad": object()}, target)
                else:
                    with patch.object(Path, "replace", side_effect=PermissionError("blocked")), self.assertRaises(PermissionError):
                        main.save_json_atomic({"new": True}, target)
                self.assertEqual(target.read_text(), '{"original": true}')
                self.assertEqual(list(Path(directory).iterdir()), [target])

    def test_interrupted_rss_read_uses_the_next_provider(self):
        state = main.ScrapeState(main.KeyPool([]), main.RequestBudget(0))
        article = {"url": "https://example.com/story"}
        feeds = {"us": {"general": [("Broken", "https://example.com/broken"), ("Working", "https://example.com/feed")]}}
        with patch.object(main, "RSS_FEEDS", feeds), patch.object(main, "fetch_rss_feed", side_effect=[http.client.IncompleteRead(b"partial"), [article]]):
            result = main.fetch_rss_articles("us", "general", "2026-11-25T00:00:00Z", state)
        self.assertEqual(result.articles, [article])
        self.assertEqual((result.attempted, result.succeeded, len(result.errors)), (2, 1, 1))

    def test_malformed_provider_articles_do_not_break_the_pipeline(self):
        self.assertEqual(main.response_articles({"status": "ok", "articles": [None, "bad", {"url": "https://example.com"}]}), [{"url": "https://example.com"}])
        self.assertEqual(main.response_articles({"status": "ok", "articles": {"url": "bad"}}), [])

    def test_invalid_article_fields_are_filtered_before_merging(self):
        valid = {"url": "https://example.com/valid", "publishedAt": "2026-11-25T00:00:00Z"}
        articles = [valid, {"url": 123}, {"url": "  "}, {"url": "https://example.com/bad-date", "publishedAt": 123}]
        self.assertEqual(main.deduplicate(main.response_articles({"status": "ok", "articles": articles})), [valid])

    def test_non_feed_xml_counts_as_provider_failure(self):
        state = main.ScrapeState(main.KeyPool([]), main.RequestBudget(0))
        feeds = {"us": {"general": [("Example", "https://example.com/feed")]}}
        for document in (b"<html><body>Unavailable</body></html>", b"<error>Unavailable</error>"):
            with self.subTest(document=document), patch.object(main, "RSS_FEEDS", feeds), patch.object(main.urllib.request, "urlopen") as fetch:
                fetch.return_value.__enter__.return_value.read.return_value = document
                result = main.fetch_rss_articles("us", "general", "2026-11-25T00:00:00Z", state)
                self.assertEqual(result.succeeded, 0)
                self.assertEqual(result.attempted, 1)
                self.assertEqual(len(result.errors), 1)

    def test_valid_empty_rss_and_atom_are_still_accepted(self):
        for document in ("<rss><channel /></rss>", '<feed xmlns="http://www.w3.org/2005/Atom" />'):
            with self.subTest(document=document):
                self.assertEqual(main.parse_rss(document, "Example", "2026-11-25T00:00:00Z"), [])

    def test_invalid_feed_structure_and_unusable_entries_are_failures(self):
        for document in ("<rss><error>Unavailable</error></rss>", "<rss><channel><item><title>No URL</title></item></channel></rss>"):
            with self.subTest(document=document), self.assertRaises(ValueError):
                main.parse_rss(document, "Example", "2026-11-25T00:00:00Z")

    def test_atom_prefers_article_link_over_self_link(self):
        document = '''<feed xmlns="http://www.w3.org/2005/Atom"><entry>
            <title>Story</title><link rel="self" href="https://example.com/api/story" />
            <link rel="alternate" href="https://example.com/story" />
            </entry></feed>'''
        articles = main.parse_rss(document, "Example", "2026-11-25T00:00:00Z")
        self.assertEqual(articles[0]["url"], "https://example.com/story")

    def test_invalid_and_overflowing_dates_use_fallback(self):
        fallback = "2026-11-25T00:00:00Z"
        for value in ("Not a date", "9999-12-31T23:59:59-12:00", "0001-01-01T00:00:00+12:00"):
            with self.subTest(value=value):
                self.assertEqual(main._normalise_date(value, fallback), fallback)

    def test_failed_refresh_keeps_feed_and_its_last_successful_timestamp(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            old = main.data_response(source="top_headlines", country="gb", category="general", fetched_at="2026-10-01T12:00:00Z", articles=[{"url": "https://example.com/story"}], providers=["rss"])
            main.save_json_atomic(old, data / "gb" / "general_headlines.json")
            main.save_json_atomic(main.root_response(old["articles"]), root / "gb" / "general.json")
            before = (root / "gb" / "general.json").read_bytes()
            state = main.ScrapeState(main.KeyPool([]), main.RequestBudget(0))
            fresh = main.data_response(source="top_headlines", country="gb", category="general", fetched_at="2026-10-05T12:00:00Z", articles=[], providers=[])
            with patch.object(main, "BASE_DIR", root), patch.object(main, "DATA_DIR", data):
                main.write_feed_pair(country="gb", category="general", articles=[], data_payload=fresh, provider_succeeded=False, state=state, dry_run=False)
            self.assertEqual((root / "gb" / "general.json").read_bytes(), before)
            self.assertEqual(state.files_updated, [])
            self.assertEqual(state.feed_outcomes[0]["status"], "retained")
            self.assertEqual(state.feed_outcomes[0]["last_successful_refresh"], old["fetched_at"])
            self.assertEqual(json.loads((data / "gb" / "general_headlines.json").read_text()), old)

    def test_successful_empty_feed_is_published_with_real_freshness(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = main.ScrapeState(main.KeyPool([]), main.RequestBudget(0))
            payload = main.data_response(source="top_headlines", country="gb", category="general", fetched_at="2026-10-05T12:00:00Z", articles=[], providers=["rss"])
            with patch.object(main, "BASE_DIR", root), patch.object(main, "DATA_DIR", root / "data"):
                main.write_feed_pair(country="gb", category="general", articles=[], data_payload=payload, provider_succeeded=True, state=state, dry_run=False)
            self.assertEqual(json.loads((root / "gb" / "general.json").read_text())["articles"], [])
            self.assertEqual(state.feed_outcomes[0]["last_successful_refresh"], payload["fetched_at"])
            self.assertEqual(len(state.files_updated), 2)

    def test_deduplicate_prefers_first_article_for_duplicate_url(self):
        articles = [
            {"url": "https://example.com/story", "title": "first", "publishedAt": "2026-01-01T00:00:00Z"},
            {"url": "https://example.com/story", "title": "second", "publishedAt": "2026-01-02T00:00:00Z"},
            {"url": "https://example.com/other", "title": "other", "publishedAt": "2026-01-03T00:00:00Z"},
        ]

        result = main.deduplicate(articles)

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["title"], "other")
        self.assertEqual(result[1]["title"], "first")

    def test_parse_rss_supports_namespace_fields_and_atom_links(self):
        xml = """
        <rss xmlns:media="http://search.yahoo.com/mrss/">
          <channel>
            <item>
              <title>Example &amp; headline</title>
              <link>https://example.com/story</link>
              <description>&lt;p&gt;A short description&lt;/p&gt;</description>
              <pubDate>Thu, 02 Oct 2026 12:00:00 GMT</pubDate>
              <media:content url="https://example.com/image.jpg" />
            </item>
          </channel>
        </rss>
        """

        result = main.parse_rss(xml, "Example News", "2026-10-02T13:00:00+00:00")

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["title"], "Example & headline")
        self.assertEqual(result[0]["description"], "A short description")
        self.assertEqual(result[0]["urlToImage"], "https://example.com/image.jpg")
        self.assertEqual(result[0]["publishedAt"], "2026-10-02T12:00:00Z")

    def test_parse_rss_supports_atom_link_attribute(self):
        xml = """
        <feed xmlns="http://www.w3.org/2005/Atom">
          <entry>
            <title>Atom story</title>
            <link href="https://example.com/atom-story" />
            <updated>2026-10-02T12:00:00Z</updated>
          </entry>
        </feed>
        """

        result = main.parse_rss(xml, "Atom News", "2026-10-02T13:00:00+00:00")

        self.assertEqual(result[0]["url"], "https://example.com/atom-story")

    def test_parse_rss_extracts_image_from_description_html(self):
        xml = """
        <rss>
          <channel>
            <item>
              <title>Headline with inline image</title>
              <link>https://example.com/story-with-image</link>
              <description>&lt;img src="https://example.com/inline-image.jpg" /&gt; Story details here</description>
              <pubDate>Thu, 02 Oct 2026 12:00:00 GMT</pubDate>
            </item>
          </channel>
        </rss>
        """
        result = main.parse_rss(xml, "Inline News", "2026-10-02T13:00:00+00:00")
        self.assertEqual(result[0]["urlToImage"], "https://example.com/inline-image.jpg")
        self.assertEqual(result[0]["description"], "Story details here")


class ScraperTests(unittest.TestCase):
    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        for name, value in {
            "BASE_DIR": self.root,
            "DATA_DIR": self.root / "data",
            "COUNTRIES": ("us",),
            "CATEGORIES": ("general", "business"),
            "EVERYTHING_CATEGORIES": ("general", "business"),
        }.items():
            stack.enter_context(patch.object(main, name, value))
        self.rss = stack.enter_context(patch.object(main, "fetch_rss_articles"))
        self.api = stack.enter_context(patch.object(main, "fetch_newsapi_everything", return_value=None))
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))

    def seed_feed(self, category, articles):
        payload = main.data_response(
            source="top_headlines", country="us", category=category,
            fetched_at="2026-11-24T00:00:00Z", articles=articles, providers=["rss"],
        )
        main.save_json_atomic(payload, self.root / "data/us" / f"{category}_headlines.json")
        main.save_json_atomic(main.root_response(articles), self.root / "us" / f"{category}.json")

    def test_combined_feed_includes_retained_category_in_normal_and_dry_runs(self):
        old = {"url": "https://example.com/old", "title": "Old story"}
        fresh = {"url": "https://example.com/fresh", "title": "Fresh story"}
        self.seed_feed("business", [old])
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run):
                self.rss.side_effect = [main.RSSResult([fresh], 1, 1), main.RSSResult(attempted=1)]
                with patch.object(main, "save_json_atomic", wraps=main.save_json_atomic) as save:
                    self.assertEqual(main.main(["--rss-only"] + (["--dry-run"] if dry_run else [])), 0)
                combined = next(call.args[0] for call in save.call_args_list if call.args[1] == self.root / "us/all.json")
                self.assertEqual({article["url"] for article in combined["articles"]}, {old["url"], fresh["url"]})
                self.assertEqual(json.loads((self.root / "us/business.json").read_text())["articles"], [old])
                if dry_run:
                    self.assertFalse((self.root / "us/all.json").exists())

    def test_total_outage_fails_and_preserves_combined_feed_and_timestamp(self):
        old = {"url": "https://example.com/old"}
        for category in ("general", "business", "all"):
            self.seed_feed(category, [old])
        before = {path: path.read_bytes() for path in self.root.rglob("*.json")}
        self.rss.side_effect = lambda *args: main.RSSResult(attempted=1)

        self.assertEqual(main.main(["--rss-only"]), 1)

        for path, contents in before.items():
            self.assertEqual(path.read_bytes(), contents)
        status = json.loads((self.root / "data/status.json").read_text())
        self.assertEqual(status["feeds_updated"], 0)
        self.assertEqual(status["feeds_retained"], 3)
        self.assertEqual(status["status"], "partial")

    def test_successful_empty_refresh_clears_old_combined_feed(self):
        self.seed_feed("all", [{"url": "https://example.com/old"}])
        self.rss.side_effect = lambda *args: main.RSSResult(attempted=1, succeeded=1)

        self.assertEqual(main.main(["--rss-only"]), 0)

        self.assertEqual(json.loads((self.root / "us/all.json").read_text())["articles"], [])

    def test_deep_feed_success_prevents_total_outage_failure(self):
        self.rss.side_effect = lambda *args: main.RSSResult(attempted=1)
        article = {"url": "https://example.com/deep"}
        self.api.return_value = {"status": "ok", "articles": [article]}

        with patch.object(main, "load_api_keys", return_value=["test-key"]), patch.object(main, "fetch_newsapi_headlines", return_value=None):
            self.assertEqual(main.main([]), 0)

        deep = json.loads((self.root / "data/us/business_everything.json").read_text())
        self.assertEqual(deep["articles"], [article])

    def test_empty_newsapi_success_is_not_a_total_outage(self):
        self.seed_feed("all", [{"url": "https://example.com/old"}])
        self.rss.side_effect = lambda *args: main.RSSResult(attempted=1)
        response = {"status": "ok", "articles": []}
        self.api.return_value = response
        with patch.object(main, "load_api_keys", return_value=["test-key"]), patch.object(main, "fetch_newsapi_headlines", return_value=response):
            self.assertEqual(main.main([]), 0)
        self.assertEqual(json.loads((self.root / "us/all.json").read_text())["articles"], [])
        deep = json.loads((self.root / "data/us/business_everything.json").read_text())
        self.assertEqual(deep["providers"], ["newsapi"])
        self.assertEqual(deep["source"], "everything")


class ConfigurationTests(unittest.TestCase):
    def test_malformed_newsapi_success_is_a_failure(self):
        for response in ([], "bad", {"status": "ok"}, {"status": "ok", "articles": {}}, {"status": "ok", "articles": [None]}):
            with self.subTest(response=response), patch.object(main, "NewsApiClient"), patch.object(main.time, "sleep"):
                state = main.ScrapeState(main.KeyPool(["test-key"]), main.RequestBudget(1))
                self.assertIsNone(main.fetch_with_retry(lambda client: response, "test", state))
                self.assertEqual(state.budget.used, 1)
                self.assertTrue(state.api_failures)

    def test_newsapi_errors_never_print_configured_keys(self):
        key = "review-fake-secret"
        for response in ({"status": "error", "message": f"failed with {key}"}, RuntimeError(f"failed with {key}")):
            with self.subTest(response=type(response)), patch.object(main, "NewsApiClient"), patch.object(main.time, "sleep"):
                state = main.ScrapeState(main.KeyPool([key]), main.RequestBudget(1))
                output = io.StringIO()
                def request(client):
                    if isinstance(response, Exception):
                        raise response
                    return response
                with contextlib.redirect_stdout(output):
                    self.assertIsNone(main.fetch_with_retry(request, "test", state))
                self.assertNotIn(key, output.getvalue())
                self.assertIn("[REDACTED]", output.getvalue())

    def test_load_api_keys_detects_combined_and_legacy_values_without_duplicates(self):
        environment = {
            "NEWSAPI_KEYS": "one,two one",
            "FIRSTAPI": "three",
            "SECONDAPI": "two",
            "THIRDAPI": "",
        }

        self.assertEqual(main.load_api_keys(environment), ["one", "two", "three"])

    def test_request_budget_has_safety_reserve(self):
        self.assertEqual(main.calculate_run_budget(4), 31)
        self.assertEqual(main.calculate_run_budget(0), 0)

    def test_root_response_keeps_flask_contract(self):
        articles = [{"url": "https://example.com"}]

        self.assertEqual(
            main.root_response(articles),
            {"status": "ok", "totalResults": 1, "articles": articles},
        )

    def test_newsapi_retry_rotates_keys_and_counts_attempts(self):
        class FakeClient:
            calls = []

            def __init__(self, api_key):
                self.api_key = api_key
                self.calls.append(api_key)

            def get_top_headlines(self, **kwargs):
                if self.api_key == "first":
                    raise RuntimeError("rateLimited")
                return {"status": "ok", "articles": []}

        state = main.ScrapeState(main.KeyPool(["first", "second"]), main.RequestBudget(4))
        with patch.object(main, "NewsApiClient", FakeClient):
            result = main.fetch_newsapi_headlines("us", "general", state)

        self.assertEqual(result, {"status": "ok", "articles": []})
        self.assertEqual(FakeClient.calls, ["first", "second"])
        self.assertEqual(state.budget.used, 2)

    def test_newsapi_headline_countries_and_categories_fully_configured(self):
        self.assertEqual(main.NEWSAPI_HEADLINE_COUNTRIES, {"in", "us", "gb", "ca", "au"})
        self.assertEqual(set(main.EVERYTHING_CATEGORIES), set(main.CATEGORIES))
        for cat in main.CATEGORIES:
            self.assertIn(cat, main.EVERYTHING_QUERIES)

    def test_budget_exhaustion_does_not_record_api_failure(self):
        class FakeClient:
            def __init__(self, api_key):
                pass
            def get_top_headlines(self, **kwargs):
                return {"status": "ok", "articles": []}

        state = main.ScrapeState(main.KeyPool(["first"]), main.RequestBudget(0))
        with patch.object(main, "NewsApiClient", FakeClient):
            result = main.fetch_newsapi_headlines("us", "general", state)
        self.assertIsNone(result)
        self.assertEqual(state.api_failures, [])


if __name__ == "__main__":
    unittest.main()
