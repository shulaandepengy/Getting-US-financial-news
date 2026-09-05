"""Configurable JSON and RSS/Atom inputs with explicit coverage accounting."""
import hashlib
import json
import os
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def parse_time(value):
    if value is None or value == "":
        return None
    if isinstance(value, (float, int)):
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        dt = parsedate_to_datetime(str(value))
    if dt.tzinfo is None:
        raise ValueError("timestamp requires explicit timezone")
    return dt.astimezone(timezone.utc).isoformat()


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
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
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
        upstream_id = json.dumps([published, content], ensure_ascii=False)
    record_id = source["id"] + ":" + hashlib.sha256(str(upstream_id).encode()).hexdigest()[:24]
    title = get("title", "") or ""
    if not isinstance(title, str):
        raise ValueError("invalid title")
    attributions = get("explicit_attributions", []) or []
    if not isinstance(attributions, list):
        raise ValueError("invalid attributions")
    return {"id": record_id, "channel_id": source["id"], "channel_name": source["name"],
            "url": url, "published_at": published, "fetched_at": fetched_at,
            "title": title, "content": content, "explicit_attributions": attributions}


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
                    return "".join(children[name].itertext())
            return None
        links = [child for child in item if local(child.tag) == "link"]
        link = next((child.attrib.get("href") or child.text for child in links
                     if child.attrib.get("rel", "alternate") == "alternate"), None)
        result.append({"id": value("guid", "id"), "url": link,
                       "published_at": value("pubDate", "published", "updated"),
                       "title": value("title"), "content": value("encoded", "content", "description", "summary")})
    return result


def collect(source, start, end):
    coverage = {"source_id": source["id"], "name": source["name"], "status": "not_configured",
                "successful_at": None, "window_start": start, "window_end": end,
                "pagination_complete": False, "gaps": []}
    url = os.getenv(source.get("url_env", ""), "") or source.get("url", "")
    if not url:
        coverage["gaps"] = ["source endpoint not configured"]
        return [], coverage
    try:
        url = url.replace("{since}", quote(start, safe="")).replace("{until}", quote(end, safe=""))
        if urlparse(url).scheme != "https":
            raise ValueError("HTTPS source required")
        headers = {"User-Agent": "Getting-US-financial-news/0.1"}
        if source.get("headers_env"):
            headers.update(json.loads(os.environ[source["headers_env"]]))
        with urlopen(Request(url, headers=headers), timeout=30) as response:
            raw = response.read(10_000_001)
        if len(raw) > 10_000_000:
            raise ValueError("source response exceeds 10 MB")
        fetched = utcnow()
        if source["kind"] == "rss":
            items = rss_items(raw)
            coverage["gaps"].append("RSS snapshot cannot prove complete historical coverage or full article text")
        elif source["kind"] == "json":
            payload = json.loads(raw)
            items = read_path(payload, source.get("items_path", "items"))
            # Only a provider/adapter's explicit bounded completeness contract is trusted.
            meta = payload.get("coverage", {}) if isinstance(payload, dict) else {}
            covered_start = parse_time(meta.get("window_start"))
            covered_end = parse_time(meta.get("window_end"))
            if (meta.get("complete") is True and covered_start and covered_end
                    and covered_start <= start and covered_end >= end):
                coverage["pagination_complete"] = True
            else:
                coverage["gaps"].append("feed does not certify complete requested window")
        else:
            raise ValueError("source kind must be json or rss")
        if not isinstance(items, list):
            raise ValueError("items must be a list")
        records = []
        for item in items:
            try:
                record = normalize(source, item, fetched)
                if record["published_at"] is None:
                    coverage["gaps"].append("article publication time unknown")
                if not record["published_at"] or start <= record["published_at"] <= end:
                    records.append(record)
            except (ValueError, TypeError, OverflowError, AttributeError):
                coverage["gaps"].append("invalid article skipped")
        coverage["gaps"] = sorted(set(coverage["gaps"]))
        coverage["status"] = "ok" if not coverage["gaps"] else "stale"
        coverage["successful_at"] = fetched
        return records, coverage
    except Exception as exc:
        # URLs and response bodies may contain credentials; log only exception class.
        coverage["status"] = "failed"
        coverage["gaps"] = ["collection failed: " + type(exc).__name__]
        return [], coverage
