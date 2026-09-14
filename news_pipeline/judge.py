"""DeepSeek subject and action extraction with evidence validation."""

import json
import os
from pathlib import Path

from jsonschema import validate

from .sources import request_json


CATEGORIES = [
    "treasury_secretary",
    "treasury_other",
    "fed_chair",
    "fed_other",
    "president",
    "white_house_other",
]
ROLES = ["current", "former", "incoming", "acting", "unknown", "institution"]
ACTION_TYPES = [
    "statement",
    "decision",
    "policy_change",
    "proposal",
    "plan",
    "meeting",
    "appointment",
    "warning",
    "data_release",
    "other",
]
ACTION_STATUSES = ["confirmed", "planned", "considering", "reported", "denied", "unknown"]


def obj(properties, required=None):
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties) if required is None else required,
        "additionalProperties": False,
    }


def array(item):
    return {"type": "array", "items": item}


STRING = {"type": "string"}
NULLABLE = {"type": ["string", "null"]}


SCHEMA = obj({
    "schema_version": {"const": "2.0"},
    "news_item_id": STRING,
    "relevant": {"type": "boolean"},
    "reason": STRING,
    "subjects": array(obj({
        "category": {"enum": CATEGORIES},
        "name": STRING,
        "role": {"enum": ROLES},
        "evidence_source_id": STRING,
        "evidence_quote": STRING,
    })),
    "actions": array(obj({
        "action_type": {"enum": ACTION_TYPES},
        "actor": STRING,
        "description": STRING,
        "object": NULLABLE,
        "status": {"enum": ACTION_STATUSES},
        "evidence_source_id": STRING,
        "evidence_quote": STRING,
    })),
})


def _json_content(value):
    value = value.strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        value = "\n".join(lines).strip()
    return json.loads(value)


def _check_quote(records, source_id, quote):
    if source_id not in records:
        raise ValueError("unknown evidence source ID")
    if not quote.strip():
        raise ValueError("empty evidence quote")
    record = records[source_id]
    searchable = (record.get("title") or "") + "\n" + (record.get("content") or "")
    if quote not in searchable:
        raise ValueError("evidence quote absent from input")


def check_evidence(result, payload):
    records = {record["id"]: record for record in payload["sources"]}
    for subject in result["subjects"]:
        _check_quote(records, subject["evidence_source_id"], subject["evidence_quote"])
    for action in result["actions"]:
        _check_quote(records, action["evidence_source_id"], action["evidence_quote"])
    if result["relevant"] != bool(result["subjects"] or result["actions"]):
        raise ValueError("relevance does not match subjects/actions")


def assess(config, payload):
    directory = Path(config["skill_directory"])
    prompt = directory.joinpath("SKILL.md").read_text(encoding="utf-8")
    contract = directory.joinpath("references/output-contract.md")
    if contract.exists():
        prompt += "\n\n" + contract.read_text(encoding="utf-8")
    prompt += "\n\n必须符合以下 JSON Schema：\n" + json.dumps(SCHEMA, ensure_ascii=False)
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise ValueError("DEEPSEEK_API_KEY is required")
    base_url = (os.getenv("DEEPSEEK_BASE_URL") or "https://api.deepseek.com").rstrip("/")
    response = request_json(
        base_url + "/chat/completions",
        {
            "model": os.getenv("DEEPSEEK_MODEL") or "deepseek-chat",
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0,
            "max_tokens": int(config["max_output_tokens"]),
        },
        {"Authorization": "Bearer " + key},
        timeout=int(config.get("deepseek_timeout_seconds", 180)),
    )
    try:
        choice = response["choices"][0]
        result = _json_content(choice["message"]["content"])
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid DeepSeek response") from exc
    validate(result, SCHEMA)
    if result["news_item_id"] != payload["news_item_id"]:
        raise ValueError("news item ID mismatch")
    check_evidence(result, payload)
    return result
