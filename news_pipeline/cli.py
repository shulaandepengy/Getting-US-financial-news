import argparse
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

from . import feishu
from .judge import assess
from .sources import collect
from .store import Store, digest


def load_config(path):
    path = Path(path).resolve()
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    for name in ("poll_seconds", "lookback_hours", "recheck_seconds", "retry_seconds",
                 "max_targets_per_cycle", "max_candidates", "max_input_chars", "max_output_tokens"):
        if type(config.get(name)) is not int or config[name] <= 0:
            raise ValueError(name + " must be a positive integer")
    for name in ("database", "skill_directory", "officeholders_file"):
        if config.get(name):
            config[name] = str((path.parent / config[name]).resolve())
    sources = config["sources"]
    ids = [source["id"] for source in sources]
    if not sources or len(set(ids)) != len(ids) or not {"cls", "jin10", "us_24h"}.issubset(ids):
        raise ValueError("source IDs must be unique and include cls, jin10, us_24h")
    load_dotenv(path.parent / ".env", override=False)
    return config


def semantic_result(article, result):
    # Exclude changing timestamps and free-form reason wording from notification identity.
    return {"content": article["content"], "title": article["title"], "relevant": result["relevant"],
            "entities": sorted((e["category"], e["actor_name"] or "", e["office_status"], e["attribution"]) for e in result["entities"]),
            "facts": sorted((f["claim_text"], f["status"],
                             (f["exclusive_origin"] or {}).get("name", ""),
                             sorted(c["source_id"] for c in f["observed_channels"]),
                             sorted((c["name"], c["verification"]) for c in f["origin_sources"])) for f in result["facts"]),
            "gaps": sorted(result["coverage"]["gaps"])}


def run_cycle(config, store):
    end = datetime.now(timezone.utc)
    start = (end - timedelta(hours=config["lookback_hours"])).isoformat()
    end = end.isoformat()
    with ThreadPoolExecutor(max_workers=min(4, len(config["sources"]))) as executor:
        outputs = list(executor.map(lambda source: collect(source, start, end), config["sources"]))
    coverage = []
    for records, state in outputs:
        coverage.append(state)
        print(f"来源 {state['name']}：{state['status']}，收到 {len(records)} 条", flush=True)
        for record in records:
            store.save_article(record)
    rows = store.recent(start)
    if not rows:
        print("当前没有可判断新闻；请配置真实新闻源。", flush=True)
        return
    officeholders = []
    if config.get("officeholders_file"):
        officeholders = json.loads(Path(config["officeholders_file"]).read_text(encoding="utf-8-sig"))
    processed = 0
    for row in rows:
        now = time.time()
        if now - row["last_attempt"] < config["retry_seconds"]:
            continue
        if now - row["last_success"] < config["recheck_seconds"]:
            continue
        if processed >= config["max_targets_per_cycle"]:
            break
        processed += 1
        store.attempted(row["id"], now)
        target = json.loads(row["body"])
        candidates = [json.loads(other["body"]) for other in rows if other["id"] != row["id"]]
        # No language-specific keyword filter: all recent evidence is eligible.
        candidates.sort(key=lambda record: record["published_at"] or record["fetched_at"], reverse=True)
        retrieval_gaps = []
        if len(candidates) > config["max_candidates"]:
            retrieval_gaps.append("候选数量超过上限，未完整比对观察窗口")
            candidates = candidates[:config["max_candidates"]]
        payload = {"as_of": end, "window_start": start,
                   "monitoring_scope": {"expected": [{"id": s["id"], "name": s["name"]} for s in config["sources"]],
                                        "enabled": [s["source_id"] for s in coverage if s["status"] != "not_configured"]},
                   "coverage": coverage, "target": target, "candidates": candidates,
                   "official_evidence": [], "officeholders": officeholders,
                   "previous_assessment": json.loads(row["latest"]) if row["latest"] else None,
                   "retrieval_gaps": retrieval_gaps}
        while len(json.dumps(payload, ensure_ascii=False)) > config["max_input_chars"] and payload["candidates"]:
            payload["candidates"].pop()
            if "输入大小超过预算，候选存在截断" not in retrieval_gaps:
                retrieval_gaps.append("输入大小超过预算，候选存在截断")
        if len(json.dumps(payload, ensure_ascii=False)) > config["max_input_chars"]:
            print(f"记录 {row['id']} 原文超过输入预算，保留原文待调整配置。", flush=True)
            continue
        try:
            print("开始判断 " + row["id"], flush=True)
            result = assess(config, payload)
            notification = feishu.render(target, result) if result["relevant"] else None
            # A correction that invalidates a previously relevant article must be visible.
            previous = payload["previous_assessment"]
            if not result["relevant"] and previous and previous["relevant"]:
                notification = f"Getting US financial news\n记录 {row['id']} 分类更正：本条不属于监控范围。\n判定时间：{end}\n原文：{target['content']}"
            store.save_assessment(target, result, time.time(), notification, digest(semantic_result(target, result)))
            print(f"记录 {row['id']}：{result['article_status']}，结果已保存。", flush=True)
        except Exception as exc:
            print(f"记录 {row['id']} 判断失败，保留并重试：{type(exc).__name__}", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Getting US financial news")
    parser.add_argument("command", choices=["doctor", "once", "run", "outbox", "flush"])
    parser.add_argument("--config", default="config.local.json")
    parser.add_argument("--send", action="store_true", help="send pending notifications to configured Feishu robot")
    args = parser.parse_args()
    config = load_config(args.config)
    if args.command == "doctor":
        for source in config["sources"]:
            ready = bool(source.get("url") or os.getenv(source.get("url_env", "")))
            print(source["name"] + ("：已配置端点（未验证连接）" if ready else "：未配置端点"))
        for env in ("DEEPSEEK_API_KEY", "FEISHU_WEBHOOK_URL"):
            print(env + ("：已配置" if os.getenv(env) else "：未配置"))
        print("本命令不请求网络、不调用模型、不发送消息。")
        return
    store = Store(config["database"])
    try:
        if args.command == "outbox":
            for row in store.db.execute("SELECT id,text,sent FROM outbox ORDER BY id"):
                print(f"[outbox {row['id']} sent={row['sent']}]\n{row['text']}\n")
            return
        if args.command == "flush":
            if not args.send:
                parser.error("flush requires --send")
            feishu.drain(store, config["retry_seconds"])
            return
        if not os.getenv("DEEPSEEK_API_KEY"):
            parser.error("set DEEPSEEK_API_KEY before once/run")
        while True:
            run_cycle(config, store)
            if args.send:
                feishu.drain(store, config["retry_seconds"])
            if args.command == "once":
                return
            print(f"本轮完成；{config['poll_seconds']} 秒后继续。", flush=True)
            time.sleep(config["poll_seconds"])
    except KeyboardInterrupt:
        print("已停止；待处理记录保留。")
    finally:
        store.close()


if __name__ == "__main__":
    main()
