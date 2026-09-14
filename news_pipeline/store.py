"""SQLite persistence for sampling runs, raw articles, deduplicated items and delivery."""

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .dedupe import same_story


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


class Store:
    """A single SQLite connection used by the orchestration thread.

    Source HTTP requests run concurrently, but database writes are intentionally
    batched through this connection. SQLite serializes writes; parallel writers
    would only add lock contention and risk partially written duplicate groups.
    """

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=30000")
        # DELETE journaling leaves one portable file for a GitHub Actions commit.
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS sampling_runs (
              id TEXT PRIMARY KEY,
              started_at TEXT NOT NULL,
              finished_at TEXT,
              window_start TEXT NOT NULL,
              window_end TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'running',
              config_json TEXT NOT NULL,
              coverage_json TEXT,
              collected_count INTEGER NOT NULL DEFAULT 0,
              news_item_count INTEGER NOT NULL DEFAULT 0,
              judgement_count INTEGER NOT NULL DEFAULT 0,
              sent_count INTEGER NOT NULL DEFAULT 0,
              error TEXT
            );

            CREATE TABLE IF NOT EXISTS raw_articles (
              id TEXT PRIMARY KEY,
              upstream_id TEXT,
              channel_id TEXT NOT NULL,
              channel_name TEXT NOT NULL,
              url TEXT,
              published_at TEXT NOT NULL,
              fetched_at TEXT NOT NULL,
              title TEXT NOT NULL,
              content TEXT NOT NULL,
              explicit_attributions_json TEXT NOT NULL,
              content_hash TEXT NOT NULL,
              revision TEXT NOT NULL,
              raw_json TEXT NOT NULL,
              first_seen_at TEXT NOT NULL,
              last_seen_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_raw_articles_published ON raw_articles(published_at);
            CREATE INDEX IF NOT EXISTS idx_raw_articles_channel ON raw_articles(channel_id, published_at);

            CREATE TABLE IF NOT EXISTS news_items (
              id TEXT PRIMARY KEY,
              canonical_article_id TEXT NOT NULL REFERENCES raw_articles(id),
              first_published_at TEXT NOT NULL,
              last_published_at TEXT NOT NULL,
              dedup_window_hours INTEGER NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_news_items_time ON news_items(first_published_at, last_published_at);

            CREATE TABLE IF NOT EXISTS news_item_sources (
              article_id TEXT PRIMARY KEY REFERENCES raw_articles(id) ON DELETE CASCADE,
              news_item_id TEXT NOT NULL REFERENCES news_items(id) ON DELETE CASCADE,
              is_primary INTEGER NOT NULL DEFAULT 0,
              relation TEXT NOT NULL DEFAULT 'observed_duplicate',
              linked_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_news_item_sources_item ON news_item_sources(news_item_id);

            CREATE TABLE IF NOT EXISTS judgements (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              news_item_id TEXT NOT NULL REFERENCES news_items(id) ON DELETE CASCADE,
              run_id TEXT NOT NULL REFERENCES sampling_runs(id) ON DELETE CASCADE,
              created_at TEXT NOT NULL,
              model TEXT NOT NULL,
              status TEXT NOT NULL,
              relevant INTEGER,
              subjects_json TEXT,
              actions_json TEXT,
              result_json TEXT,
              input_hash TEXT,
              fingerprint TEXT,
              error TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_judgements_item ON judgements(news_item_id, created_at);

            CREATE TABLE IF NOT EXISTS feishu_outbox (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              news_item_id TEXT NOT NULL REFERENCES news_items(id) ON DELETE CASCADE,
              judgement_id INTEGER NOT NULL REFERENCES judgements(id) ON DELETE CASCADE,
              fingerprint TEXT UNIQUE NOT NULL,
              text TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending',
              attempts INTEGER NOT NULL DEFAULT 0,
              last_attempt REAL NOT NULL DEFAULT 0,
              sent_at TEXT,
              last_error TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_feishu_outbox_pending ON feishu_outbox(status, id);
            """
        )

    def start_run(self, window_start, window_end, config):
        run_id = "run-" + uuid.uuid4().hex
        with self.db:
            self.db.execute(
                """INSERT INTO sampling_runs
                   (id, started_at, window_start, window_end, config_json)
                   VALUES (?,?,?,?,?)""",
                (run_id, now_iso(), window_start, window_end, encode(config)),
            )
        return run_id

    def save_coverage(self, run_id, coverage, collected_count):
        with self.db:
            self.db.execute(
                "UPDATE sampling_runs SET coverage_json=?, collected_count=? WHERE id=?",
                (encode(coverage), collected_count, run_id),
            )

    def finish_run(self, run_id, status, news_item_count, judgement_count, sent_count, error=None):
        with self.db:
            self.db.execute(
                """UPDATE sampling_runs
                   SET finished_at=?, status=?, news_item_count=?, judgement_count=?, sent_count=?, error=?
                   WHERE id=?""",
                (now_iso(), status, news_item_count, judgement_count, sent_count, error, run_id),
            )

    def save_articles(self, articles):
        """Insert/update raw source records and return records changed this run."""
        changed = set()
        seen_at = now_iso()
        with self.db:
            for article in articles:
                article = dict(article)
                revision = digest({key: value for key, value in article.items() if key != "fetched_at"})
                content_hash = hashlib.sha256(article["content"].encode("utf-8")).hexdigest()
                old = self.db.execute("SELECT revision, first_seen_at FROM raw_articles WHERE id=?", (article["id"],)).fetchone()
                if old is None or old["revision"] != revision:
                    changed.add(article["id"])
                first_seen = old["first_seen_at"] if old else seen_at
                self.db.execute(
                    """INSERT INTO raw_articles
                       (id, upstream_id, channel_id, channel_name, url, published_at, fetched_at,
                        title, content, explicit_attributions_json, content_hash, revision, raw_json,
                        first_seen_at, last_seen_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(id) DO UPDATE SET
                         upstream_id=excluded.upstream_id,
                         channel_id=excluded.channel_id,
                         channel_name=excluded.channel_name,
                         url=excluded.url,
                         published_at=excluded.published_at,
                         fetched_at=excluded.fetched_at,
                         title=excluded.title,
                         content=excluded.content,
                         explicit_attributions_json=excluded.explicit_attributions_json,
                         content_hash=excluded.content_hash,
                         revision=excluded.revision,
                         raw_json=excluded.raw_json,
                         last_seen_at=excluded.last_seen_at""",
                    (
                        article["id"], article.get("upstream_id"), article["channel_id"], article["channel_name"],
                        article.get("url"), article["published_at"], article["fetched_at"], article.get("title", ""),
                        article["content"], encode(article.get("explicit_attributions", [])), content_hash, revision,
                        encode(article), first_seen, seen_at,
                    ),
                )
        return changed

    def _raw_article(self, article_id):
        row = self.db.execute("SELECT * FROM raw_articles WHERE id=?", (article_id,)).fetchone()
        return self._raw_from_row(row) if row else None

    @staticmethod
    def _raw_from_row(row):
        raw = json.loads(row["raw_json"])
        raw["id"] = row["id"]
        raw["upstream_id"] = row["upstream_id"]
        return raw

    def _candidate_articles(self, article, window_hours):
        anchor = _parse_iso(article.get("published_at") or article.get("fetched_at"))
        if not anchor:
            return []
        low = (anchor - timedelta(hours=window_hours)).isoformat()
        high = (anchor + timedelta(hours=window_hours)).isoformat()
        rows = self.db.execute(
            """SELECT r.* FROM raw_articles r
               JOIN news_item_sources s ON s.article_id=r.id
               WHERE r.published_at BETWEEN ? AND ? AND r.id <> ?""",
            (low, high, article["id"]),
        ).fetchall()
        return [self._raw_from_row(row) for row in rows]

    def _create_item(self, article, window_hours, linked_at):
        item_id = "item-" + hashlib.sha256(article["id"].encode("utf-8")).hexdigest()[:24]
        timestamp = article["published_at"]
        self.db.execute(
            """INSERT OR IGNORE INTO news_items
               (id, canonical_article_id, first_published_at, last_published_at,
                dedup_window_hours, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?)""",
            (item_id, article["id"], timestamp, timestamp, window_hours, linked_at, linked_at),
        )
        return item_id

    def _merge_items(self, keep_id, other_ids):
        for other_id in sorted(set(other_ids) - {keep_id}):
            self.db.execute(
                "UPDATE news_item_sources SET news_item_id=? WHERE news_item_id=?",
                (keep_id, other_id),
            )
            self.db.execute("UPDATE judgements SET news_item_id=? WHERE news_item_id=?", (keep_id, other_id))
            self.db.execute("UPDATE feishu_outbox SET news_item_id=? WHERE news_item_id=?", (keep_id, other_id))
            self.db.execute("DELETE FROM news_items WHERE id=?", (other_id,))

    def _refresh_item(self, item_id, linked_at):
        rows = self.db.execute(
            """SELECT r.*, s.is_primary FROM raw_articles r
               JOIN news_item_sources s ON s.article_id=r.id
               WHERE s.news_item_id=?""",
            (item_id,),
        ).fetchall()
        if not rows:
            return
        def sort_key(row):
            timestamp = _parse_iso(row["published_at"]) or datetime.max.replace(tzinfo=timezone.utc)
            # Earlier publication is the stable primary; longer text breaks ties.
            return timestamp, -len(row["content"]), row["id"]
        primary = sorted(rows, key=sort_key)[0]
        timestamps = [_parse_iso(row["published_at"]) for row in rows]
        timestamps = [value for value in timestamps if value]
        first = min(timestamps).isoformat() if timestamps else primary["published_at"]
        last = max(timestamps).isoformat() if timestamps else primary["published_at"]
        self.db.execute("UPDATE news_item_sources SET is_primary=0, relation='observed_duplicate' WHERE news_item_id=?", (item_id,))
        self.db.execute("UPDATE news_item_sources SET is_primary=1, relation='primary' WHERE news_item_id=? AND article_id=?",
                        (item_id, primary["id"]))
        self.db.execute(
            """UPDATE news_items SET canonical_article_id=?, first_published_at=?, last_published_at=?, updated_at=?
               WHERE id=?""",
            (primary["id"], first, last, linked_at, item_id),
        )

    def ingest_news_items(self, articles, changed_article_ids, dedup_config):
        """Group changed raw records into stable six-hour news items.

        Raw records are never deleted during deduplication. Every source article
        remains in ``raw_articles`` and is linked to exactly one canonical item.
        """
        if not changed_article_ids:
            return set()
        window_hours = int(dedup_config.get("window_hours", 6))
        changed = set(changed_article_ids)
        touched = set()
        linked_at = now_iso()
        with self.db:
            for article_id in sorted(changed):
                article = self._raw_article(article_id)
                if not article:
                    continue
                existing = self.db.execute(
                    "SELECT news_item_id FROM news_item_sources WHERE article_id=?", (article_id,)
                ).fetchone()
                matching_groups = set()
                for candidate in self._candidate_articles(article, window_hours):
                    if same_story(article, candidate, dedup_config):
                        group = self.db.execute(
                            "SELECT news_item_id FROM news_item_sources WHERE article_id=?", (candidate["id"],)
                        ).fetchone()
                        if group:
                            matching_groups.add(group["news_item_id"])
                if existing:
                    matching_groups.add(existing["news_item_id"])
                if matching_groups:
                    keep_id = sorted(matching_groups)[0]
                    self._merge_items(keep_id, matching_groups - {keep_id})
                else:
                    keep_id = self._create_item(article, window_hours, linked_at)
                self.db.execute(
                    """INSERT INTO news_item_sources(article_id, news_item_id, is_primary, relation, linked_at)
                       VALUES(?,?,?,?,?)
                       ON CONFLICT(article_id) DO UPDATE SET news_item_id=excluded.news_item_id, linked_at=excluded.linked_at""",
                    (article_id, keep_id, 0, "observed_duplicate", linked_at),
                )
                self._refresh_item(keep_id, linked_at)
                touched.add(keep_id)
        return touched

    def pending_news_item_ids(self, window_start, window_end, limit):
        rows = self.db.execute(
            """SELECT n.id FROM news_items n
               WHERE n.first_published_at <= ? AND n.last_published_at >= ?
                 AND (NOT EXISTS (
                        SELECT 1 FROM judgements j
                        WHERE j.news_item_id=n.id AND j.status='success'
                     ) OR n.updated_at > COALESCE((
                        SELECT MAX(j.created_at) FROM judgements j
                        WHERE j.news_item_id=n.id AND j.status='success'
                     ), ''))
               ORDER BY n.first_published_at ASC, n.id ASC LIMIT ?""",
            (window_end, window_start, int(limit)),
        ).fetchall()
        return [row["id"] for row in rows]

    def get_news_item(self, item_id):
        row = self.db.execute(
            """SELECT n.*, r.id AS canonical_id, r.upstream_id AS canonical_upstream_id,
                      r.channel_id AS canonical_channel_id, r.channel_name AS canonical_channel_name,
                      r.url AS canonical_url, r.published_at AS canonical_published_at,
                      r.fetched_at AS canonical_fetched_at, r.title AS canonical_title,
                      r.content AS canonical_content, r.explicit_attributions_json AS canonical_attributions
               FROM news_items n JOIN raw_articles r ON r.id=n.canonical_article_id
               WHERE n.id=?""",
            (item_id,),
        ).fetchone()
        if not row:
            return None
        sources = self.db.execute(
            """SELECT r.id, r.upstream_id, r.channel_id, r.channel_name, r.url,
                      r.published_at, r.fetched_at, r.title, r.content,
                      r.explicit_attributions_json, s.is_primary, s.relation
               FROM news_item_sources s JOIN raw_articles r ON r.id=s.article_id
               WHERE s.news_item_id=?
               ORDER BY s.is_primary DESC, r.published_at ASC, r.id ASC""",
            (item_id,),
        ).fetchall()
        canonical = {
            "id": row["canonical_id"],
            "upstream_id": row["canonical_upstream_id"],
            "channel_id": row["canonical_channel_id"],
            "channel_name": row["canonical_channel_name"],
            "url": row["canonical_url"],
            "published_at": row["canonical_published_at"],
            "fetched_at": row["canonical_fetched_at"],
            "title": row["canonical_title"],
            "content": row["canonical_content"],
            "explicit_attributions": json.loads(row["canonical_attributions"]),
        }
        source_records = []
        for source in sources:
            source_records.append({
                "id": source["id"],
                "upstream_id": source["upstream_id"],
                "channel_id": source["channel_id"],
                "channel_name": source["channel_name"],
                "url": source["url"],
                "published_at": source["published_at"],
                "fetched_at": source["fetched_at"],
                "title": source["title"],
                "content": source["content"],
                "explicit_attributions": json.loads(source["explicit_attributions_json"]),
                "is_primary": bool(source["is_primary"]),
                "relation": source["relation"],
            })
        return {
            "id": row["id"],
            "canonical": canonical,
            "sources": source_records,
            "first_published_at": row["first_published_at"],
            "last_published_at": row["last_published_at"],
            "dedup_window_hours": row["dedup_window_hours"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def save_judgement(self, run_id, item_id, result=None, model="", input_hash=None, error=None):
        status = "success" if result is not None else "failed"
        created_at = now_iso()
        stable = None
        if result is not None:
            stable = {
                "relevant": result.get("relevant"),
                "subjects": result.get("subjects", []),
                "actions": result.get("actions", []),
            }
        source_ids = [row["article_id"] for row in self.db.execute(
            "SELECT article_id FROM news_item_sources WHERE news_item_id=? ORDER BY article_id",
            (item_id,),
        ).fetchall()]
        fingerprint = digest({"news_item_id": item_id, "source_ids": source_ids,
                              "input_hash": input_hash, "stable": stable, "error": error})
        with self.db:
            cursor = self.db.execute(
                """INSERT INTO judgements
                   (news_item_id, run_id, created_at, model, status, relevant,
                    subjects_json, actions_json, result_json, input_hash, fingerprint, error)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    item_id, run_id, created_at, model, status,
                    int(result["relevant"]) if result is not None else None,
                    encode(result.get("subjects", [])) if result is not None else None,
                    encode(result.get("actions", [])) if result is not None else None,
                    encode(result) if result is not None else None,
                    input_hash, fingerprint, error,
                ),
            )
        return cursor.lastrowid, fingerprint

    def enqueue_outbox(self, item_id, judgement_id, text, fingerprint):
        chunks = [text[index:index + 2500] for index in range(0, len(text), 2500)] or [""]
        inserted = 0
        with self.db:
            for index, chunk in enumerate(chunks, 1):
                chunk_fingerprint = f"{fingerprint}:{index}:{len(chunks)}"
                cursor = self.db.execute(
                    """INSERT OR IGNORE INTO feishu_outbox
                       (news_item_id, judgement_id, fingerprint, text)
                       VALUES(?,?,?,?)""",
                    (item_id, judgement_id, chunk_fingerprint,
                     f"[消息 {index}/{len(chunks)}]\n{chunk}"),
                )
                inserted += cursor.rowcount
        return inserted

    def pending_outbox(self, now_epoch, retry_seconds):
        return self.db.execute(
            """SELECT * FROM feishu_outbox
               WHERE status='pending' AND (? - last_attempt) >= ?
               ORDER BY id LIMIT 1""",
            (now_epoch, retry_seconds),
        ).fetchone()

    def mark_outbox_attempt(self, outbox_id, now_epoch):
        with self.db:
            self.db.execute(
                "UPDATE feishu_outbox SET attempts=attempts+1,last_attempt=? WHERE id=?",
                (now_epoch, outbox_id),
            )

    def mark_outbox_sent(self, outbox_id):
        with self.db:
            self.db.execute(
                "UPDATE feishu_outbox SET status='sent',sent_at=?,last_error=NULL WHERE id=?",
                (now_iso(), outbox_id),
            )

    def mark_outbox_failed(self, outbox_id, error):
        with self.db:
            self.db.execute(
                "UPDATE feishu_outbox SET last_error=? WHERE id=?",
                (error, outbox_id),
            )

    def outbox_rows(self):
        return self.db.execute(
            "SELECT id,news_item_id,judgement_id,status,attempts,last_error,text FROM feishu_outbox ORDER BY id"
        ).fetchall()

    def prune(self, cutoff_iso):
        """Remove old completed news groups while retaining pending deliveries."""
        removed = 0
        with self.db:
            groups = self.db.execute(
                "SELECT id FROM news_items WHERE last_published_at < ?", (cutoff_iso,)
            ).fetchall()
            for group in groups:
                pending = self.db.execute(
                    "SELECT 1 FROM feishu_outbox WHERE news_item_id=? AND status='pending' LIMIT 1",
                    (group["id"],),
                ).fetchone()
                if pending:
                    continue
                self.db.execute("DELETE FROM news_item_sources WHERE news_item_id=?", (group["id"],))
                self.db.execute("DELETE FROM judgements WHERE news_item_id=?", (group["id"],))
                self.db.execute("DELETE FROM feishu_outbox WHERE news_item_id=?", (group["id"],))
                self.db.execute("DELETE FROM news_items WHERE id=?", (group["id"],))
                removed += 1
            self.db.execute(
                """DELETE FROM raw_articles
                   WHERE last_seen_at < ? AND id NOT IN (SELECT article_id FROM news_item_sources)""",
                (cutoff_iso,),
            )
        return removed

    def close(self):
        self.db.commit()
        self.db.close()
