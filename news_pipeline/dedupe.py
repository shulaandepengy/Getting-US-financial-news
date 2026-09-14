"""Deterministic, configurable news-story duplicate detection.

The database keeps every raw article for auditability, while this module decides
which raw articles belong to one six-hour news item. It deliberately does not
call an LLM: the DeepSeek call is reserved for subject/action extraction.
"""

import re
import unicodedata
from datetime import datetime, timezone
from difflib import SequenceMatcher
from urllib.parse import urlsplit, urlunsplit


_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def normalize_text(value):
    """Return a compact comparison key without changing stored source text."""
    value = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(ch for ch in value if ch.isalnum() or _CJK.match(ch))


def normalize_url(value):
    if not value:
        return ""
    try:
        parts = urlsplit(value.strip())
        return urlunsplit((parts.scheme.casefold(), parts.netloc.casefold(),
                           parts.path.rstrip("/"), parts.query, ""))
    except ValueError:
        return value.strip().casefold().rstrip("/")


def _ngrams(value, size=2):
    if len(value) < size:
        return {value} if value else set()
    return {value[index:index + size] for index in range(len(value) - size + 1)}


def similarity(left, right):
    """Blend sequence and character-shingle similarity for Chinese and English."""
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    # Long article bodies can make SequenceMatcher quadratic; the beginning and
    # end preserve the headline/lead and the conclusion without unbounded cost.
    left = left[:4000]
    right = right[:4000]
    ratio = SequenceMatcher(None, left, right, autojunk=False).ratio()
    left_grams = _ngrams(left)
    right_grams = _ngrams(right)
    union = left_grams | right_grams
    jaccard = len(left_grams & right_grams) / len(union) if union else 0.0
    return max(ratio, jaccard)


def _timestamp(article):
    value = article.get("published_at") or article.get("fetched_at")
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def within_window(left, right, window_hours):
    left_time = _timestamp(left)
    right_time = _timestamp(right)
    if not left_time or not right_time:
        return False
    return abs((left_time - right_time).total_seconds()) <= window_hours * 3600


def same_story(left, right, config):
    """Return true when two records are probable duplicates in the configured window.

    URL/upstream-ID equality is strongest. Otherwise the decision requires
    title/body evidence so that two unrelated headlines about the same person
    are not collapsed merely because they were published close together.
    """
    window_hours = int(config.get("window_hours", 6))
    if not within_window(left, right, window_hours):
        return False

    left_url = normalize_url(left.get("url"))
    right_url = normalize_url(right.get("url"))
    if left_url and right_url and left_url == right_url:
        return True

    if (left.get("channel_id") == right.get("channel_id")
            and left.get("upstream_id") and left.get("upstream_id") == right.get("upstream_id")):
        return True

    left_title = normalize_text(left.get("title"))
    right_title = normalize_text(right.get("title"))
    if not left_title or not right_title:
        return False
    if left_title == right_title and len(left_title) >= int(config.get("min_title_chars", 6)):
        return True

    title_score = similarity(left_title, right_title)
    title_threshold = float(config.get("title_similarity", 0.86))
    if title_score >= title_threshold and min(len(left_title), len(right_title)) >= int(config.get("min_title_chars", 6)):
        return True

    left_body = normalize_text(left.get("content"))
    right_body = normalize_text(right.get("content"))
    if len(left_body) < int(config.get("min_content_chars", 80)) or len(right_body) < int(config.get("min_content_chars", 80)):
        return False
    body_score = similarity(left_body, right_body)
    body_threshold = float(config.get("content_similarity", 0.76))
    return body_score >= body_threshold and title_score >= float(config.get("related_title_similarity", 0.48))
