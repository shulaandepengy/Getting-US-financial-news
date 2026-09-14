import unittest
from datetime import datetime, timedelta, timezone

from news_pipeline.dedupe import same_story
from news_pipeline.store import Store


def article(article_id, channel_id, title, content, published_at):
    return {
        "id": f"{channel_id}:{article_id}",
        "upstream_id": article_id,
        "channel_id": channel_id,
        "channel_name": channel_id.upper(),
        "url": f"https://example.com/{channel_id}/{article_id}",
        "published_at": published_at,
        "fetched_at": published_at,
        "title": title,
        "content": content,
        "explicit_attributions": [],
    }


class DedupeAndStoreTests(unittest.TestCase):
    def test_duplicate_window_and_expiry(self):
        now = datetime.now(timezone.utc)
        first = article("1", "a", "Fed holds rates steady", "The Federal Reserve held rates steady today.", now.isoformat())
        duplicate = article("2", "b", "Fed holds rates steady", "The Federal Reserve held rates steady today.", (now + timedelta(hours=5)).isoformat())
        old = article("3", "c", "Fed holds rates steady", "The Federal Reserve held rates steady today.", (now + timedelta(hours=7)).isoformat())
        config = {"window_hours": 6, "min_title_chars": 6}
        self.assertTrue(same_story(first, duplicate, config))
        self.assertFalse(same_story(first, old, config))

    def test_all_duplicate_sources_are_kept_and_outbox_is_idempotent(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        first = article("1", "cls", "Fed holds rates steady", "The Federal Reserve held rates steady today.", now.isoformat())
        duplicate = article("2", "jin10", "Fed holds rates steady", "The Federal Reserve held rates steady today.", (now + timedelta(minutes=10)).isoformat())
        store = Store(":memory:")
        try:
            run_id = store.start_run(now.isoformat(), (now + timedelta(hours=6)).isoformat(), {"sample_hours": 6})
            changed = store.save_articles([first, duplicate])
            groups = store.ingest_news_items(
                [first, duplicate], changed,
                {"window_hours": 6, "title_similarity": 0.86, "content_similarity": 0.76,
                 "related_title_similarity": 0.48, "min_title_chars": 6, "min_content_chars": 80},
            )
            self.assertEqual(len(groups), 1)
            item = store.get_news_item(next(iter(groups)))
            self.assertEqual(len(item["sources"]), 2)
            self.assertEqual({source["channel_id"] for source in item["sources"]}, {"cls", "jin10"})

            result = {"relevant": True, "subjects": [], "actions": []}
            judgement_id, fingerprint = store.save_judgement(run_id, item["id"], result=result, model="test")
            self.assertEqual(store.enqueue_outbox(item["id"], judgement_id, "message", fingerprint), 1)
            self.assertEqual(store.enqueue_outbox(item["id"], judgement_id, "message", fingerprint), 0)
            self.assertIsNotNone(store.pending_outbox(9999999999, 0))
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
