import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from news_pipeline.judge import assess


class JudgeTests(unittest.TestCase):
    def test_deepseek_result_is_schema_and_evidence_checked(self):
        timestamp = datetime.now(timezone.utc).isoformat()
        source = {
            "id": "cls:1", "channel_id": "cls", "channel_name": "财联社",
            "title": "美联储主席表示将关注通胀",
            "content": "美联储主席表示将关注通胀走势。",
            "url": "https://example.com/1", "published_at": timestamp,
        }
        result = {
            "schema_version": "2.0", "news_item_id": "item-1", "relevant": True,
            "reason": "原文明确报道目标主体的表态。",
            "subjects": [{"category": "fed_chair", "name": "美联储主席", "role": "unknown",
                          "evidence_source_id": "cls:1", "evidence_quote": "美联储主席"}],
            "actions": [{"action_type": "statement", "actor": "美联储主席",
                          "description": "表示将关注通胀走势", "object": "通胀走势", "status": "confirmed",
                          "evidence_source_id": "cls:1", "evidence_quote": "表示将关注通胀走势"}],
        }
        payload = {"news_item_id": "item-1", "target": source, "sources": [source]}
        config = {"skill_directory": str(Path("skills/news-subject-action").resolve()), "max_output_tokens": 4000,
                  "deepseek_timeout_seconds": 10}
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test", "DEEPSEEK_MODEL": "test"}, clear=False), \
             patch("news_pipeline.judge.request_json", return_value={
                 "choices": [{"message": {"content": json.dumps(result, ensure_ascii=False)}}]
             }):
            self.assertEqual(assess(config, payload)["actions"][0]["action_type"], "statement")


if __name__ == "__main__":
    unittest.main()
