# Docling 源码集成验收

## 范围

本功能是 KnowSeam 通用文档解析扩展，不包含产品 A 考核、产品 B 画像规则或公司私密词表。
2026-10-09 按用户确认，已将验证提交快进整合回 `/srv/knowseam`
的 `main`，通过三方恢复保留原有未提交修改。没有推送 GitHub、修改生产配置、
重启产品或运行生产重建索引；增强解析仍默认关闭。

完整源码以 subtree 导入 `third_party/docling`。固定基线为 Docling v2.135.0，
上游 SHA `2b3a00c5bfcd8b218d32e7e9c33d509aeaa0cb56`；本地有明确记录的
RapidOCR 3.10 公共接口兼容定制，不声称运行源码与未经修改的上游完全一致。
构建和启动均检查实际源码、依赖锁、模型文件及规范化代码摘要。

## 验收环境与边界

- 独立开发目录与测试镜像，未在现有业务镜像上覆盖。
- 实际解析器：Linux x86_64 / Python 3.12 / CPU torch 2.8.0 / RapidOCR 3.10 ONNX。
- 解析容器：UID 10001、无网络、只读根文件系统、只读模型、UDS 通道，无开放端口。
- 资源：CPU 2、内存 4 GiB、PID 256、tmpfs 512 MiB、解析并发 1。
- 实际数据库仅为单独的 `knowseam_test`，连接前校验名称和服务器当前数据库。
- 所有样例为合成企业文本、Office 表格及中文扫描图，无真实员工消息或公司资料。
- 向量推理和重排序使用确定性替身，因此证明接口、增量、权限和存储流程，不证明语义检索质量。

## 核验内容

1. 默认关闭时轻量服务不导入 Docling、不连接解析服务，原生路径继续工作。
2. 本地源码 wheel 与 vendored 实现逐文件一致；模型缺失、损坏和未登记文件拒绝启动。
3. DOCX 标题/正文/表格、XLSX 真实 worksheet 偏移/合并表头、PPTX 正文/slide。
4. PNG、JPEG、TIFF、扫描 PDF 的真实中文 OCR；空白图片不能发布为成功。
5. 元数据 URL/绝对路径拒绝，执行器不可访问公网或业务凭据；超时后继续服务。
6. 标题路径区分同名子标题，长表格按字段切片并保留行标识与来源位置。
7. 指纹变化重新解析、相同嵌入输入复用缓存；首次失败不发布空版本，旧版本失败标 stale。
8. 解析期间撤权/删除不能恢复发布；回滚重新处理当前源 revision，不恢复旧权限或删除内容。
9. 合成来源→实际解析容器→PostgreSQL→知识服务→REST→MCP 的通用链路。

## 可重复命令

维护依赖、模型和镜像准备方法见 [DOCLING-MAINTENANCE.md](DOCLING-MAINTENANCE.md)。
在专用测试环境运行：

```bash
export SUITE_KNOWLEDGE_TEST_DSN='postgresql://TEST_USER:TEST_PASSWORD@TEST_HOST/knowseam_test'
export KNOWSEAM_TEST_PARSER_SOCKET='/absolute/task/socket/parser.sock'
export KNOWSEAM_TEST_PARSER_CONTAINER='task-owned-parser-container'
export KNOWSEAM_TEST_CHINESE_FONT='/absolute/legal-cjk-font.ttc'
export KNOWSEAM_TEST_DOCLING_PYTHON='/absolute/runtime/python'
python -m pytest tests -q -rs
python -m ruff check src/knowseam tests/document_parsing scripts/*.py
```

容器检查用例要求测试容器标签 `knowseam.docling.task=20261009`；复制验收时应同步更新
该明确任务标签，不能指向生产容器。没有这些资源时用例会明确 skip，不算真实验收通过。
普通生产/默认关闭部署不需要这些测试变量。

## 独立审查

一位独立审查者只读审查并复查：Excel 原始偏移、多级表头、标题层级和长值字段关系的
四项 Important 已修复并补回归。小预算真实解析用例不再假定任意机器必然超时；
确定性睡眠子进程用例另验证 HTTP 504 后同一服务恢复 200。

## 限制与上线

