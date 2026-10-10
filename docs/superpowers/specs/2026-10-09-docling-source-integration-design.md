# KnowSeam 通用文档解析源码整合设计

日期：2026-10-09
状态：用户已于 2026-10-09 确认设计；实施计划待核对。未实施运行代码、依赖安装或服务切换。

## 1. 目标与范围

KnowSeam 是独立、通用的企业知识库能力，不依赖产品 A、产品 B、考核场景或某一智能体底座。数据源接入、文件解析、增量索引、权限过滤、检索与对外协议由 KnowSeam 实现。产品 A 只是使用现有 API、MCP 或 SuiteHarness 适配器的消费者之一。

本次将 Docling 的完整 Git 仓库源码纳入 KnowSeam，允许维护人员直接优化内部实现，并持续合并上游更新；将其作为可配置解析引擎接入现有流程。首批范围为 PDF、扫描 PDF、PNG/JPEG/TIFF 图像、DOCX、XLSX、PPTX，以及现有飞书原生 JSON、Markdown、文本路径的兼容。

不增加产品业务工具、考核规则、客户画像逻辑，不修改 SuiteHarness 或产品 A/B 源码，不改变对外检索协议。Docling 上游包含但本次不启用的音视频、远程视觉模型、公式增强等能力不作为首批交付承诺。第三方模型与依赖仍按需引入，不承诺整个依赖生态都已复制。

## 2. 当前源码事实

服务器仓库：`/srv/knowseam`。

- `src/knowseam/domain.py` 定义 `DocumentParser(ABC)`，`parse(SourceContent) -> ParsedDocument`。`SourceContent` 包含原始字节、文件名与媒体类型；`ParsedDocument` 包含 sections、warnings、parser_version。
- `src/knowseam/parsers.py` 的 `StructuredDocumentParser` 实现文本、Office、原生飞书结构与 PDF 文本抽取，但没有 OCR，图片/扫描内容会产生警告或解析失败。
- `src/knowseam/cli.py` 的 sync/worker 路径直接构造该解析器，还没有引擎工厂或路由。
- `src/knowseam/sync.py` 在比较源版本与 pipeline_id 后决定跳过；流水线指纹目前覆盖 parser.version、chunker、向量模型、维度和 tokenizer.version。
- 同步流程在解析前刷新来源访问规则，仅对完整扫描执行删除对账；解析、切片与向量准备之后调用 store.publish。失败进入 fail_document，不应改造成空内容成功发布。
- `src/knowseam/chunking.py` 保留 section 定位，但长表格行仍可能按字符拆分；本次只对表格结构适配做必要增强，不整体更换检索架构。
- 当前仓库存在企业词典、权限、来源排除等未提交改动，均属于现有工作，不能覆盖、丢弃或混入本次提交。

这些事实来自 2026-10-09 的服务器只读检查，不代表本次功能已经实现。

## 3. 源码组织与上游维护

使用 `git subtree --squash` 将上游完整源码导入 `third_party/docling/`；不建立嵌套 Git 仓库，不使用 git submodule。普通部署人员拉取 KnowSeam 即得到所选上游快照及已提交定制。

拟定新增内容：

```text
src/knowseam/document_parsing/
  config.py                 # 引擎、格式路由与资源限制配置
  factory.py                # 工厂；默认返回原生解析器
  router.py                 # 按媒体类型与格式选择引擎
  docling_adapter.py        # 实现现有 DocumentParser ABC
  normalizer.py             # DoclingDocument 转换为 ParsedDocument
  fingerprint.py           # 实际源码、模型、配置与转换器指纹
  client.py                # 通过本机 Unix socket 请求受限解析执行器
  executor.py              # 受限解析容器入口及请求校验
  process_runner.py        # 执行器内部耗时解析进程与超时回收
  extensions/              # 自有清理、表格等解析优化
third_party/docling/       # 完整上游 Git 源文件，含 LICENSE
third_party/docling.upstream.json
deployment/Dockerfile.parser
deployment/requirements-parser.lock
scripts/docling-upstream-update.sh
scripts/build-vendored-docling.sh
tests/document_parsing/
docs/DOCLING-MAINTENANCE.md
```

