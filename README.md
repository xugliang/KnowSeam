# KnowSeam · 企业知识库

独立部署、通过 MCP 接入智能体的企业知识服务。核心不依赖 SuiteHarness、Codex、Claude Code 或 DeepSeek Harness。智能体负责理解问题和组织答案，KnowSeam 负责资料同步、检索、授权和可追溯证据。

源码仓库：[xugliang/KnowSeam](https://github.com/xugliang/KnowSeam)。仓库名称为 **KnowSeam**；Python 包、命令和插件标识仍使用小写 `knowseam`。

## 能力与边界

- 数据源：飞书 Wiki、显式授权的文件目录。ABC（抽象基类）连接器、解析器、向量模型、重排序模型接口支持扩展。
- 解析：TXT/Markdown、DOCX、XLSX、PPTX、文本 PDF、已支持的飞书原生文档/表格。保留段落、单元格、幻灯片定位。
- 存储：PostgreSQL + pgvector；原始材料内容寻址存档。不包含业务考核、客户数据、聊天记忆。
- 检索：向量 + Jieba 分词后的 BM25 → RRF 排名融合 → 重排序。BM25 使用 PostgreSQL 倒排词频表和标准公式，不依赖额外数据库扩展。
- 增量：来源版本、正文 SHA-256、解析流水线指纹、分块哈希、向量缓存。失败不先删旧版本；完整扫描才判定删除；权限收回或过期后拒绝读取。
- 接入：MCP Streamable HTTP、可选 stdio 远程桥接、可选 SuiteHarness 原生适配器。员工电脑不运行数据库或索引任务。
- 权限：集合支持全员／部门／指定员工三层授权，召回前过滤、读原文及重排序后复核。来源仍须经过发布范围核验；不自动同步飞书私有文档 ACL。详见 [权限与 BM25 运维](docs/BM25-ACCESS.md)。

不支持扫描件/图片 OCR、图片语义、音视频、旧二进制 Office、任意原生多维表格/Slides；不执行 Office 宏。资料不足时返回无依据，不编造全文。

## 目录

| 模块 | 职责 |
| --- | --- |
| `src/knowseam/connectors/` | 来源枚举、下载、权限和版本读取 |
| `domain.py`、`parsers.py`、`chunking.py` | 扩展契约、解析、分块与指纹 |
| `storage.py`、`sync.py` | 数据库、索引发布、增量同步与撤回 |
| `models.py`、`tokenization.py`、`retrieval.py` | 向量、分词、融合、重排序 |
| `service.py`、`api.py` | 应用层与可信后端 REST 接口 |
| `mcp_server.py`、`bridge.py` | MCP 网关、远程桥接；个人凭据绑定身份 |
| `harness_plugin.py` | 可选 SuiteHarness 适配；核心不导入 |
| `integrations/`、`deployment/`、`tests/` | 接入包、部署、回归测试 |

## 源码运行

Python 3.11+，独立 PostgreSQL/pgvector。业务配置只来自 JSON 文件。

```bash
git clone https://github.com/xugliang/KnowSeam.git
cd KnowSeam
python -m venv .venv
.venv/bin/python -m pip install -e '.[mcp,test]'
cp config.example.json config.local.json
# 配置独立数据库、模型、来源和可信后端客户端
.venv/bin/python -m knowseam.cli --config config.local.json init-db
.venv/bin/python -m knowseam.cli --config config.local.json sync
.venv/bin/python -m knowseam.cli --config config.local.json serve --port 18790
# 另起进程运行持续同步
.venv/bin/python -m knowseam.cli --config config.local.json worker
# 另起 MCP 网关
.venv/bin/python -m knowseam.mcp_server --config /private/mcp-server.json
```

模型示例：Qwen3-Embedding-0.6B（1024 维）和 Qwen3-Reranker-0.6B，独立部署，HTTP 接口为 `/v1/embeddings`、`/v1/rerank`。非回环模型端点（含内网）需要 `allow_online=true`。本地 Qwen 后端要求预先准备权重，可选依赖为 `.[models]`。

MCP 示例见 `examples/` 和 [接入说明](integrations/README.md)。必须更换示例凭据、域名和身份绑定。只支持静态个人令牌；不包含 OAuth 登录服务器。跨电脑优先 HTTPS 或加密隧道，HTTP 需明确可信内网放行。

## 验证

```bash
python -m pytest -q
python -m ruff check --config pyproject.toml src tests
```

测试替身不等于模型质量验收。数据库/SuiteHarness/Torch 可选测试缺依赖时明确跳过。见 [测试与迁移](docs/VALIDATION.md)。

## 发布边界

本仓采用 [MIT](LICENSE)，允许商业使用和修改，须保留许可声明。仓库不包含公司文档、索引、凭据、权重或 A/B 业务代码。依赖库及模型权重遵循各自许可证，MIT 不重新授权第三方内容。
