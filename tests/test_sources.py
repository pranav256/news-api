import json
import unittest
from pathlib import Path

import main


class SourceParityTests(unittest.TestCase):
    def test_rust_experiment_uses_the_same_sources_as_python(self):
        config = json.loads((Path(__file__).resolve().parents[1] / "sources.json").read_text(encoding="utf-8"))
        expected = {
            "countries": main.COUNTRIES,
            "categories": main.CATEGORIES,
            "queries": main.EVERYTHING_QUERIES,
            "country_sources": main.COUNTRY_SOURCES,
            "rss_feeds": main.RSS_FEEDS,
            "ctv_health_url": main.CTV_HEALTH_URL,
        }
        self.assertEqual(config, json.loads(json.dumps(expected)))


if __name__ == "__main__":
    unittest.main()
