"""Command-line orchestration for one scheduled news-sampling run."""

import argparse
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

from . import feishu
from .judge import assess
from .sources import collect
from .store import Store, digest


def _positive_int(config, name):
    if type(config.get(name)) is not int or config[name] <= 0:
        raise ValueError(name + " must be a positive integer")


def load_config(path):
    path = Path(path).resolve()
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    # Keep old local templates usable while moving the runtime to one-shot runs.
    if "sample_hours" not in config and "lookback_hours" in config:
        config["sample_hours"] = config["lookback_hours"]
    config.setdefault("dedup_window_hours", 6)
    config.setdefault("source_workers", 4)
    config.setdefault("max_input_chars", 100000)
    config.setdefault("max_output_tokens", 4000)
    config.setdefault("deepseek_timeout_seconds", 180)
    config.setdefault("retry_seconds", 300)
    config.setdefault("max_news_items_per_run", 100)
    config.setdefault("duplicate_source_preview_chars", 1500)
    config.setdefault("retention_days", 30)
    config.setdefault("send_irrelevant", False)
    if not isinstance(config.get("dedup", {}), dict):
        raise ValueError("dedup must be an object")
    config.setdefault("dedup", {})
    # The top-level value is the user-facing setting; keep the nested matcher
    # configuration synchronized if only dedup_window_hours is edited.
    config["dedup"]["window_hours"] = config["dedup_window_hours"]
    for name in (
        "sample_hours", "dedup_window_hours", "source_workers", "max_input_chars",
        "max_output_tokens", "deepseek_timeout_seconds", "retry_seconds",
        "max_news_items_per_run", "duplicate_source_preview_chars", "retention_days",
    ):
        _positive_int(config, name)
    if not isinstance(config.get("send_irrelevant"), bool):
        raise ValueError("send_irrelevant must be boolean")
    for name in ("database", "skill_directory", "officeholders_file"):
        if config.get(name):
            config[name] = str((path.parent / config[name]).resolve())
    if not config.get("database") or not config.get("skill_directory"):
        raise ValueError("database and skill_directory are required")
    sources = config.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("sources must be a non-empty list")
    if any(not isinstance(source, dict) for source in sources):
        raise ValueError("every source must be an object")
    ids = [source.get("id") for source in sources]
    if any(not value for value in ids):
        raise ValueError("every source needs an id")
    if len(set(ids)) != len(ids):
        raise ValueError("source IDs must be unique")
    for source in sources:
        if not source.get("name") or source.get("kind") not in ("json", "rss", "cls", "jin10"):
            raise ValueError("every source needs name and kind=json/rss/cls/jin10")
    load_dotenv(path.parent / ".env", override=False)
    return config


def _source_payload(item, config):
    """Keep the canonical article complete and provide every duplicate source."""
    preview = config["duplicate_source_preview_chars"]
    result = []
    for source in item["sources"]:
        value = dict(source)
        if not source["is_primary"]:
            value["content"] = source["content"][:preview]
            value["content_truncated"] = len(source["content"]) > preview
        result.append(value)
    return result


def build_payload(item, coverage, window_start, window_end, config, sample_hours=None):
    canonical = dict(item["canonical"])
    payload = {
        "news_item_id": item["id"],
        "sample_window": {"start": window_start, "end": window_end,
                           "hours": sample_hours or config["sample_hours"]},
        "deduplication": {"window_hours": item["dedup_window_hours"],
                           "source_count": len(item["sources"])},
        "coverage": coverage,
        "target": canonical,
        "sources": _source_payload(item, config),
    }
    serialized = json.dumps(payload, ensure_ascii=False)
    if len(serialized) > config["max_input_chars"]:
        raise ValueError("input exceeds max_input_chars")
    return payload


