"""Configurable JSON/RSS/direct news-source adapters.

Each source is fetched independently.  Network work is parallelized by the
caller; this module only normalizes the returned records and coverage status.
"""

import hashlib
import json
import os
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def parse_time(value):
    if value is None or value == "":
        return None
    if isinstance(value, (float, int)):
        # Unix milliseconds are accepted because several news feeds use them.
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        parsed = parsedate_to_datetime(str(value))
    if parsed.tzinfo is None:
        raise ValueError("timestamp requires explicit timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def read_path(obj, path, default=None):
    if not path:
        return obj
    for key in path.split("."):
        if not isinstance(obj, dict) or key not in obj:
            return default
        obj = obj[key]
    return obj


def request_json(url, payload=None, headers=None, timeout=60):
    if urlparse(url).scheme != "https":
        raise ValueError("HTTPS URL required")
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(url, data=data, headers={"Content-Type": "application/json", **(headers or {})})
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


def normalize(source, item, fetched_at):
    fields = source.get("fields", {})

    def get(name, default=None):
        return read_path(item, fields.get(name, name), default)

    content = get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("missing full content")
    url = get("url")
    if url is not None and (not isinstance(url, str) or urlparse(url).scheme not in ("http", "https")):
        raise ValueError("invalid article URL")
    published = parse_time(get("published_at"))
    upstream_id = get("id") or url
    if upstream_id is None:
        upstream_id = hashlib.sha256((str(published) + content).encode("utf-8")).hexdigest()
    upstream_id = str(upstream_id)
    record_id = source["id"] + ":" + hashlib.sha256(upstream_id.encode("utf-8")).hexdigest()[:24]
    title = get("title", "") or ""
    if not isinstance(title, str):
        raise ValueError("invalid title")
    attributions = get("explicit_attributions", []) or []
    if not isinstance(attributions, list):
        raise ValueError("invalid attributions")
    return {
        "id": record_id,
        "upstream_id": upstream_id,
        "channel_id": source["id"],
        "channel_name": source["name"],
        "url": url,
        "published_at": published,
        "fetched_at": fetched_at,
        "title": title,
        "content": content,
        "explicit_attributions": attributions,
    }


def rss_items(raw):
    root = ET.fromstring(raw)

    def local(tag):
        return tag.rsplit("}", 1)[-1]

    result = []
    for item in root.iter():
        if local(item.tag) not in ("item", "entry"):
            continue
        children = {local(child.tag): child for child in item}

        def value(*names):
            for name in names:
                if name in children:
                    return "".join(children[name].itertext()).strip()
            return None

        links = [child for child in item if local(child.tag) == "link"]
        link = next((child.attrib.get("href") or child.text for child in links
                     if child.attrib.get("rel", "alternate") == "alternate"), None)
        result.append({
            "id": value("guid", "id"),
            "url": link.strip() if isinstance(link, str) else link,
            "published_at": value("pubDate", "published", "updated"),
            "title": value("title") or "",
            "content": value("encoded", "content", "description", "summary"),
        })
    return result


def _direct_items(kind, payload, coverage):
    items = []
    if kind == "cls":
        if payload.get("errno") != 0:
            raise ValueError("CLS upstream error")
        for row in payload.get("data", {}).get("roll_data", []):
            items.append({
                "id": row.get("id"),
                "title": row.get("title", ""),
                "content": row.get("content"),
                "published_at": row.get("ctime"),
                "url": row.get("shareurl") or "https://www.cls.cn/detail/" + str(row.get("id")),
            })
    elif kind == "jin10":
        if payload.get("status") != 200:
            raise ValueError("Jin10 upstream error")
        for row in payload.get("data", []):
            data = row.get("data", {})
            if data.get("lock"):
                coverage["gaps"].append("locked Jin10 item omitted")
                continue
            if not data.get("content"):
                coverage["gaps"].append("Jin10 item without text omitted")
                continue
            published = datetime.strptime(row["time"], "%Y-%m-%d %H:%M:%S")
            published = published.replace(tzinfo=timezone(timedelta(hours=8))).isoformat()
            items.append({
                "id": row.get("id"),
                "title": data.get("title", ""),
                "content": data["content"],
                "published_at": published,
                "url": "https://flash.jin10.com/detail/" + str(row.get("id")),
            })
    else:
        raise ValueError("unsupported direct source kind")
    return items


def collect(source, start, end):
    """Fetch one source and return records strictly inside [start, end]."""
    coverage = {
        "source_id": source["id"],
        "name": source["name"],
        "status": "not_configured",
        "successful_at": None,
        "window_start": start,
        "window_end": end,
        "pagination_complete": False,
        "received_count": 0,
        "accepted_count": 0,
        "gaps": [],
    }
    url = os.getenv(source.get("url_env", ""), "") or source.get("url", "")
    if not url:
        coverage["gaps"] = ["source endpoint not configured"]
        return [], coverage
    try:
        direct_kind = source.get("kind")
        if direct_kind == "cls":
            # Mirrors the public CLS feed signature; it does not use an account secret.
            params = urlencode(sorted({
                "appName": "CailianpressWeb",
                "os": "web",
                "sv": "8.7.9",
                "name": "telegraph",
            }.items()))
            signature = hashlib.md5(hashlib.sha1(params.encode("utf-8")).hexdigest().encode("utf-8")).hexdigest()
            url = url.split("?", 1)[0] + "?" + params + "&sign=" + signature
        url = url.replace("{since}", quote(start, safe="")).replace("{until}", quote(end, safe=""))
        if urlparse(url).scheme != "https":
            raise ValueError("HTTPS source required")
        headers = {"User-Agent": "Getting-US-financial-news/0.2", "Accept": "application/json, application/rss+xml, application/atom+xml, text/xml"}
        if direct_kind == "jin10":
            headers.update({"x-app-id": "bVBF4FyRTn5NJF5n", "x-version": "1.0.0"})
        if source.get("headers_env"):
            headers.update(json.loads(os.environ[source["headers_env"]]))
        timeout = int(source.get("timeout_seconds", 30))
        with urlopen(Request(url, headers=headers), timeout=timeout) as response:
            raw = response.read(10_000_001)
        if len(raw) > 10_000_000:
            raise ValueError("source response exceeds 10 MB")
        fetched = utcnow()
        if direct_kind in ("cls", "jin10"):
            items = _direct_items(direct_kind, json.loads(raw), coverage)
            coverage["gaps"].append("latest-feed snapshot does not certify complete historical coverage")
        elif direct_kind == "rss":
            items = rss_items(raw)
            coverage["gaps"].append("RSS snapshot does not certify complete historical coverage")
        elif direct_kind == "json":
            payload = json.loads(raw)
            items = read_path(payload, source.get("items_path", "items"))
            meta = payload.get("coverage", {}) if isinstance(payload, dict) else {}
            covered_start = parse_time(meta.get("window_start"))
            covered_end = parse_time(meta.get("window_end"))
            if (meta.get("complete") is True and covered_start and covered_end
                    and covered_start <= start and covered_end >= end):
                coverage["pagination_complete"] = True
            else:
                coverage["gaps"].append("feed does not certify complete requested window")
        else:
            raise ValueError("source kind must be json, rss, cls or jin10")
        if not isinstance(items, list):
            raise ValueError("items must be a list")

        records = []
        coverage["received_count"] = len(items)
        max_items = int(source.get("max_items", 500))
        if len(items) > max_items:
            coverage["gaps"].append(f"source returned more than max_items={max_items}; tail omitted")
            items = items[:max_items]
        for item in items:
            try:
                record = normalize(source, item, fetched)
                if record["published_at"] is None:
                    coverage["gaps"].append("article publication time unknown and omitted")
                    continue
                if start <= record["published_at"] <= end:
                    records.append(record)
            except (ValueError, TypeError, OverflowError, AttributeError, KeyError):
                coverage["gaps"].append("invalid article skipped")
        coverage["accepted_count"] = len(records)
        coverage["gaps"] = sorted(set(coverage["gaps"]))
        coverage["status"] = "ok" if not coverage["gaps"] else "stale"
        coverage["successful_at"] = fetched
        return records, coverage
    except Exception as exc:
        # URLs and response bodies may contain credentials; log only exception class.
        coverage["status"] = "failed"
        status_code = getattr(exc, "code", None)
        coverage["gaps"] = ["collection failed: " + type(exc).__name__
                            + (f" (HTTP {status_code})" if isinstance(status_code, int) else "")]
        return [], coverage