保留现有 `parsers.py`，避免同名文件和新包冲突。部署入口仍是同一仓库的 Compose，不要求另行部署 docling-serve 或另一个业务仓库。

优先在自有适配层、配置、扩展接口优化企业文档。必须修改 Docling 内部时直接修改 subtree 中源码，并将这些修改与 KnowSeam 改动分别提交。Git 提交是唯一生效的补丁来源，不在镜像构建时重复应用已提交补丁。保留 subtree 合并记录与元数据，不通过重置、覆盖复制或平台二次 squash 丢失维护关系。

上游记录文件包含来源 URL、版本标签、准确提交 SHA、subtree 前缀、上游许可证、引入日期和定制提交说明。开发实现时按发布记录与兼容性核验选择稳定基线；部署固定到准确 SHA，不自动追踪 main。

更新脚本要求工作区干净，在独立升级分支执行 subtree pull；禁止自动部署、重启、推送或在冲突时覆盖本地修改。维护文档给出冲突处理、依赖更新、验证与回滚流程。普通 git pull 更新已发布 KnowSeam；subtree pull 仅供维护人员合并上游。

## 4. 构建与依赖边界

镜像必须从导入的实际源码构建并安装本地 wheel，而不是安装 PyPI 同名实现。当前上游 main 的根包是实际提供 Python `docling` 模块的 `docling-slim`，`packages/docling` 为依赖包装包；构建脚本必须根据选定版本的实际布局检查包名和产物，并锁定本地来源，避免包装包重新下载实现。

开发环境可以使用本地 editable 安装；发布镜像安装固定 wheel。构建结果记录 subtree 运行源码内容摘要、适配器版本和相关依赖版本；启动诊断验证实际加载的分发包、版本和构建指纹。不能仅用 import 路径存在证明定制已生效，至少包含一个定制行为或源码身份验证用例。

Docling 外部组件和 OCR 依赖统一进入受限解析容器的部署锁文件。锁文件不覆盖查询服务和来源同步 worker 的轻量依赖环境。模型权重独立放在部署数据卷，不进入 Git，不复制服务器私密配置和企业资料进入公开测试样例。

保留上游 MIT 版权、LICENSE，并更新 KnowSeam 的第三方许可说明。模型及其他组件分别核查其许可证；KnowSeam 的 MIT 不覆盖第三方所有资源。

## 5. 解析接入与配置

继续使用现有 DocumentParser ABC，内部新增工厂与路由；对外 ParsedDocument 保持兼容。禁用增强解析时，API、MCP、同步原生格式不需要加载 Docling 或 OCR 模型。

新增严格验证的解析配置：

| 字段 | 类型/默认 | 含义 |
| --- | --- | --- |
| parsing.docling.enabled | bool / false | 是否启用内置 Docling 引擎 |
| parsing.routes | 格式到 native/docling 的映射 / 空 | 空配置延续当前原生行为；图像仅在启用后可路由至 Docling |
| parsing.docling.model_path | 本地目录 / 未设置 | 启用涉及模型的流程必须配置并预置所需模型 |
| parsing.docling.socket_path | 绝对路径 / /run/knowseam-parser/parser.sock | 同仓库受限解析容器的 Unix socket；不开放网络端口 |
| parsing.docling.ocr_enabled | bool / true | 扫描 PDF/图像 OCR；仅增强引擎使用 |
| parsing.docling.table_structure_enabled | bool / true | PDF 表格结构识别 |
| parsing.docling.timeout_seconds | 正整数 / 120 | 单文档解析最长时间，超时回收子进程 |
| parsing.docling.max_pages | 正整数 / 200 | PDF 页面数量上限 |
| parsing.docling.max_sections | 正整数 / 50000 | 统一输出段落上限 |

