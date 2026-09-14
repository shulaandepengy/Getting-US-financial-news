"""Direct Feishu custom-bot delivery with a durable retry queue."""

import base64
import hashlib
import hmac
import os
import time
from urllib.parse import urlparse

from .sources import request_json


LABELS = {
    "treasury_secretary": "美国财政部长",
    "treasury_other": "美国财政部其他人员/机构",
    "fed_chair": "美联储主席",
    "fed_other": "美联储其他人员/机构",
    "president": "美国总统",
    "white_house_other": "白宫其他人员/机构",
}
ACTION_LABELS = {
    "statement": "表态/声明",
    "decision": "决定",
    "policy_change": "政策变化",
    "proposal": "提议",
    "plan": "计划",
    "meeting": "会见/会议",
    "appointment": "任命",
    "warning": "警告",
    "data_release": "数据发布",
    "other": "其他动作",
}


def render(item, result, run_info):
    canonical = item["canonical"]
    lines = [
        "美国金融新闻监测",
        "去重记录：" + item["id"],
        "标题：" + (canonical["title"] or "无标题"),
        "发布时间：" + (item["first_published_at"] or "未知"),
        "采样窗口：" + run_info["window_start"] + " 至 " + run_info["window_end"],
        "来源数量：" + str(len(item["sources"])) + "（六小时内重复来源已合并）",
    ]
    for source in item["sources"]:
        label = "主记录" if source["is_primary"] else "重复来源"
        lines.append(
            f"来源[{label}]：{source['channel_name']} | {source['published_at'] or '未知'} | {source['url'] or '无链接'}"
        )
    lines.extend(["DeepSeek 判断：" + ("相关" if result["relevant"] else "不相关"),
                  "判断依据：" + result["reason"]])
    if result["subjects"]:
        lines.append("主体：")
        for subject in result["subjects"]:
            lines.append(
                f"- {LABELS[subject['category']]}｜{subject['name']}｜身份：{subject['role']}｜证据：{subject['evidence_quote']}"
            )
    if result["actions"]:
        lines.append("动作：")
        for action in result["actions"]:
            lines.append(
                f"- {ACTION_LABELS[action['action_type']]}｜{action['actor']}｜{action['status']}｜{action['description']}"
                + (f"｜对象：{action['object']}" if action["object"] else "")
                + f"｜证据：{action['evidence_quote']}"
            )
    lines.extend(["原文：", canonical["content"]])
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
        string_to_sign = f"{timestamp}\n{secret}"
        body["timestamp"] = timestamp
        body["sign"] = base64.b64encode(
            hmac.new(string_to_sign.encode("utf-8"), b"", hashlib.sha256).digest()
        ).decode("ascii")
    response = request_json(url, body, timeout=30)
    if not isinstance(response, dict) or response.get("code", response.get("StatusCode")) != 0:
        raise ValueError("Feishu did not acknowledge success")


def drain(store, retry_seconds, max_messages=100):
    """Send pending messages now; failed messages remain pending for a later run."""
    sent = 0
    while sent < max_messages:
        now = time.time()
        row = store.pending_outbox(now, retry_seconds)
        if row is None:
            return sent
        store.mark_outbox_attempt(row["id"], now)
        try:
            send(row["text"])
        except Exception as exc:
            store.mark_outbox_failed(row["id"], type(exc).__name__)
            print("飞书发送失败，保留队列待下次重试：" + type(exc).__name__, flush=True)
            return sent
        store.mark_outbox_sent(row["id"])
        sent += 1
        # Avoid hammering the webhook when one run has many long messages.
        time.sleep(0.2)
    return sent
