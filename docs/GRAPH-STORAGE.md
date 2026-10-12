# 通用知识库的可选图谱存储

KnowSeam 提供两种正式部署方式，不要求产品 A，也不依赖 SuiteHarness 内部实现。

| 模式 | 数据库 | 检索 |
|---|---|---|
| 标准模式 | PostgreSQL＋pgvector | 向量＋BM25 → RRF → 重排序 |
| 图谱增强模式 | 上述数据库＋Neo4j | 增加有证据、可授权的图谱候选，再融合重排序 |

PostgreSQL 是**权威数据源**：文档、不可变版本、分块、向量、BM25、员工及集合
权限仍在这里。Neo4j 是可重建的**投影**，即从权威数据整理出的实体关系索引；
不能替代 PostgreSQL，也不能由智能体直接执行任意 Cypher（图查询语言）。

## 模块边界

- `repository.py`：KnowledgeRepository（权威存储基类）、RetrievalBackend
  （检索基类）。默认集合范围校验和无连接资源的 close 放在基类。
- `graph/domain.py`：GraphStore、GraphExtractor 抽象基类，以及证据、实体、
  关系、文档投影的数据契约。
- `graph/config.py`：显式启用、集合本体、模型配置、秘密无关的投影指纹。
  **本体**表示允许哪些实体类型及关系类型，例如“产品—支持—功能”。
- `graph/extraction.py`：OpenAI 兼容聊天接口的结构化抽取。逐块提取，验证引用
  必须是块内非空原文、类型必须在本体中、关系端点必须存在。
- `graph/neo4j_store.py`：固定标签、参数化查询、文档锁与事件序号防止旧任务
  覆盖新版本。按企业和集合隔离实体；提及、关系各自绑定来源。
- `graph/postgres.py`：Outbox（事务内写入的待处理事件表）、租约、检查点、
  重试、当前证据授权。图谱是可选能力，不扩张基础存储 ABC 的必实现接口。
- `graph/worker.py`：独立图谱工作进程；不阻塞原来的文档同步进程。
- `graph/search.py`：有界一至两跳搜索。每一条边得到授权后，才能进入下一跳。

企业产品、部门名称、人员及别名均由私有配置或资料决定，不写入通用代码。
当前继承原来的部署契约：一份配置对应一个企业；所有图键仍包含 company_id。
这不意味着已经实现多企业 SaaS 管理后台。

## 配置

旧配置不需要添加字段，默认 `graph.enabled=false`。关闭时不导入 Neo4j 驱动、
不连接图数据库，也不构造图谱抽取模型。标准镜像只安装原有依赖。

根目录 `graph.example.json` 是**配置片段**，不是完整的服务配置。把其内容合并
到私有 `core.json` 的 `graph` 字段；集合 ID 必须已经在 collections 里存在。
设置实际密码、模型地址和模型名后，再把 enabled 改为 true。

- uri/database/username/password：Neo4j 连接信息；密码只放私有配置。
- collections：按集合配置 entity_types、relation_types、aliases、version。
  aliases 是人工批准的“别名→规范名”。不同集合不自动合并实体；同名不同
  对象可以通过 qualifier（限定信息）区分。
- extractor：单独的聊天模型，不复用向量模型或重排序模型。base_url 例如
  `http://chat-model:8000/v1`。非本机回环地址必须显式 allow_online=true，
  包括容器网络地址；这表示明确许可该地址，不等于默认把资料发送到公网。
- model_revision：建议固定模型版本。更换模型或本体后，配置初始化和图谱工作
  进程启动会自动为当前文档补建新指纹任务；可用 graph-rebuild 显式全量重建。
  hops 等查询参数不改变抽取指纹，不会触发重新抽取。
- hops：1～2，默认 1；candidate_limit：1～100，默认 40；被拒绝的候选也消耗
  遍历额度，防止借助不可读关系无限探查。
- include_inferred：默认 false；min_confidence：0～1，默认 0.7。模型置信度
  不是事实正确概率，开启推断也不能把它当原文确定事实。
- lease_seconds：60～3600，默认 600；至少为 `4×extractor.timeout＋30`，以
  覆盖超时和重试。任务最长失败尝试次数 max_attempts 为 1～3，默认 3。

每次抽取响应采用 JSON 对象，并由本地严格校验，不要求服务商支持严格
JSON Schema 输出。单次 HTTP 操作最多尝试 3 次；任务最多失败尝试 3 次，
二者是不同层级。已经提交的块检查点会复用，不因图数据库写失败重复调用模型。
每次发送下一块之前检查资料是否仍授权、版本是否仍有效；撤销后停止发送剩余块。
已发送的正在进行中的网络请求无法撤回。不要配置未经企业批准的外部服务。

## 部署

仍然是一个仓库和一个 Compose 项目。需要先准备私有配置及模型服务。
`secrets/` 不应上传 GitHub。api/worker 的 PostgreSQL DSN 使用 `postgres:5432`
（自带数据库）或既有外部 PostgreSQL。

标准模式可以使用已有 PostgreSQL；若需要 KnowSeam 管理数据库容器，增加
`compose.storage.yaml`。准备 `secrets/postgres-password.txt`，其内容应与
core.json DSN 密码一致。数据库端口不映射到主机。

