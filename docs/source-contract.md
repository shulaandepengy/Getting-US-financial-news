# 数据源契约

`CLS_FEED_URL`、`JIN10_FEED_URL`、`US_FEED_URL` 分别指向实际提供对应渠道内容的 HTTPS 接口。默认返回结构如下，示例新闻是虚构的协议示例：

```json
{
  "items": [
    {
      "id": "provider-stable-article-id",
      "title": "示例标题",
      "content": "完整新闻原文。不得用摘要冒充全文。",
      "url": "https://example.com/article/1",
      "published_at": "2026-09-05T20:00:00+08:00",
      "explicit_attributions": []
    }
  ],
  "coverage": {
    "complete": false,
    "window_start": "2026-09-04T20:00:00+08:00",
    "window_end": "2026-09-05T20:00:00+08:00"
  }
}
```

`complete=true` 只适用于供数端确实完成所有分页、没有漏采、返回全文且覆盖整个请求观察窗口的情况。程序比较该区间与当前所需区间，不因 HTTP 200 或空数组认定完整。窗口不足、正文缺失、日期未知或 RSS 快照都会降级覆盖。

若接口支持按时间查询，URL 可包含 `{since}` 与 `{until}`，程序将替换为经过 URL 编码的 UTC ISO 8601 观察窗口边界，例如 `https://your-feed.example/news?start={since}&end={until}`。查询参数名称由你的供数接口决定。

程序保存输入正文，不自行扩写 RSS 摘要。若 feed 只提供摘要，应在供数适配层取回全文，否则不得宣称全文或完整覆盖。

JSON 路径用点分隔，仅支持对象字段。示例配置：

```json
{
  "id": "cls",
  "name": "财联社",
  "kind": "json",
  "url_env": "CLS_FEED_URL",
  "items_path": "data.articles",
  "headers_env": "CLS_HEADERS",
  "fields": {
    "id": "article_id",
    "title": "headline",
    "content": "body.text",
    "url": "permalink",
    "published_at": "timestamp"
  }
}
```

这里的字段名称也是映射演示，需要以真实供应商接口为准。时间接受带时区 ISO 8601、带时区 RFC 822 或 Unix 秒数；Unix 毫秒需先由适配端转换，未带时区时间不会被擅自猜测成北京时间。

`headers_env` 对应环境变量，例如 `.env` 中 `CLS_HEADERS='{"Authorization":"Bearer YOUR_TOKEN"}'`。不要把 token 放入提交的配置或对话中。

RSS 示例为 `{"id":"us_24h","name":"实际供应商","kind":"rss","url_env":"US_FEED_URL"}`。RSS 自动读取 item/entry 的 guid/id、title、link、pubDate/published/updated 及 encoded/content/description/summary，仍保留快照覆盖限制。

上游 ID 或链接稳定时，更正正文会更新同一记录并触发重评。两者都缺失时使用发布时间与正文生成 ID，正文更正会成为新记录；应尽量提供稳定 ID。