def run_once(config, store, sample_hours=None, send=True):
    hours = sample_hours or config["sample_hours"]
    if type(hours) is not int or hours <= 0:
        raise ValueError("sample_hours must be a positive integer")
    window_end_dt = datetime.now(timezone.utc)
    window_start_dt = window_end_dt - timedelta(hours=hours)
    window_start = window_start_dt.isoformat()
    window_end = window_end_dt.isoformat()
    run_id = store.start_run(window_start, window_end, config)
    judgement_count = 0
    judgement_failures = 0
    sent_count = 0
    item_ids = set()
    coverage = []
    try:
        worker_count = min(config["source_workers"], len(config["sources"]))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            outputs = list(executor.map(
                lambda source: collect(source, window_start, window_end), config["sources"]
            ))
        records = []
        for source_records, state in outputs:
            records.extend(source_records)
            coverage.append(state)
            print(f"来源 {state['name']}：{state['status']}，窗口内收到 {len(source_records)} 条", flush=True)
        store.save_coverage(run_id, coverage, len(records))
        changed = store.save_articles(records)
        item_ids.update(store.ingest_news_items(records, changed, config["dedup"]))
        item_ids.update(store.pending_news_item_ids(
            window_start, window_end, config["max_news_items_per_run"]
        ))
        item_ids = set(sorted(item_ids)[:config["max_news_items_per_run"]])
        print(f"本轮原始记录 {len(records)} 条，六小时去重后待判断 {len(item_ids)} 条", flush=True)

        if send:
            sent_count += feishu.drain(store, config["retry_seconds"])
        model = os.getenv("DEEPSEEK_MODEL") or "deepseek-chat"
        for item_id in sorted(item_ids):
            item = store.get_news_item(item_id)
            if not item:
                continue
            try:
                payload = build_payload(item, coverage, window_start, window_end, config, hours)
                result = assess(config, payload)
                input_hash = digest(payload)
                judgement_id, fingerprint = store.save_judgement(
                    run_id, item_id, result=result, model=model, input_hash=input_hash
                )
                judgement_count += 1
                if result["relevant"] or config["send_irrelevant"]:
                    text = feishu.render(item, result, {
                        "window_start": window_start,
                        "window_end": window_end,
                    })
                    store.enqueue_outbox(item_id, judgement_id, text, fingerprint)
                print(f"判断 {item_id}：{'相关' if result['relevant'] else '不相关'}，已入库", flush=True)
            except Exception as exc:
                judgement_failures += 1
                store.save_judgement(
                    run_id, item_id, result=None, model=model,
                    error=type(exc).__name__
                )
                print(f"判断 {item_id} 失败，保留待下轮重试：{type(exc).__name__}", flush=True)

        if send:
            sent_count += feishu.drain(store, config["retry_seconds"])
        cutoff = (datetime.now(timezone.utc) - timedelta(days=config["retention_days"])).isoformat()
        removed = store.prune(cutoff)
        source_failures = sum(state["status"] == "failed" for state in coverage)
        status = "completed_with_errors" if source_failures or judgement_failures else "completed"
        store.finish_run(run_id, status, len(item_ids), judgement_count, sent_count)
        print(f"本轮完成：入库判断 {judgement_count} 条，发送 {sent_count} 条，清理旧记录 {removed} 个。", flush=True)
        return {
            "run_id": run_id,
            "records": len(records),
            "news_items": len(item_ids),
            "judgements": judgement_count,
            "judgement_failures": judgement_failures,
            "sent": sent_count,
            "status": status,
        }
    except Exception as exc:
        store.finish_run(run_id, "failed", len(item_ids), judgement_count, sent_count, type(exc).__name__)
        raise


def main():
    parser = argparse.ArgumentParser(description="Scheduled US financial news sampling")
    parser.add_argument("command", choices=["doctor", "once", "outbox", "flush"])
    parser.add_argument("--config", default="config.example.json")
    parser.add_argument("--sample-hours", type=int, help="override configured sample_hours for this run")
    parser.add_argument("--no-send", action="store_true", help="do not send Feishu messages; keep them in the outbox")
    args = parser.parse_args()
    config = load_config(args.config)
    if args.command == "doctor":
        for source in config["sources"]:
            ready = bool(source.get("url") or os.getenv(source.get("url_env", "")))
            print(source["name"] + ("：已配置端点（未验证连接）" if ready else "：未配置端点"))
        for env in ("DEEPSEEK_API_KEY", "FEISHU_WEBHOOK_URL"):
            print(env + ("：已配置" if os.getenv(env) else "：未配置"))
        print(f"采样周期：每次 {config['sample_hours']} 小时；去重窗口：{config['dedup_window_hours']} 小时。")
        print("本命令不请求网络、不调用模型、不发送消息。")
        return

    store = Store(config["database"])
    try:
        if args.command == "outbox":
            for row in store.outbox_rows():
                print(f"[outbox {row['id']} {row['status']} attempts={row['attempts']}]\n{row['text']}\n")
            return
        if args.command == "flush":
            if not os.getenv("FEISHU_WEBHOOK_URL"):
                parser.error("set FEISHU_WEBHOOK_URL before flush")
            sent = feishu.drain(store, config["retry_seconds"])
            print(f"发送完成：{sent} 条。")
            return
        if not os.getenv("DEEPSEEK_API_KEY"):
            parser.error("set DEEPSEEK_API_KEY before once")
        if not args.no_send and not os.getenv("FEISHU_WEBHOOK_URL"):
            parser.error("set FEISHU_WEBHOOK_URL or use --no-send")
        run_once(config, store, sample_hours=args.sample_hours, send=not args.no_send)
    finally:
        store.close()


if __name__ == "__main__":
    main()
