# RSSHub 连接失败时使用直连采集

2026-09-11 检查：RSSHub 公共实例返回 HTTP 403，正文提示限制部分阅读器访问。新增 cls/jin10 两种采集方式，参考 RSSHub 上游路由调用方式，不需要额外新闻源账号。它们是网站接口，并非有稳定性承诺的官方开放 API。

```powershell
python -m news_pipeline.cli once --config config.direct.example.json
```

DeepSeek 密钥仍从同目录的 `.env` 读取。不带 `--send` 不会发送飞书。直连配置用 `CLS_DIRECT_FEED_URL`、`JIN10_DIRECT_FEED_URL` 覆盖默认值，避免已有 RSS 的环境变量覆盖直连地址。Benzinga 仍使用 `US_FEED_URL`。

实际单轮结果：财联社 20 条、金十 17 条、Benzinga 10 条。限制为 3 条的模型试跑全部完成校验并保存，三个国内新闻样本均为 out_of_scope，未生成通知。不能据此确认所有相关性判断都正确。

金十锁定消息和无文本条目会跳过并记录覆盖缺口；时间按中国标准时间转换。三路均为最近消息快照，不冒充完整历史窗口。Benzinga `/feed` 当前样本为加密货币预测文章；`/news/feed` 返回空订阅。美国宏观新闻覆盖仍待解决。

接口实现参考：https://github.com/DIYgod/RSSHub/tree/master/lib/routes/cls 和 https://github.com/DIYgod/RSSHub/blob/master/lib/routes/jin10/index.ts 。新增适配未复制或存储账号密钥；RSSHub 是 AGPL-3.0 开源项目。
