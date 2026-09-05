import json
import os
from pathlib import Path

from jsonschema import validate

from .sources import request_json


STATES = ["non_exclusive", "exclusive_supported", "single_source_observed", "insufficient_evidence"]
CATEGORIES = ["treasury_secretary", "treasury_other", "fed_chair", "fed_other", "president", "white_house_other"]


def obj(properties, required=None):
    return {"type": "object", "properties": properties, "required": list(properties) if required is None else required}


def array(item):
    return {"type": "array", "items": item}


STRING = {"type": "string"}
NULLABLE = {"type": ["string", "null"]}
STRINGS = array(STRING)
SOURCE = obj({"name": STRING, "evidence_ids": {"type": "array", "items": STRING, "minItems": 1}, "url": NULLABLE})
SCHEMA = obj({
    "schema_version": {"const": "1.0"}, "target_id": STRING, "event_id": NULLABLE,
    "as_of": STRING, "relevant": {"type": "boolean"},
    "article_status": {"enum": STATES + ["mixed", "out_of_scope"]},
    "entities": array(obj({
        "category": {"enum": CATEGORIES}, "actor_name": NULLABLE,
        "office_status": {"enum": ["current", "former", "incoming", "acting", "unknown", "institution"]},
        "attribution": {"enum": ["direct", "relayed", "institutional", "uncertain"]},
        "relay_actor": NULLABLE, "evidence_id": STRING, "quote": STRING})),
    "unresolved_entities": {"type": "array"},
    "facts": array(obj({
        "fact_id": STRING, "claim_text": STRING, "categories": array({"enum": CATEGORIES}),
        "status": {"enum": STATES}, "exclusive_origin": {"anyOf": [{"type": "null"}, SOURCE]},
        "claimed_exclusive_by": STRINGS,
        "observed_channels": array(obj({"source_id": STRING, "name": STRING,
                                         "evidence_ids": {"type": "array", "items": STRING, "minItems": 1}})),
        "origin_sources": array(obj({"source_id": NULLABLE, "name": STRING,
                                     "verification": {"enum": ["directly_verified", "attributed_only", "unknown"]},
                                     "evidence_ids": STRINGS, "url": NULLABLE})),
        "attribution_chain": array(obj({"from": STRING, "to": STRING, "evidence_id": STRING, "quote": STRING})),
        "independent_origin_count": {"type": ["integer", "null"], "minimum": 0},
        "matches": array(obj({"candidate_id": STRING,
                              "relation": {"enum": ["same_fact", "related_update", "background_only", "contradiction", "unrelated"]},
                              "reason": STRING})),
        "evidence": {"type": "array", "minItems": 1, "items": obj({"id": STRING, "quote": STRING, "supports": STRING})},
        "reason": STRING, "limitations": STRINGS, "previous_status": {"enum": STATES + [None]},
        "change_reason": NULLABLE, "display_text": STRING})),
    "coverage": obj({"complete": {"type": "boolean"}, "checked_source_ids": STRINGS,
                     "gaps": STRINGS, "window_start": NULLABLE, "window_end": NULLABLE}),
    "needs_recheck": {"type": "boolean"}
})


