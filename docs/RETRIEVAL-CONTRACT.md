# 检索、证据和扩展工具契约

本文件对应当前代码；调用者负责理解问题及综合回答，KnowSeam 返回有来源的资料，
不返回未经证实的公司产品全集或业务推论。所有工具只读。

## 身份与兼容

Principal（可信调用身份）由 REST 凭据、MCP 令牌绑定或 SuiteHarness RequestScope 提供。
工具没有 actor_id/company_id/员工权限参数。collection_ids 只能缩小既有授权范围。
MCP gateway、stdio bridge 和 SuiteHarness adapter 的 extended_tools 默认 false：
保留原 knowledge_list/search/read；启用才注册新增工具，不改 SuiteHarness 核心。

## 接口

| MCP 名称 | HTTP | 输入、边界与返回含义 |
| --- | --- | --- |
| knowledge_list | GET /v1/collections | 只返回当前可用集合 |
| knowledge_search | POST /v1/search | query 1–4096 字；limit 默认 8，1–20，限制返回 chunk 数而非文档数 |
| knowledge_read | GET /v1/documents/{id} | document/version，offset 默认 0、最大 1000000，limit 默认 50、最大 100；只读当前版本 |
| knowledge_capabilities | GET /v1/capabilities | 当前身份的可选能力；视觉还检查 live runtime 指纹和就绪状态 |
| knowledge_catalog | POST /v1/catalog | collection_ids 必填且 1–32 个唯一 ID，kind 必填；limit 50、最大 100；cursor 可空 |
| knowledge_context | POST /v1/context | document/version、1–20 个 chunk_ids；neighbors/section；before/after 默认 1、0–5；max_chars 默认 12000、1000–30000 |
| knowledge_table_list | GET /v1/documents/{id}/tables | version_id、offset、limit 默认 50、最大 100；返回 table_id 及列结构 |
| knowledge_table_read | POST /v1/documents/{id}/tables/{table}/read | version_id、row_start 默认 1、row_limit 默认 50、最大 100；column_ids 可空或 1–32 个唯一列；最多 4096 单元格 |
| knowledge_history_search | POST /v1/history/search | query、valid_as_of 必填带时区，known_as_of 可空；include_unknown_validity 默认 false；limit 默认 8、最大 20 |
| knowledge_history_read | POST /v1/history/read | document/version、known_as_of 可空；分页与旧 read 相同；当前 ACL 生效 |
| knowledge_visual_search | POST /v1/visual/search | query、collection_ids、limit 默认 8、最大 20；返回视觉资源而非文本 SearchHit |
| MCP 资源 | GET /v1/assets/{asset_id}?version_id=… | knowseam://assets/{asset_id}/{version_id}；每次读取重新鉴权，PNG 最大 10 MiB，不缓存授权结果 |

不同传输入参形态以实际 Schema 为准：REST 路径 ID 不得在 body 重复冒充；
MCP 中 document_id/version_id 是工具参数。JSON 未知字段、重复 key、NaN、bool 冒充整数均拒绝。
新版 JSON 响应有 512 KiB 上限，图片走独立受控资源，不把图片 base64 塞入搜索结果。
桥接端必须 extended_tools=true 才允许扩展工具和图片资源；资源身份范围不能比上游令牌更宽。

## 搜索策略

- legacy：保留原双通道/配置图检索行为。
- coverage：授权候选内 RRF、重排后每文档配额和重叠去重。候选池有界，不保证找齐全库。
- relationship：使用当前授权事实、本体/别名、有限跳数；include_paths 可返回逐条引文支持路径。
- filters 仅 kind/topic/document_type，必须管理员在 coverage.allowed_filters 登记；
  元数据来自已授权目录，不接受模型生成 SQL 或任意 where。
- include_paths 只能与 relationship 一起使用。
- include_diagnostics 是请求，不是授权；身份必须列入 diagnostic_actor_ids。

排序分数不是事实正确率。目录 complete 只指声明来源范围扫描完整；
语义 coverage.complete=null 表示无法判定全部。版本改变或撤权会使目录游标失效。
无能力、无授权、没有结果、降级/截断是不同状态，不得统一描述为“数据库中没有”。

## 证据与分页

- 返回 document_id/version_id/chunk_id/source_url/locator，证据绑定版本，不能跨版拼接。
- 原文跨度按 Python Unicode 字符计数，左闭右开，不按 UTF-8 字节或 tokenizer token。
- next_offset/next_row/cursor 仍有值时可继续读取；不代表一次把全文送入模型。
- 父节读取返回原始 Section，不返回为向量模型追加的标题前缀。
- 表格保存 raw_text、value/value_kind、行列、合并锚点、公式与缓存状态；不运行 Excel 公式。
  精确数值以十进制字符串保存，保留前导零；未知类型不能猜为数字或日期。
- 视觉粗召回再 MaxSim 的结果 complete=false；页图命中不是数值读取正确，
  可用结构化表格时优先读单元格。
- 历史支持缺失、旧版本未抽取或时间不可核实必须明确标注，详见升级文档。

## 诊断与评测

阶段包括 auth/embed/tokenize/retrieve/dense/lexical/graph/fusion/rerank/coverage/revalidate，
有界记录请求 ID、耗时、状态、返回数量和候选预算。Observer 默认 NoOp，
故障/阻塞不阻断查询；日志不记录查询、正文、令牌、身份或 URL。

证据评测同时区分文档命中、必要证据组覆盖、完整支持集合、表格单元格命中与无答案误召回。
不同组是 AND，组内替代证据是 OR；同一文档错段不算完整证据。
旧评测标签没有跨度时，该指标是 null，而不是自动算正确。
合成测试检验软件契约；真实模型效果、企业标注、规模性能均须独立验收。
