# 三批增强：升级、配置和回退

本指南只描述 KnowSeam。产品的对话、考核、客户画像、业务权限仍由调用方负责。
旧的 knowledge_list/search/read 保留；新工具通过 extended_tools 显式开放。
**开发测试、模型质量验收和生产切换是三个不同步骤。不要直接对生产执行本文的迁移命令。**

## 1. 数据与可选组件

- PostgreSQL 是正文、版本、授权、证据、表格、历史事实及视觉资产的权威数据库。
- Neo4j 是可重建的图投影，不取代 PostgreSQL 授权。查询感知关系检索也可使用 PostgreSQL 事实。
- Docling 解析器、视觉模型分别独立运行，不升级原文本模型环境。
- 所有增强默认关闭；未安装视觉/Neo4j/Docling 不影响旧文本服务。
- Python 新 ABC 能力按组合注入，未配置返回 capability_unavailable；旧存储适配器不用实现新抽象方法。

## 2. 迁移前检查

1. 记录代码版本、当前配置、运行镜像和服务启动方式。保留未提交修改。
2. 对数据库做可恢复备份并演练恢复；确认附件归档与数据库版本对应。
3. 在独立数据库 knowseam_test 跑回归；不可将测试 DSN 指向业务库。
4. 将新代码部署到未接流量的实例，暂不启用增强、不启动新 worker。
5. 只检查计划：

~~~sh
python -m knowseam.cli --config /private/core.json migrate --dry-run
~~~

检查 001–006 连续迁移及校验和。缺失、跳号、校验和变化须停止，不要删除迁移记录绕过。
正式变更窗口内经确认再执行同命令的 migrate --apply。服务启动不自动执行迁移。
迁移是增量建表/索引，不会为旧资料虚构接受时间、历史事实或解析结构。

## 3. 功能开关及数据准备

文件配置中的 features 可分别启用 catalog/context/tables/coverage/history/query_graph/visual。
具体字段类型、默认值以 config.py 及各模块 Pydantic 模型为准；示例增强片段见
[features.v2.json](../examples/features.v2.json)，它不是完整可启动配置。

| 能力 | 准备 | 关闭时 |
| --- | --- | --- |
| 目录 | catalog.entries 显式登记来源、kind、item_id、name；不是把术语词典当目录 | 旧搜索仍可用，不能将 Top-K 宣称为全集 |
| 父节上下文 | 新版本保存原始 Section 和跨度，重新同步受影响来源 | 旧 read 仍按块分页 |
| 类型化表格 | parser 的 structured_tables 开启，版本随正文原子发布 | 旧文本结果仍可用 |
| 索引复用 | indexing.enabled=true；先 shadow 对照，再 reuse | reuse 回退前须物化旧词项 |
| 来源权限 | permissions.enabled=true；配置来源 permission_mode/可信身份绑定 | 不可用关闭权限功能的方式绕过私有资料校验 |
| 历史事实 | history=true；登记 graph.collections 本体及 extractor；运行 facts-worker | 保留原始数据，不删除历史 |
| 查询感知图 | query_graph=true；可用事实、本体；显式 strategy=relationship | legacy 检索语义不变 |
| 视觉 | features.visual 和 visual.enabled 同步启用，权限 worker、离线模型和独立 runtime 就绪 | 文本链路继续工作 |

目录、表格、父节需要新的解析/发布结果；开关打开不代表旧数据自动齐全。
来源配置、分词词典、模型、抽取提示或本体指纹变化会使相应派生产物失效。
不能把旧版本的 locator 或 ACL 直接复制为新版本证据。

## 4. worker 分工

- worker：扫描/归档/解析/正文索引，不替产品发送业务消息。
- permissions-worker：独立刷新来源权限，不初始化文本模型；过期或不完整观察默认拒绝。
- graph-worker / facts-worker：受租约和 claim token 约束的图投影/事实抽取。
- visual-worker：读取已归档且当前可用的源文件，生成受限页图并调用视觉 runtime。
- visual-runtime：只进行离线编码，不挂载知识库 core.json、数据库、飞书凭据。

