import base64
import hashlib
import hmac
import os
import time
from urllib.parse import urlparse

from .sources import request_json


LABELS = {"treasury_secretary": "美国财政部长", "treasury_other": "财政部其他人员/机构",
          "fed_chair": "美联储主席", "fed_other": "美联储其他人员/机构",
          "president": "美国总统", "white_house_other": "白宫其他人员/机构"}
STATES = {"non_exclusive": "非独家", "exclusive_supported": "独家证据支持（限监控范围）",
          "single_source_observed": "暂见单一来源，独家未确认", "insufficient_evidence": "证据不足，独家未确认"}


def render(article, result):
    lines = ["Getting US financial news", "记录：" + article["id"], "标题：" + article["title"],
             "采集渠道：" + article["channel_name"], "链接：" + (article["url"] or "未知"),
             "发布时间：" + (article["published_at"] or "未知"), "判定时间：" + result["as_of"]]
    for entity in result["entities"]:
        lines.append(f"主体：{LABELS[entity['category']]} / {entity['actor_name'] or '未知'} / {entity['office_status']} / {entity['attribution']}")
    for index, fact in enumerate(result["facts"], 1):
        lines.extend([f"事实 {index}：{fact['claim_text']}", "判断：" + STATES[fact["status"]],
                      "已知传播渠道：" + ("、".join(c["name"] for c in fact["observed_channels"]) or "未知"),
                      "原始来源：" + ("、".join(c["name"] + "（" + c["verification"] + "）" for c in fact["origin_sources"]) or "未知")])
        if fact["exclusive_origin"]:
            lines.append("独家归属：" + fact["exclusive_origin"]["name"])
        lines.extend(["依据：" + fact["reason"], "限制：" + ("；".join(fact["limitations"]) or "限本次监控范围")])
        for evidence in fact["evidence"]:
            lines.append(f"证据 {evidence['id']}：{evidence['quote']}")
    lines.append("覆盖缺口：" + ("；".join(result["coverage"]["gaps"]) or "无已知缺口"))
    lines.extend(["新闻原文：", article["content"]])
    return "\n".join(lines)


def send(text):
    url = os.getenv("FEISHU_WEBHOOK_URL", "")
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "open.feishu.cn" or not parsed.path.startswith("/open-apis/bot/v2/hook/"):
        raise ValueError("valid FEISHU_WEBHOOK_URL required")
    body = {"msg_type": "text", "content": {"text": text}}
    secret = os.getenv("FEISHU_SECRET")
    if secret:
        timestamp = str(int(time.time()))
        body["timestamp"] = timestamp
        body["sign"] = base64.b64encode(hmac.new(f"{timestamp}\n{secret}".encode(), b"", hashlib.sha256).digest()).decode()
    response = request_json(url, body, timeout=30)
    if response.get("code", response.get("StatusCode")) != 0:
        raise ValueError("Feishu did not acknowledge success")


def drain(store, retry_seconds):
    while True:
        now = time.time()
        row = store.pending(now, retry_seconds)
        if row is None:
            return
        with store.db:
            store.db.execute("UPDATE outbox SET last_attempt=? WHERE id=?", (now, row["id"]))
        try:
            send(row["text"])
        except Exception as exc:
            print("飞书发送未确认，将重试：" + type(exc).__name__, flush=True)
            return
        with store.db:
            store.db.execute("UPDATE outbox SET sent=1 WHERE id=?", (row["id"],))
        time.sleep(1)
