# SQLite 数据库结构

默认数据库路径为 `data/news.sqlite3`。数据库采用 SQLite，采集 HTTP 请求由多个线程并发执行，写库由主线程在事务中批量完成，以避免 SQLite 多写入者锁冲突。

## 表和关键字段

### `sampling_runs`

记录一次程序启动到结束的完整采样窗口。

- `id`：本轮运行 ID。
- `window_start`、`window_end`：本轮实际请求区间。
- `coverage_json`：每个新闻源的成功/失败、收到数量和覆盖缺口。
- `status`：`running`、`completed`、`completed_with_errors` 或 `failed`。

### `raw_articles`

每个源的原始新闻一条一行。`content` 保留完整正文，`content_hash` 和 `revision` 用于识别正文变化，`channel_id`、`channel_name`、`url` 用于追溯来源。

### `news_items`

六小时去重后的业务新闻。一条业务新闻只保留一个稳定 `id`，`canonical_article_id` 指向用于 DeepSeek 和飞书展示的主记录。主记录优先选择发布时间更早的文章，同一时间优先选择正文更长的文章。

### `news_item_sources`

这是去重关系表：

- 一条 `news_item` 可以关联多个 `raw_articles`。
- 每个原始文章只关联一个 `news_item`。
- `is_primary=1` 表示主记录，其余行都是重复来源。
- 即使业务上只保留一条新闻，财联社、金十数据和其他来源仍全部保留。

### `judgements`

每次 DeepSeek 判断一行，不覆盖旧判断。`result_json` 保存完整 JSON，`subjects_json` 和 `actions_json` 方便后续直接查询，`status=failed` 时保留失败类型，下一轮会重试。

### `feishu_outbox`

相关新闻的飞书消息队列。长消息自动分段；发送成功为 `sent`，失败保留 `pending`，下一轮继续发送。`fingerprint` 防止相同判断和相同来源集合重复通知。

## 后续可修改的位置

- 想把六小时改成其他窗口：改 `sample_hours` 和 GitHub workflow 的 cron。
- 想把重复判断变严格/宽松：改 `config.github.json` 的 `dedup` 阈值。
- 想保留更久历史：改 `retention_days`。
- 想把判断结果拆成更多字段：在 `judgements` 增加列，同时保留 `result_json` 作为兼容兜底。
- 想换 PostgreSQL、MySQL 或云数据库：保留这些逻辑表，将 `Store` 替换为对应数据库实现即可。
