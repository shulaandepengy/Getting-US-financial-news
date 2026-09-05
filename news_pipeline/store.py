import hashlib
import json
import sqlite3
from pathlib import Path


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
          PRAGMA journal_mode=WAL;
          CREATE TABLE IF NOT EXISTS articles (
            id TEXT PRIMARY KEY, body TEXT NOT NULL, revision TEXT NOT NULL,
            last_attempt REAL NOT NULL DEFAULT 0, last_success REAL NOT NULL DEFAULT 0,
            latest TEXT, notification_hash TEXT);
          CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY, article_id TEXT NOT NULL, created_at TEXT NOT NULL,
            body TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS outbox (
            id INTEGER PRIMARY KEY, article_id TEXT NOT NULL, fingerprint TEXT UNIQUE NOT NULL,
            text TEXT NOT NULL, sent INTEGER NOT NULL DEFAULT 0, last_attempt REAL NOT NULL DEFAULT 0);
        ''')

    def save_article(self, article):
        revision = digest({key: value for key, value in article.items() if key != "fetched_at"})
        old = self.db.execute("SELECT revision FROM articles WHERE id=?", (article["id"],)).fetchone()
        if old and old["revision"] == revision:
            return
        with self.db:
            self.db.execute('''INSERT INTO articles(id,body,revision) VALUES(?,?,?)
              ON CONFLICT(id) DO UPDATE SET body=excluded.body,revision=excluded.revision,last_success=0''',
                            (article["id"], encode(article), revision))

    def recent(self, start):
        return self.db.execute('''SELECT * FROM articles
          WHERE COALESCE(json_extract(body,'$.published_at'),json_extract(body,'$.fetched_at')) >= ?
          ORDER BY last_attempt ASC, id ASC''', (start,)).fetchall()

    def attempted(self, article_id, now):
        with self.db:
            self.db.execute("UPDATE articles SET last_attempt=? WHERE id=?", (now, article_id))

    def save_assessment(self, article, result, now, notification, semantic_hash):
        old = self.db.execute("SELECT notification_hash FROM articles WHERE id=?", (article["id"],)).fetchone()
        with self.db:
            cur = self.db.execute("INSERT INTO history(article_id,created_at,body) VALUES(?,?,?)",
                                 (article["id"], result["as_of"], encode(result)))
            self.db.execute("UPDATE articles SET latest=?,last_success=?,notification_hash=? WHERE id=?",
                            (encode(result), now, semantic_hash, article["id"]))
            if notification and old["notification_hash"] != semantic_hash:
                # Chunk exact original content at characters, within webhook payload limits.
                chunks = [notification[i:i+2500] for i in range(0, len(notification), 2500)]
                for index, chunk in enumerate(chunks):
                    text = f"[记录 {article['id']} / 版本 {cur.lastrowid} / {index+1}/{len(chunks)}]\n{chunk}"
                    self.db.execute("INSERT INTO outbox(article_id,fingerprint,text) VALUES(?,?,?)",
                                    (article["id"], f"{cur.lastrowid}:{index}", text))

    def pending(self, now, retry_seconds):
        # Global sequence: do not deliver later chunks ahead of a failed earlier chunk.
        row = self.db.execute("SELECT * FROM outbox WHERE sent=0 ORDER BY id LIMIT 1").fetchone()
        return row if row and now - row["last_attempt"] >= retry_seconds else None

    def close(self):
        self.db.close()
