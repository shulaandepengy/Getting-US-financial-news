import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from news_pipeline import cli
from news_pipeline.store import Store


class PipelineTests(unittest.TestCase):
    def test_one_run_collects_concurrently_deduplicates_and_queues_feishu(self):
        timestamp = datetime.now(timezone.utc).isoformat()
        first = {
            "id": "cls:1", "upstream_id": "1", "channel_id": "cls", "channel_name": "财联社",
            "url": "https://example.com/1", "published_at": timestamp, "fetched_at": timestamp,
            "title": "Fed holds rates steady", "content": "The Federal Reserve held rates steady today.",
            "explicit_attributions": [],
        }
        duplicate = dict(first)
        duplicate.update({"id": "jin10:2", "upstream_id": "2", "channel_id": "jin10", "channel_name": "金十数据",
                          "url": "https://example.com/2"})
        config = {
            "sample_hours": 6, "dedup_window_hours": 6, "source_workers": 2,
            "max_news_items_per_run": 100, "max_input_chars": 100000, "max_output_tokens": 4000,
            "deepseek_timeout_seconds": 180, "retry_seconds": 0, "duplicate_source_preview_chars": 1500,
            "retention_days": 30, "send_irrelevant": False,
            "database": ":memory:", "skill_directory": str(Path("skills/news-subject-action").resolve()),
            "dedup": {"window_hours": 6, "title_similarity": 0.86, "content_similarity": 0.76,
                      "related_title_similarity": 0.48, "min_title_chars": 6, "min_content_chars": 80},
            "sources": [{"id": "cls", "name": "财联社", "kind": "json", "url": "https://example.com/cls"},
                        {"id": "jin10", "name": "金十数据", "kind": "json", "url": "https://example.com/jin10"}],
        }
        coverage = lambda source: ({"source_id": source["id"], "name": source["name"], "status": "ok",
                                    "gaps": [], "pagination_complete": True,
                                    "window_start": timestamp, "window_end": timestamp},
                                   [first] if source["id"] == "cls" else [duplicate])

        def collect_result(source, start, end):
            state, records = coverage(source)
            return records, state

        model_result = {"schema_version": "2.0", "news_item_id": "unused", "relevant": True,
                        "reason": "目标主体有明确动作。", "subjects": [], "actions": []}
        store = Store(":memory:")
        try:
            with patch("news_pipeline.cli.collect", side_effect=collect_result), \
                 patch("news_pipeline.cli.assess", return_value=model_result), \
                 patch("news_pipeline.cli.feishu.drain", return_value=0):
                summary = cli.run_once(config, store, send=True)
            self.assertEqual(summary["records"], 2)
            self.assertEqual(summary["news_items"], 1)
            self.assertEqual(summary["judgements"], 1)
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM news_item_sources").fetchone()[0], 2)
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM feishu_outbox").fetchone()[0], 1)
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()

