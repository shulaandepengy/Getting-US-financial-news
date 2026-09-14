# Getting US financial news

这是一个“定时采样 → 六小时内去重 → DeepSeek 判断主体和动作 → 飞书发送”的 Python 程序。每次运行只执行一轮，定时由 GitHub Actions 负责。

## 运行逻辑

1. 每轮并发请求配置中的新闻源；每个源只接收最近 `sample_hours` 小时的新闻。
2. 所有原始文章先保存到 SQLite。相同新闻在 `dedup.window_hours` 内合并成一个 `news_item`，原始文章不会丢失，所有渠道都会挂在该条新闻下。
3. 对本轮新增或发生变化的去重新闻调用 DeepSeek，提取监控主体、主体身份和动作；判断 JSON 与证据引文一起保存。
4. 相关新闻进入飞书发送队列，本轮直接发送；发送失败的消息保留在队列，下一轮自动重试。

程序不会在本地常驻轮询。`sample_hours` 是每次采样窗口，不是秒级轮询间隔；GitHub Actions 的 cron 应与它保持一致。

## 本地运行

需要 Python 3.11+：

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e .
Copy-Item .env.example .env
Copy-Item config.example.json config.local.json
```

在 `.env` 填入 `DEEPSEEK_API_KEY`、`FEISHU_WEBHOOK_URL`，必要时填 `FEISHU_SECRET`。`config.local.json` 中最重要的参数是：

```json
{
  "sample_hours": 6,
  "dedup_window_hours": 6,
  "database": "data/news.sqlite3"
}
```

运行检查和单轮采样：

```powershell
.venv\Scripts\python -m news_pipeline.cli doctor --config config.local.json
.venv\Scripts\python -m news_pipeline.cli once --config config.local.json
```

本地只测试采集和入库、不发送飞书：

```powershell
.venv\Scripts\python -m news_pipeline.cli once --config config.local.json --no-send
```

查看发送队列或手动重试：

```powershell
.venv\Scripts\python -m news_pipeline.cli outbox --config config.local.json
.venv\Scripts\python -m news_pipeline.cli flush --config config.local.json
```

## GitHub Actions

`.github/workflows/news-pipeline.yml` 默认每 6 小时运行一次，也支持手动触发。先在 GitHub 仓库 Settings → Secrets and variables → Actions 中配置：

- `DEEPSEEK_API_KEY`
- `FEISHU_WEBHOOK_URL`
- 可选：`DEEPSEEK_BASE_URL`、`DEEPSEEK_MODEL`、`FEISHU_SECRET`

工作流会把 `data/news.sqlite3` 提交回仓库，使下一轮能够继续做六小时去重、保存历史判断并重试飞书消息。当前仓库原本是公开仓库；SQLite 中含有新闻原文，正式启用前建议将仓库设为 Private，或把 `database` 改接到你自己的私有数据库/存储服务。

如要改为每 3 小时运行，需要同时修改工作流里的 cron `0 */3 * * *` 和 `sample_hours`，或者手动运行时使用 `--sample-hours 3`。GitHub Actions 的 schedule 只决定什么时候启动，程序参数决定每轮查询多长时间。

## 数据库设计

数据库表先按以下结构确定，后续可以直接修改：

| 表 | 作用 |
|---|---|
| `sampling_runs` | 每次采样的时间窗口、源覆盖情况、运行结果 |
| `raw_articles` | 每个渠道的原始文章，保留标题、正文、链接和内容哈希 |
| `news_items` | 六小时去重后的唯一新闻记录，指向一条主记录 |
| `news_item_sources` | 去重新闻与所有原始来源的关联；`is_primary=1` 为主记录 |
| `judgements` | 每次 DeepSeek 判断的完整 JSON、主体、动作、模型和失败信息 |
| `feishu_outbox` | 待发送/已发送消息、重试次数和错误信息 |

这样既能让业务上只保留一条去重新闻，又能保留财联社、金十数据和其他渠道的全部来源证据。原始文章和 DeepSeek 判断均可从 SQLite 导出。

## DeepSeek 判断内容

当前只要求 DeepSeek 判断：

- 主体：美国财政部长、财政部其他人员/机构、美联储主席、美联储其他人员/机构、美国总统、白宫其他人员/机构。
- 动作：声明、决定、政策变化、提议、计划、会议、任命、警告、数据发布等。
- 每个主体和动作必须带来源记录 ID 与原文短引文；程序会校验引文确实存在于输入正文。

判断规则位于 [skills/news-subject-action/SKILL.md](skills/news-subject-action/SKILL.md)。新闻源字段映射见 [docs/source-contract.md](docs/source-contract.md)，数据库字段说明见 [docs/database.md](docs/database.md)。