文件大小、ZIP 展开大小、宏/加密拒绝策略延续现有限制；引擎路由不能绕过这些检查。明确配置 Docling 但依赖缺失、模型不存在或模型身份校验失败时返回可诊断错误，不静默假装增强成功。首次交付不启用自动二次引擎兜底；用户可以显式切回 native 路由，以避免把不同质量结果伪装为同一解析策略。

原生飞书 JSON 始终使用原生路径，其来源定位不能丢失。首批上线优先开启 PDF/图片增强，Office 保持可配置，依据通用样例的解析质量决定路由，不因产品 A 的业务需求写死。

## 6. 统一结构与表格处理

适配器读取 DoclingDocument 的结构化段落和表格，不只导出一整段 Markdown。输出保持阅读顺序，继承标题层次，保留原文中的页码、工作表、行、表格标识及可用来源定位；没有可靠定位时不伪造。

表格优先按具有表头语义的数据行形成 Section，保留工作表/表格名称、表头和值的对应关系，避免把单元格误当作无关文本。合并单元格和多级表头只能根据源结构展开；空值不填造数据。较长行需要拆分时重复必要表头和主键上下文，保持相同源行定位。不得将模型生成的解释当作原始表格事实。

初期不采用生成式视觉模型补写事实，不自动推断图片中的个人画像，也不把解析器输出作为可执行指令。截图识别只负责内容与结构抽取，之后如何使用由消费方决定。

## 7. 增量索引与失败语义

解析器对外 version 为确定性指纹，覆盖所选上游准确 SHA、实际定制运行源码摘要、输出相关依赖/模型身份、规范化器版本及解析配置。该 version 进入现有 pipeline_id，并与 ParsedDocument.parser_version 保持一致的来源规则。

同一源 revision、同一流水线跳过；解析实现或关键配置变化，即使源 revision 不变也重新处理。首版允许路由总指纹变化造成较宽的重处理范围，优先确保不漏更新；不在首版重构每格式水位线。向量缓存继续按输入内容摘要、模型身份和维度复用，解析版本变化不意味着所有向量都必须重算。

新文档结果完整解析、切片、向量和关键词准备成功后才发布；不先删除旧 active_version。失败保留可诊断状态和现有版本，是否允许旧内容被检索延续当前 stale/权限策略，不以本次集成扩大权限。来源权限收紧、删除或访问拒绝不能因旧解析结果存在而恢复授权。

回滚采用上一版本代码镜像与解析配置；保留历史索引版本和必要模型，不承诺自动全库秒级回退。使用旧解析器重新处理当前有效源 revision，再通过原有发布机制逐文档发布；不得直接激活已经过时的历史索引。仅当历史结果匹配当前源 revision、当前权限与删除状态校验通过时才允许复用。源 revision 无法核实时不可自动复用。回滚不回退当前员工权限，也不恢复已删除来源。上线前在隔离数据集验证，不直接对现有企业数据做全量重建。

## 8. 部署与安全

查询 API/MCP 和来源同步 worker 维持轻量镜像；启用 Docling 时启动同一仓库、同一 Compose 管理的受限解析容器，内置本地构建的实现，不使用 docling-serve，不引入新业务代码仓。禁用时不启动此容器。

worker 负责来源认证和网络下载，通过共享 Unix socket 向解析容器传递原始字节、文件名、媒体类型和已验证的解析选项，返回现有 ParsedDocument 兼容结构。解析执行器禁止调用者指定任意本地路径或 URL。socket 不监听 TCP、不暴露公司网络端口。

解析容器配置 network_mode: none，不加入来源/API/数据库网络；不挂载来源认证配置、数据库数据、worker 环境文件或 Docker socket。只读挂载对应模型目录，使用最小环境白名单，不传入飞书、数据库和在线模型密钥；输入仅通过 socket 提供，临时目录在受限 tmpfs 内，输出限制在有大小上限的结构化响应中。普通子进程不能提供完整的凭据、文件或网络隔离，因此不以“在 worker 内启动子进程”替代此隔离边界。