def check_evidence(result, evidence):
    records = {record["id"]: record for record in evidence}
    def check_quote(record_id, quote):
        record = records[record_id]
        if not quote.strip() or quote not in record["title"] + "\n" + record["content"]:
            raise ValueError("evidence quote absent from input")
    def walk(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in ("evidence_id", "candidate_id") and child not in records:
                    raise ValueError("unknown evidence ID")
                if key == "evidence_ids" and any(item not in records for item in child):
                    raise ValueError("unknown evidence IDs")
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(result)
    for entity in result["entities"]:
        check_quote(entity["evidence_id"], entity["quote"])
    for fact in result["facts"]:
        same_ids = {match["candidate_id"] for match in fact["matches"] if match["relation"] == "same_fact"}
        permitted = same_ids | {result["target_id"]}
        if not fact["observed_channels"]:
            raise ValueError("fact requires observed channels")
        cited_channels = {rid for channel in fact["observed_channels"] for rid in channel["evidence_ids"]}
        if result["target_id"] not in cited_channels or not cited_channels.issubset(permitted):
            raise ValueError("observed channels must refer to target or same-fact matches")
        if not same_ids.issubset(cited_channels):
            raise ValueError("same-fact source omitted from observed channels")
        for evidence_item in fact["evidence"]:
            check_quote(evidence_item["id"], evidence_item["quote"])
        for edge in fact["attribution_chain"]:
            check_quote(edge["evidence_id"], edge["quote"])
        for channel in fact["observed_channels"]:
            if any(records[rid]["channel_id"] != channel["source_id"]
                   or records[rid]["channel_name"] != channel["name"] for rid in channel["evidence_ids"]):
                raise ValueError("channel attribution disagrees with collected records")
        for origin in fact["origin_sources"] + ([fact["exclusive_origin"]] if fact["exclusive_origin"] else []):
            if origin["url"] and not any(origin["url"] == r["url"] or origin["url"] in r["content"] for r in evidence):
                raise ValueError("origin URL absent from input")


def assess(config, payload):
    directory = Path(config["skill_directory"])
    prompt = directory.joinpath("SKILL.md").read_text(encoding="utf-8") + "\n\n" + directory.joinpath(
        "references/output-contract.md").read_text(encoding="utf-8")
    prompt += "\n\n必须符合以下 JSON Schema：\n" + json.dumps(SCHEMA, ensure_ascii=False)
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise ValueError("DEEPSEEK_API_KEY is required")
    response = request_json(os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/") + "/chat/completions",
                            {"model": os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro"),
                             "messages": [{"role": "system", "content": prompt},
                                          {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                             "response_format": {"type": "json_object"},
                             "max_tokens": config["max_output_tokens"]},
                            {"Authorization": "Bearer " + key}, timeout=180)
    choice = response["choices"][0]
    if choice.get("finish_reason") != "stop":
        raise ValueError("incomplete model response")
    result = json.loads(choice["message"]["content"])
    validate(result, SCHEMA)
    if result["target_id"] != payload["target"]["id"]:
        raise ValueError("target mismatch")
    check_evidence(result, [payload["target"], *payload["candidates"], *payload["official_evidence"]])
    if result["relevant"] != bool(result["facts"]):
        raise ValueError("relevance and facts disagree")
    fact_ids = [fact["fact_id"] for fact in result["facts"]]
    if len(set(fact_ids)) != len(fact_ids):
        raise ValueError("duplicate fact ID")
    # Coverage and timestamps are computed by the program, never by the model.
    gaps = [f"{source['name']}: {gap}" for source in payload["coverage"] for gap in source["gaps"]]
    gaps.extend(payload.get("retrieval_gaps", []))
    complete = not gaps and all(source["status"] == "ok" and source["pagination_complete"] for source in payload["coverage"])
    result["coverage"] = {"complete": complete,
                          "checked_source_ids": [source["source_id"] for source in payload["coverage"] if source["status"] == "ok"],
                          "gaps": gaps,
                          "window_start": payload["window_start"] if complete else None,
                          "window_end": payload["as_of"] if complete else None}
    result["as_of"] = payload["as_of"]
    result["event_id"] = payload["target"]["id"]
    for fact in result["facts"]:
        if not fact["categories"]:
            raise ValueError("relevant fact requires category")
        if fact["status"] == "exclusive_supported" and not fact["exclusive_origin"]:
            raise ValueError("exclusive claim requires origin evidence")
        if not complete and fact["status"] != "non_exclusive":
            fact.update(status="insufficient_evidence", exclusive_origin=None,
                        display_text="证据不足，独家未确认；来源覆盖或候选检索存在缺口。",
                        reason="程序依据采集与检索状态判定覆盖不完整，不能确认独家。")
            fact["limitations"] = sorted(set(fact["limitations"] + gaps))
        if fact["status"] != "exclusive_supported":
            fact["exclusive_origin"] = None
    states = {fact["status"] for fact in result["facts"]}
    result["article_status"] = "out_of_scope" if not states else next(iter(states)) if len(states) == 1 else "mixed"
    result["needs_recheck"] = not complete or any(state in ("single_source_observed", "insufficient_evidence") for state in states)
    return result