首版不承诺旧 `.doc/.xls/.ppt`、音视频、Office 内嵌图片 OCR、远程 VLM 或任意网页解析。
OCR 与表格识别仍可能有误，需要检索质量样本评估；模型许可须在分发前另行复核。
当前服务和 API 不变；启用需明确添加 parser profile、模型/UDS 挂载及配置路由，
并对当前源 revision 做增量处理。上线决策和生产资源变更需另外确认。

## 最终运行结果

2026-10-09 提交版本 `4ad4c81` 的干净源码快照全量验收：
**437 passed、1 skipped、17 subtests passed**，耗时 190.55 秒。
此前快照为 436 passed，最后增加 builder 最小挂载用例后再次完成全量。
真实解析和隔离数据库用例没有跳过。唯一跳过项为原有轻量测试环境中
`tests/test_models.py:321` 的可选 torch 张量契约，未声称已做嵌入/重排序模型质量评估。
另有维护 builder 最小挂载回归 **3 passed**、规范化结构回归 **7 passed**、
执行器协议/确定性超时回归 **11 passed**，静态检查通过。

该快照仅含本任务代码，不包含部署目录原有未提交的企业词表、BM25/ACL 或来源排除改动。
同一测试环境中保留那些改动的开发目录也完成了非真实解析回归：434 passed、1 skipped、
13 deselected、17 subtests passed（该次运行早于最后新增的两项测试）。
因此既检查了当前部署代码兼容性，也检查了本次交付不依赖未提交改动；不混合两套计数。

源码基线原有八份修改文件的差异摘要与实施前一致，四份重点源文件 SHA256 检查一致，
上述检查发生于整合前。之后通过 `--ff-only --autostash` 整合到原目录，
未用旧文件整份覆盖新实现；十个重叠修改文件与已验收的组合版本逐字一致。
原有未跟踪文档/测试、私有配置和 Qwen 模型均保留。

独立审查最终通过：没有未关闭的 Critical、Important 或 Minor。审查者亲自只读执行
七份纯结构断言并核对完整运行日志；Linux 容器及数据库测试由主实施会话执行。

补充上游规范检查：唯一 vendored 修改文件通过上游 Ruff lint；格式检查发现一处换行差异，
已机械格式化并重建源码 wheel/镜像。上游 `make validate` 仅在隔离上游工作树尝试，
因专用环境没有 `prek` 退出，未安装上游全部开发依赖，也未宣称通过上游完整贡献 CI。
本次 KnowSeam 验收及选定格式的实际解析测试不受该维护工具缺失影响。

格式调整后的最终镜像另行补验：**10 passed**，耗时 108.85 秒，无跳过。
包含 PNG/JPEG/TIFF/扫描 PDF 中文 OCR、真实 Docling→隔离 PostgreSQL→REST/MCP，
以及源码身份与构建布局检查。运行时实际源码 SHA256 为
`d61d5e570aee90dbcd4c3d8304bc64539a4492dfaa06cefc78d4bd570fdcf9d4`。
完整验收日志与补验日志已归档到原目录的
`.build-docling-integration-20261009/acceptance/task-6-tests.log` 和
`post-format-tests.log`；不将两次运行计数累加为覆盖用例数。

## 原目录整合验收

2026-10-09 在原目录 `knowseam` 中，保留现有企业词表、BM25/ACL 和来源范围修改后，
完整运行 KnowSeam 测试：**449 passed、1 skipped、17 subtests passed**，196.34 秒。
真实 OCR、隔离 PostgreSQL、REST/MCP 和增量/权限用例均实际执行；唯一跳过项仍是
轻量测试环境的可选 torch 张量契约。该计数包含原目录已有的未提交测试，
不等同于前述仅包含交付提交的 437 项。

整合日志为 `.build-docling-integration-20261009/integrated-full-tests.log`。
原有 `.gitignore` 与整合前逐字一致；两份私有配置 SHA256 校验通过，原有修改和
未跟踪文件保持原先未提交状态。备份位于同一忽略目录内，目录权限 0700。
已准备的模型在 `models/docling`，公开构建依赖在 `.build-parser/wheelhouse`；
不搬迁旧测试虚拟环境，不修改生产数据库、生产容器或在线服务配置。
