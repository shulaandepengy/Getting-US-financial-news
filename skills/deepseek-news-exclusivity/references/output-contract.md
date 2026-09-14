# JSON 输出结构与边界示例

## 结构

以下是完整的结构示例，所有名称和 ID 均为演示，不是实际新闻或已接入渠道。调用时替换为输入证据。

```json
{
  "schema_version": "1.0",
  "target_id": "target-1",
  "event_id": null,
  "as_of": "2026-09-05T22:00:00+08:00",
  "relevant": true,
  "article_status": "single_source_observed",
  "entities": [
    {
      "category": "treasury_other",
      "actor_name": "美国财政部发言人",
      "office_status": "institution",
      "attribution": "direct",
      "relay_actor": null,
      "evidence_id": "target-1",
      "quote": "美国财政部发言人表示"
    }
  ],
  "unresolved_entities": [],
  "facts": [
    {
      "fact_id": "target-1:f1",
      "claim_text": "财政部正在考虑调整某项安排",
      "categories": ["treasury_other"],
      "status": "single_source_observed",
      "exclusive_origin": null,
      "claimed_exclusive_by": [],
      "observed_channels": [
        {"source_id": "cls", "name": "财联社", "evidence_ids": ["target-1"]}
      ],
      "origin_sources": [
        {
          "source_id": null,
          "name": "某美国媒体（示例）",
          "verification": "attributed_only",
          "evidence_ids": ["target-1"],
          "url": null
        }
      ],
      "attribution_chain": [
        {"from": "财联社", "to": "某美国媒体（示例）", "evidence_id": "target-1", "quote": "据某美国媒体报道"}
      ],
      "independent_origin_count": null,
      "matches": [],
      "evidence": [
        {"id": "target-1", "quote": "据某美国媒体报道", "supports": "来源归属，尚未核验原始报道"}
      ],
      "reason": "在已完整检查的监控范围内，仅观察到这一来源链，缺少可核验的独家采写证据。",
      "limitations": ["原始报道未直接取得"],
      "previous_status": null,
      "change_reason": null,
      "display_text": "暂仅见财联社转引某美国媒体；独家未确认。"
    }
  ],
  "coverage": {
    "complete": true,
    "checked_source_ids": ["cls", "jin10", "configured_us_source"],
    "gaps": [],
    "window_start": "2026-09-04T22:00:00+08:00",
    "window_end": "2026-09-05T22:00:00+08:00"
  },
  "needs_recheck": true
}
```

## 字段约束

- `event_id`：沿用输入已分配 ID；没有时为 null，由存储层创建，模型不凭空声称已找到历史事件。
- `fact_id`：优先沿用已确认的历史事实 ID；新事实可用 target ID 加序号，由调用端保存映射。不能仅依赖跨次生成序号匹配旧事实。
- `article_status`：所有事实同状态时用该状态；不同状态用 `mixed`；无相关内容用 `out_of_scope`。
- `status`：只允许 `non_exclusive/exclusive_supported/single_source_observed/insufficient_evidence`。
- `exclusive_origin`：null 或含 `name`、`evidence_ids`、`url` 的对象；url 可为 null，但必须有足够可追溯的原始报道证据。
- `verification`：`directly_verified` 表示已收到对应原文；`attributed_only` 表示只在其他报道中见到归属；`unknown` 表示来源未明。
- `matches`：每项含 `candidate_id`、`relation`、`reason`，relation 使用 SKILL.md 中五种关系枚举。只把 `same_fact` 的渠道纳入该事实已观察渠道。
- `coverage.complete`：根据预期渠道和实际成功完整覆盖计算，不能使用模型感觉。渠道各有不同区间时，顶层区间仅表示共同完整覆盖区间；不存在时为 null，并在 gaps 给出各渠道问题。
- `needs_recheck`：未确认、覆盖缺失时为 true；其余是否重查由程序策略决定，不意味着模型已安排后台任务。
- 空列表用 `[]`，未知单值用 null，不能用“无”冒充已确认不存在。
- `display_text` 只描述判断和来源，不改写新闻原文。

## 判断样例

### 两个渠道均引用同一家媒体

财联社、金十数据均写“据媒体 A”，没有取得媒体 A 原文或独家采写证据。完整覆盖下返回 `single_source_observed`，列两家渠道及媒体 A；不是两家独立验证，不能判定媒体 A 独家。如果同时有渠道失败，则为 `insufficient_evidence`。

### 有独家采访原文，后来出现转载

已取得媒体 A 的原始独家采访，明确该事实来自该采访；财联社、金十均注明转引，完整观察中无公开或独立来源反证。返回 `exclusive_supported`，归属媒体 A，同时列出两家传播渠道。展示“媒体 A 原始报道独家证据支持，财联社/金十已转载；限本次监控范围”。

### 官方新闻稿已经公开

已抓取财政部新闻稿，内容与目标事实一致。返回 `non_exclusive`；列出已采集新闻渠道和美国财政部这个原始来源。即使其他渠道当时断线，这份公开证据仍支持非独家。正文无部长个人声明则分类 `treasury_other`。

### 只抓到金十，美国渠道没有配置

返回 `insufficient_evidence`。展示“已观察到金十数据；美国渠道未配置，独家未确认”。不能输出“金十独家”，也不能把未配置渠道写成已经检索。

### 同一篇包含公开决定和新采访细节

事实一为已公开 FOMC 决议，分类 `fed_other`，状态 `non_exclusive`。事实二为主席在某媒体采访中新披露的不同细节，分类 `fed_chair`，单独评估采访的独家证据。两条状态不同则文章为 `mixed`。

### 总统与白宫新闻秘书

“新闻秘书称总统已决定 X”：事实主体可为 `president`，归属 `relayed`，转述人为新闻秘书；“新闻秘书认为 X 的影响有限”是另一事实，分类 `white_house_other`。只出现“白宫正在考虑 X”时不得推断为总统本人决定。

### 最早报道与后续公开

10:00 的判定有证据支持媒体 A 独家；10:20 官方公开同一事实。本次改为 `non_exclusive`，保留 10:00 历史判断，并说明“后续官方公开”。不是把两条记录当作无关新事件，也不是删除原独家来源。

