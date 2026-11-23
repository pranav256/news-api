import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import main


class FeedUtilityTests(unittest.TestCase):
    def test_malformed_provider_articles_do_not_break_the_pipeline(self):
        self.assertEqual(main.response_articles({"status": "ok", "articles": [None, "bad", {"url": "https://example.com"}]}), [{"url": "https://example.com"}])
        self.assertEqual(main.response_articles({"status": "ok", "articles": {"url": "bad"}}), [])

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


class ConfigurationTests(unittest.TestCase):
    def test_load_api_keys_detects_combined_and_legacy_values_without_duplicates(self):
        environment = {
            "NEWSAPI_KEYS": "one,two one",
            "FIRSTAPI": "three",
            "SECONDAPI": "two",
            "THIRDAPI": "",
        }

        self.assertEqual(main.load_api_keys(environment), ["one", "two", "three"])

    def test_request_budget_has_safety_reserve(self):
        self.assertEqual(main.calculate_run_budget(4), 26)
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
        self.assertEqual(main.NEWSAPI_HEADLINE_COUNTRIES, {"in", "us", "gb"})
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
