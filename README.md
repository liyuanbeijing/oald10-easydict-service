# OALD10 EasyDict Offline Service

将本地合法取得的《牛津高阶英汉双解词典》第 10 版 MDX/MDD 转换为
EasyDict 兼容的 SQLite 数据，并通过本机只读 HTTP 服务查询。本项目仅供个人、
非商业学习；不得公开分发 Oxford 源文件、生成数据库或音频。

## 功能

- 保留英美发音、英中释义与例句、标签、义项组、习语、短语动词、派生词和交叉引用。
- 每个实际词性生成稳定 entry，ID 为
  `(一基 MDX 记录序号 << 10) | 局部词性序号`。
- 保持例句所属义项和原始顺序，去除 HTML 高亮与例句音频引用。
- 解析跳转与别名，并为嵌入词条建立索引。
- 只提取被引用的单词音频；运行时不访问 MDX/MDD。

输出格式兼容
[easydict-cloud f1fd43c](https://github.com/AstraLeap/easydict-cloud/commit/f1fd43c8044e4decb2e19c84383a395168c3136d)
和
[EasyDict builder b72c475](https://github.com/AstraLeap/easydict/commit/b72c475d8a6db2b5e50b6424f0db5aa49aa26904)。

## 构建

需要 Python 3.12 和 `uv`。在仓库根目录准备以下文件（`sources/` 已被 Git
忽略）：

```text
sources/oald10-bilingual/oald10-bilingual.mdx
sources/oald10-bilingual/oald10-bilingual.1.mdd
sources/oald10-bilingual/oald10-bilingual.png
```

```bash
uv sync --all-groups
uv run oald10 sample answer record information "answer back"
uv run oald10 build
```

如需使用其他位置，可传入 `--mdx`、`--mdd`、`--logo` 和 `--output`。
构建在临时目录完成，审计通过后才原子替换默认的 `generated/`。

当前源文件的通过审计：

| 项目 | 数量 |
|---|---:|
| MDX records / redirects | 109,014 / 57,704 |
| Entries / indices | 55,762 / 146,416 |
| Paired EN/ZH definitions | 93,696 |
| Raw / retained example pairs | 110,243 / 101,044 |
| Retained example lists / excluded duplicates | 58,898 / 9,199 |
| Word audio files / decoded MDD blocks | 96,395 / 7,271 |
| 单边释义、缺失语言、孤立或重复例句 | 0 |
| 解析失败、ID 冲突、无解跳转、缺失或重复音频 | 0 |

生成结果：

```text
generated/
├── entries.jsonl
├── audit.json
└── easydict-data/dictionaries/oald10/
    ├── dictionary.db
    ├── media.db
    ├── metadata.json
    └── logo.png
```

## 运行服务

需要 Docker 和 Docker Compose。首次运行先创建本地配置：

```bash
cp .env.example .env
docker compose config --quiet
docker compose up -d --build
docker compose ps
```

容器内服务端口固定为 `33070`；Compose 宿主机端口由 `.env` 中的 `OALD10_PORT`
设置（默认 `33070`，本机访问如 `http://127.0.0.1:33070`）；生成数据路径由
`OALD10_DATA_PATH` 设置。容器以非 root 用户运行，根文件系统只读，且只读挂载生成数据。

```bash
curl "http://127.0.0.1:${OALD10_PORT:-33070}/health"
```

接口与响应格式见 [API.md](API.md)。

## 开发验证

```bash
uv run ruff format --check .
uv run ruff check .
uv run ty check src tests
uv run pytest
docker compose config --quiet
```

## 数据与许可

本仓库不授予 Oxford 内容的任何权利。不要提交、上传或再分发源词典、
`generated/`、SQLite 数据库或提取的音频。