执行器内部使用受限子进程执行耗时工作，在超时后回收；限制并发、模型线程、页面数量与输出体积。容器保持非 root、最小权限、受控临时目录和资源限制；所需容器预算作为项目 Compose 配置处理，不修改服务器内核、驱动、Docker 全局配置或其他项目。禁止远程模型调用，预置模型并验证断网解析能力。

保持已有来源范围、认证和员工授权规则。解析器无权选择其他员工身份、开放集合或读取考核库。文件名、嵌入文本、错误信息视为不可信内容，不写入密钥或完整企业原文到通用错误日志。

## 9. 通用验收标准

主验收是 KnowSeam 自身的通用接入、解析、索引、权限和对外协议，不以产品 A 考核链路替代。

1. 原生文本、Markdown、飞书原生文档、Office 路由兼容，未启用增强时无需 Docling 依赖即可运行。
2. 使用合法合成/公开样例验证文本 PDF、扫描 PDF、中文图片、多列表格、合并单元格 Excel、含文本与表格 PPTX、DOCX 标题与表格的结果和定位。
3. 在无网络解析容器中使用预置模型完成真实解析，验证无运行时依赖或权重下载；模型缺失有明确失败。增加负向验收：解析容器看不到 worker 的认证配置或密钥，不可请求公网/内网外链，不接受任意 URL/本地路径；超时回收不影响来源 worker。
4. 普通克隆与镜像构建确认使用 vendored 源码，不被 PyPI 实现替换；源码定制和版本指纹变化能在测试中被观测。
5. 重复同步跳过、源修改更新、解析配置变化更新、相同向量输入复用、失败不发布空版本、非完整扫描不误删索引。
6. 全员/部门/指定员工权限过滤维持，来源撤权和删除继续有效；解析错误不得绕过授权。
7. 在隔离 PostgreSQL 数据与合成资料上走来源下载/读取、真实解析、索引发布、关键词与向量检索、重排序、API/MCP/适配器消费的通用端到端流程。可使用确定性向量/重排序替身，不调用在线生成模型、不发送员工消息；另验证真实解析模型。
8. 现有公开协议及契约测试保持兼容。消费者回归测试包含产品 A 使用现有知识库入口，但不为产品 A 编写 KnowSeam 内部特例，不用考核测试替代上述通用测试。
9. 在来源修改、删除或权限收紧后执行解析器回滚，验证使用当前 revision 重新发布，不恢复旧内容或旧权限。
10. 仅完成上述证据并保留回滚路径后，才讨论更新部署配置或同步 worker。没有上线证据时交付明确标为开发完成/未上线。

## 10. 实施前置与维护交付

在隔离开发区保存当前工作树基线，不擅自提交已有企业专属配置或私密数据，不改变其公开策略。文档提交只包含本文件。

书面设计确认后生成实施计划，划分源码引入与构建、解析适配与配置、增量和失败处理、通用验收与部署维护四个任务包；采用受控并行只读审查和单一实现写入协调，避免重叠修改。每个任务包给出可执行测试命令与验收证据。

交付包含完整源码维护方式、依赖/模型清单、公开样例、升级和回滚操作说明，以及接口保持兼容的证据。当前不推送 GitHub，不重启 A/B，不安装第三方源码或模型。

## 11. 上游依据

- Git subtree：https://github.com/git/git/blob/master/contrib/subtree/git-subtree.adoc
- Docling 构建定义：https://github.com/docling-project/docling/blob/main/pyproject.toml
- Docling 包装包：https://github.com/docling-project/docling/blob/main/packages/docling/pyproject.toml
- Docling 离线模型配置：https://docling-project.github.io/docling/usage/advanced_options/
- Docling 许可证：https://github.com/docling-project/docling/blob/main/LICENSE

这些链接用于解释维护机制；实现与部署必须记录准确版本，不能把滚动 main 链接当作固定构建来源。