```bash
docker compose -f deployment/compose.yaml -f deployment/compose.storage.yaml build api
docker compose -f deployment/compose.yaml -f deployment/compose.storage.yaml up -d postgres
docker compose -f deployment/compose.yaml -f deployment/compose.storage.yaml run --rm api --config /private/core.json init-db
docker compose -f deployment/compose.yaml -f deployment/compose.storage.yaml up -d api worker mcp
```

图谱增强模式另外准备 `secrets/neo4j-auth.txt`，单行内容为 `neo4j/实际密码`
且以换行结尾，与 core.json 的 graph.password 一致。Neo4j 的初始化密码只对
新数据卷生效，已有卷不能通过修改此文件重设密码。

```bash
docker compose -f deployment/compose.yaml -f deployment/compose.storage.yaml -f deployment/compose.graph.yaml --profile graph build api
docker compose -f deployment/compose.yaml -f deployment/compose.storage.yaml -f deployment/compose.graph.yaml --profile graph up -d postgres neo4j
docker compose -f deployment/compose.yaml -f deployment/compose.storage.yaml -f deployment/compose.graph.yaml --profile graph run --rm api --config /private/core.json init-db
docker compose -f deployment/compose.yaml -f deployment/compose.storage.yaml -f deployment/compose.graph.yaml --profile graph run --rm api --config /private/core.json init-graph
docker compose -f deployment/compose.yaml -f deployment/compose.storage.yaml -f deployment/compose.graph.yaml --profile graph run --rm api --config /private/core.json graph-rebuild
docker compose -f deployment/compose.yaml -f deployment/compose.storage.yaml -f deployment/compose.graph.yaml --profile graph up -d api worker mcp graph-worker
```

首次部署时等待 PostgreSQL/Neo4j 健康检查成功，再执行初始化。使用外部
PostgreSQL 时删去 `-f deployment/compose.storage.yaml` 并使用现有 DSN。
不要将这些命令直接套在现有生产目录而不核对 Compose 项目、数据卷和配置。

镜像固定 Neo4j 5.26.32 Community、Python 驱动 6.4.0；PostgreSQL 示例固定
pgvector 0.8.7/PG17，实际验收环境可保持已有兼容版本，不自动升级生产数据库。
Neo4j Community 是 GPLv3 的独立运行依赖，不把它的源码复制为 KnowSeam MIT
代码；Community 不提供企业版细粒度权限/高可用承诺。图数据库不开放主机端口，
它的管理员凭据和原始资料不向普通员工暴露。

## 增量、权限与恢复

发布新文档版本和插入 sk_graph_jobs 在同一个 PostgreSQL 事务中：要么都提交，
要么都回滚。图谱异步更新期间，用户可以继续使用向量＋BM25。

任务状态：pending 等待、working 租约内处理、retry 稍后重试、done 完成、
obsolete 被新版或撤销取代、failed 达到失败尝试上限。claim_token 防止旧工作
进程续约/提交他人的任务；event_sequence 防止旧图谱写覆盖新投影。
检查点保存每块结构化抽取，不保存智能体会话。

检索先授权原始候选，再取图谱种子；每跳从 PostgreSQL 重新取真实分块，并检查
企业、集合、员工状态、源启用、源 ACL 时效、文档活动版本和分块引用。不可读
边不进入下一跳。保存完整路径依赖，在重排序后批量核验种子、中间边和最终
候选的证据；路径失效时移除该路径的候选，独立向量/BM25 命中的资料不受影响。
同集合多个种子采用保守依赖，任一种子失效可能降低召回，但不会扩大权限。
撤销或删除立即影响读取，不等异步
图清理。搜索返回原有文档引用，不生成混合员工权限的全局图摘要。

图数据库故障时显式返回 graph_status=unavailable，继续正常向量/BM25 检索。
原有检索或重排序失败不会被伪装成图谱成功。关闭模式保持原有响应格式；
增强模式增加 graph_status 字段，list/read/MCP 参数不变。

```bash
knowseam --config secrets/core.json graph-status
knowseam --config secrets/core.json graph-worker --once
knowseam --config secrets/core.json graph-rebuild
```

失败任务可通过 graph-rebuild 生成新任务；重建不重新下载或重新嵌入资料。
检查点和图事件都是企业数据，备份、保留时长和运维访问需要按企业政策管理。
更换模型、本体、别名、抽取版本需重建图谱，启动时会自动补建缺失的新指纹
任务，不会重新生成向量。失败任务不会通过重启无限重试，需要显式 graph-rebuild。
投影指纹变化期间旧投影不参与查询。被撤销后恢复且正文未变化的文档，在 ready
状态恢复的同一事务重新入队，避免遗漏投影。

备份以 PostgreSQL、私有配置和原文件存档为主。可清空**专用 Neo4j 数据卷**后
重建；不得误删共享图数据库。若 PostgreSQL 回滚到旧备份，需要同步恢复
Neo4j 或清空专用投影后重建，否则旧的高事件序号可能阻挡恢复后的较小序号。
这类清理是运维操作，必须先备份并确认目标，本代码不会自动清空数据库。

## 验证边界

自动化测试使用合成资料、真实 PostgreSQL/Neo4j，外部模型由确定性替身替代；
覆盖版本、删除、每跳 ACL、租约、检查点与幂等。它证明链路与授权契约，不证明
真实模型的抽取准确率、图谱质量或生产吞吐量。最终测试命令和结果另见本次验收
记录；不应把跳过的外部运行测试写成已验证。
