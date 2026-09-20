# OALD10 HTTP API Reference

Base URL 格式为 `http://<host>:<port>`。容器内部端口固定为 `33070`，Compose 宿主机映射端口由 `.env` 中的 `OALD10_PORT` 配置（默认端口为 `33070`，本机访问默认为 `http://127.0.0.1:33070`）。
词典 ID 固定为 `oald10`。服务支持 CORS 跨域访问（适配 Anki 等前端客户端集成），提供基于 Swagger UI 的在线交互文档（`/docs` 与 `/openapi.json`），不提供认证、前缀搜索
或模糊搜索。

> 下文调用示例使用 `${OALD10_PORT:-33070}` 表示宿主机访问端口（未设置环境变量时回退到默认端口 `33070`）。请根据实际部署的主机与端口替换。

## 路由

| 方法 | 路径 | 用途 |
|---|---|---|
| `GET` | `/health` | 检查数据库是否可读 |
| `GET` | `/dictionaries` | 获取词典信息与实时数量 |
| `GET` | `/word/oald10/{word}` | 按完整词头查询 |
| `GET` | `/entry/oald10/{entry_id}` | 按稳定 ID 获取词条 |
| `GET` | `/audio/oald10/{filename}` | 获取 MP3 发音 |
| `GET` | `/docs` | Swagger UI 在线交互文档 |
| `GET` | `/openapi.json` | OpenAPI 规范描述文件 |

## 健康检查

```bash
curl "http://127.0.0.1:${OALD10_PORT:-33070}/health"
```

```json
{"status":"ok","dictionary":"oald10"}
```

数据库不可用时返回 `503`，`status` 为 `unavailable`。

## 词典信息

```bash
curl "http://127.0.0.1:${OALD10_PORT:-33070}/dictionaries"
```

响应为 `{"dictionaries":[...]}`。每个词典对象包含：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id`、`name`、`version` | string/string/integer | 词典标识、名称和数据版本 |
| `entry_count`、`audio_count`、`image_count` | integer | SQLite 中的实时数量 |
| `dict_size`、`media_size` | integer | 数据库大小（字节） |

## 按词头查询

```bash
curl "http://127.0.0.1:${OALD10_PORT:-33070}/word/oald10/answer"
curl "http://127.0.0.1:${OALD10_PORT:-33070}/word/oald10/answer%20back"
```

查询使用 NFKC Unicode 规范化、不区分大小写，并折叠首尾及连续空白；仍须匹配
完整词头。响应最多包含 100 个 entry：

```json
{
  "dict_id": "oald10",
  "word": "not-found",
  "entries": [],
  "total": 0
}
```

未命中仍返回 `200` 和空 `entries`。

## 按 ID 获取词条

```bash
curl "http://127.0.0.1:${OALD10_PORT:-33070}/entry/oald10/3239937"
```

entry 的顶层字段：

| 字段 | 说明 |
|---|---|
| `dict_id`、`page` | 固定为 `oald10` |
| `entry_id` | 正整数稳定 ID |
| `headword`、`pos`、`section` | 词头、词性与源分区 |
| `entry_type` | `word` 或 `phrase` |
| `pronunciation` | `{region, notation, audio_url}` 列表 |
| `sense` | 按源顺序保存的义项节点 |
| `child_idioms` | 嵌入习语 |
| `child_phrasal_verbs` | 嵌入短语动词 |
| `child_derivatives` | 嵌入派生词 |
| `cross_references` | 顶层交叉引用 |

`sense[].kind` 为：

- `sense`：可含 `index`、`labels`、`definition: {en, zh}`、
  `examples: [{en, zh}]` 和 `cross_references`。
- `group`：含双语 `title` 与嵌套的 `senses`。

只有例句或引用的义项会省略 `definition`。例句保持所属义项和源顺序，只含
纯文本，不含 HTML、例句 ID 或 `sound://audio/example/` 地址。

非法 ID 返回 `400`；合法但不存在的 ID 返回 `404`。

## 获取发音

使用 `pronunciation[].audio_url` 返回的相对路径：

```bash
curl --output answer-uk.mp3 \
  "http://127.0.0.1:${OALD10_PORT:-33070}/audio/oald10/answer__gb_1.mp3"
```

成功时返回 `200`、`Content-Type: audio/mpeg` 和
`Cache-Control: public, max-age=2592000, immutable`。文件名必须是安全的
`.mp3` 基本文件名；不存在时返回 `404`。

## 在线文档

服务启动后，可在浏览器中直接访问 Swagger UI 交互式在线文档：

```
http://127.0.0.1:${OALD10_PORT:-33070}/docs
```

OpenAPI 规范 JSON 数据可通过以下地址获取：

```bash
curl "http://127.0.0.1:${OALD10_PORT:-33070}/openapi.json"
```

## 错误

除健康检查外，错误通常为 `{"detail":"错误说明"}`。

| 状态码 | 含义 |
|---:|---|
| `400` | entry ID 非法，或音频文件名不安全 |
| `404` | 词典、entry、音频或路由不存在 |
| `503` | 生成的词典数据不可用 |
