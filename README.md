# Getting US financial news

采集财联社、金十数据和Benzinga RSS的美国财政部、美联储、白宫新闻，经 DeepSeek 判断来源与独家状态，保留原文并发送到飞书自定义机器人。

## 当前交付状态

这是重新建立的独立 Python 项目，不依赖原 vibecoding 仓库。已实现可配置 JSON/RSS 采集、SQLite 原文与历史存储、DeepSeek skill 加载及输出校验、六类主体展示、重新判断、飞书签名和持久发送队列。

**已填写三路 RSS 默认配置，尚未完成全链路运行。** 财联社使用 RSSHub `/cls/telegraph`，金十使用 `/jin10`，美国源使用 Benzinga `https://www.benzinga.com/feed`。本次检查 Benzinga 返回 HTTP 200 和有效 RSS（10 条，样本偏加密货币文章），不保证覆盖完整宏观快讯；RSSHub 公共实例的两路请求均在 20 秒后连接超时。未调用付费模型或发送飞书消息。

## 安装与启动

需要 Python 3.11+。在仓库目录打开终端：

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e .
Copy-Item .env.example .env
Copy-Item config.example.json config.local.json
```

编辑 `.env`，填写 DeepSeek key、模型名称、飞书机器人 webhook/签名密钥，；三个新闻源 URL 已预填，可按需替换。配置文件可为各源设置 JSON 字段映射或切换 RSS。密钥与本地配置已加入 `.gitignore`，不要提交。

```powershell
# 仅查看配置是否齐全
.venv\Scripts\python -m news_pipeline.cli doctor
# 采集并调用 DeepSeek，结果存储在本地，暂不发送飞书
.venv\Scripts\python -m news_pipeline.cli once
# 查看已生成的待发送原文与判断
.venv\Scripts\python -m news_pipeline.cli outbox
# 发送待发送队列
.venv\Scripts\python -m news_pipeline.cli flush --send
# 持续轮询并向飞书发送，Ctrl+C 停止
.venv\Scripts\python -m news_pipeline.cli run --send
```

`once/run` 会使用配置的 DeepSeek API，产生相应服务用量。程序默认每轮完成后等待 60 秒；实际延迟包括供应商延迟、AI 判断耗时及队列等待，不是毫秒级推送。每轮最多判断 20 条，完整窗口默认 24 小时，每条默认 10 分钟重新判断；可调整配置。不要同时运行多个实例共用同一数据库。

## 新闻源接入

默认地址同时写入 `.env.example` 与 `config.example.json`；非空环境变量优先于 JSON 中的 `url`。已有 `.env` 或 `config.local.json` 不会随仓库模板更新自动改变，请同步三个 URL，并将三路 `kind` 改为 `rss`；避免覆盖自己的密钥。RSS 快照会继续标记覆盖不完整，不会因填入地址而被认定为完整 24 小时证据。

三家源在配置中分别使用 `cls`、`jin10`、`us_24h`。`us_24h` 当前显示名为 Benzinga；不同来源必须有独立身份，不能将同一家转载聚合服务当作多个独立源。

默认使用 RSS。JSON 输入可另行配置，其统一接口协议并非财联社或金十官方端点。将你拥有的实际数据接口返回字段映射到本项目协议；无需虚构 URL。详见 [数据源契约](docs/source-contract.md)。也可设置 `kind: rss`，读取真实 RSS/Atom URL；RSS 快照无法证明历史完整性，程序会保守标记覆盖不完整。

JSON 采集使用一次 GET；不会猜测供应商的分页参数。需要历史分页时，应由供数接口/适配服务返回完整观察窗口及明确 `coverage`，否则按部分覆盖处理。鉴权头从 `headers_env` 指向的环境变量读取，值为 JSON 对象。

## 判断与飞书展示

- 六类：财政部长 / 财政部其他人员或机构、美联储主席 / 美联储其他人员或机构、总统 / 白宫其他人员或机构。
- 四种事实状态：非独家、有证据支持独家、暂见单一来源、证据不足。
- 同一篇消息包含不同事实时逐项判断；所有状态都列出已知传播渠道及原始来源。多家转引一家，不等于多个独立原始来源。
- 原文保持不变。飞书长消息分段，每段带记录、版本和分段编号。
- 覆盖缺失、候选数量/输入大小超限时，程序强制撤销独家和单源确定性；有正面证据的非独家判断仍可保留。
- skill 两份文件自动拼接进入 DeepSeek 系统提示，JSON Schema、证据 ID、短引文和输入链接经程序校验。语义判断仍依赖模型质量，不能保证绝对独家。

[详细 DeepSeek skill](skills/deepseek-news-exclusivity/SKILL.md) · [输出契约和边界案例](skills/deepseek-news-exclusivity/references/output-contract.md)

SQLite `articles` 保存原文与最新判断，`history` 保留判定历史，`outbox` 保存发送任务。重启不丢待发送内容。变化后的来源和状态触发新通知；同一采集记录以稳定 ID 追踪，各渠道记录仍分别保存，用 `matches` 相互关联。当前版本没有将跨渠道不同文章合并成单一全局事件，避免多事实文章误合并。

飞书只有返回成功才标记已发送。网络超时发生在服务端接收之后时，重试可能重复，消息中的记录和版本可辅助识别；自定义机器人没有这里可用的端到端幂等保证。失败任务保留并按顺序重试，避免长消息后半段先到。

## 已知接入边界

- RSS 地址已填写，配置完成不等于三家已经接通。RSSHub 公共实例本次连接超时，可替换为自建实例。Benzinga RSS 仅验证 XML 可解析，实时性、宏观新闻覆盖和全文完整性仍需观察。
- 未自动联网核验官员名册或官方新闻稿。可用 `officeholders_file` 传入带任期与来源的名册；只出现名字且缺乏身份依据时，skill 要求保留未知。
- 当前以飞书自定义机器人群消息实现“上传”。若你需要飞书多维表格，应再补表格 ID、字段和应用授权进行接入。
- 输入候选来自数据库内观察窗口，没有外部全网检索；若数量超限，明确报告截断，不会冒充完整比较。
- 不存在已部署的后台服务、定时云任务或已启用 GitHub Actions。

## 接口参考

[DeepSeek JSON 模式](https://api-docs.deepseek.com/guides/json_mode/)用于 JSON 输出配置；[飞书自定义机器人说明](https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot)用于机器人配置。来源网站：[财联社电报](https://www.cls.cn/telegraph)、[金十数据](https://www.jin10.com/)。网站地址不是已经验证的数据 API。