先用 --help 确认当前版本参数。数据查询工具不暴露任意 SQL、Cypher、同步、改权限或纠错接口。

## 5. 私有来源权限边界

当前集合权限与来源权限取交集，Principal 必须来自可信认证层，不能由模型填写。
员工/部门/全员集合规则仍保留。来源权限另有观察序号、完整性、TTL 和版本检查。
飞书公开资料观察已接入；私有文档的群组/继承权限未完整核实时 fail-closed（拒绝访问），
不能声称已支持飞书所有类型的自动私有 ACL 同步。
详见 [SOURCE-PERMISSIONS.md](SOURCE-PERMISSIONS.md)。

撤权测试应覆盖 search/read/catalog/context/table/history/graph/asset 全部入口。
模型重排、图展开之后还要检查当前版本及权限，不能仅在请求入口鉴权。

## 6. 历史语义和已知边界

- valid_as_of：原文明确记载的业务有效时间；known_as_of：系统当时已接受并存储的认知。
- 上传时间不是事实生效时间。有效日期必须附可核验的原文引文；当前自动校验只接受
  引文中明确带时区的 ISO 时间，日期不完整时保留 unknown，不擅自补时区。
- 历史读取仍按**当前**权限检查；撤权后不能用旧版本恢复读取。
- 旧版本元数据缺失、版本尚未抽取、候选/支持集截断必须明确返回不完整状态。
- 事实 worker 只处理当前有效任务；连续快速更新时，被跳过旧版本的事实不会自动补抽。
  此时历史原文可读，但 fact_coverage=unextracted_versions、temporal_status=partial。
- 当前显式纠错允许关闭旧认知并指向新认知；已关闭的相同事实身份不能再次开启。
  需要反复撤销纠错/重开认知的场景仍需追加 episode（认知阶段）设计，不应直接改历史行。
- 旧表、历史原文、事实观察与图投影分别存在；不能因为图停用就删除事实档案。

## 7. 索引回退

从 reuse 回退时，不要直接切回旧二进制。先在新版本执行：

~~~sh
python -m knowseam.cli --config /private/core.json index-rollback-legacy
~~~

命令按版本补齐旧词项并核对计数，再把模式改为 shadow；不调用模型。
之后关闭 indexing，检查旧检索结果，再切换旧程序。增强表无需删掉。
异常缓存当前默认拒绝而非自动改写共享不可变缓存；须先排查并显式修复，避免静默污染索引。

## 8. 视觉部署

见 [VISUAL-DEPLOYMENT.md](VISUAL-DEPLOYMENT.md)。首版是独立的离线 CPU 依赖基线；
GPU 部署需要另行选择兼容的 wheel/驱动/设备配额并重新记录依赖指纹。
未获批准不得下载权重、占用 GPU、提高服务器资源限额或启用生产视觉索引。
资源不足只能报 unavailable，不能用伪向量顶替真实推理。

## 9. 上线与回滚顺序

1. 确认备份、切换窗口、责任人及旧镜像/配置；先迁移，再开启单项能力。
2. 从已授权测试集合同步，核实目录、分页、数值、版本和撤权；不对真实员工发消息。
3. 在隔离配置开放 extended_tools；旧调用方继续使用旧工具，无需新增必填参数。
4. 观察错误号、耗时、权限过期、队列积压和模型指纹。诊断默认不记录查询/正文。
5. 异常时先关闭受影响的新能力/worker，不删除档案；reuse 按第 7 节回退。
6. 未通过真实模型质量/规模门槛的能力不启用。测试替身通过不代表业务回答效果达标。

解析协议升级需要先更新支持 v1/v2 协商的 KnowSeam 客户端，再更新 Docling 服务端。
新客户端已验证可读取旧 v1 服务端，并按服务端声明的规范版本校验指纹。
旧客户端不保证接受新版服务端指纹，可能安全拒绝；不要把双向混部署描述为已支持。
