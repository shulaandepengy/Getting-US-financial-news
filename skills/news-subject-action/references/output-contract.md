# DeepSeek 输出契约

返回对象必须符合下面的结构。`news_item_id` 必须原样复制输入值，`schema_version` 固定为 `2.0`。

```json
{
  "schema_version": "2.0",
  "news_item_id": "item-...",
  "relevant": true,
  "reason": "原文明确报道了目标主体的实际动作。",
  "subjects": [
    {
      "category": "fed_chair",
      "name": "某某",
      "role": "current",
      "evidence_source_id": "fed:...",
      "evidence_quote": "逐字引文"
    }
  ],
  "actions": [
    {
      "action_type": "statement",
      "actor": "某某",
      "description": "表示将继续关注通胀走势",
      "object": "通胀走势",
      "status": "confirmed",
      "evidence_source_id": "fed:...",
      "evidence_quote": "逐字引文"
    }
  ]
}
```

允许的主体类别：`treasury_secretary`、`treasury_other`、`fed_chair`、`fed_other`、`president`、`white_house_other`。

允许的身份状态：`current`、`former`、`incoming`、`acting`、`unknown`、`institution`。

允许的动作类型：`statement`、`decision`、`policy_change`、`proposal`、`plan`、`meeting`、`appointment`、`warning`、`data_release`、`other`。

允许的动作状态：`confirmed`、`planned`、`considering`、`reported`、`denied`、`unknown`。

范围外新闻必须返回 `relevant=false`、`subjects=[]`、`actions=[]`。不要增加未定义字段，不要给出百分比置信度，不要生成输入中没有的日期、链接、姓名或事实。
