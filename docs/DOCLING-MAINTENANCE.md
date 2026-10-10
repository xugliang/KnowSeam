# 可选 Docling 源码解析能力

KnowSeam 是通用企业知识库，产品 A/B 和其他智能体仅使用现有 API/MCP。
本能力默认关闭，不改变原有配置、权限体系或对外检索协议。

## 一份仓库，两类运行环境

普通 `git clone` / `git pull` 包含 third_party/docling 的完整源码，无子模块。
轻量 worker 下载文件并通过 Unix socket 发送字节；独立解析容器执行 Docling。
容器无网络、数据库凭据或来源配置，不向局域网开放端口；内置子进程负责超时回收，
子进程本身不是沙箱。模型、构建缓存和部署私密配置不进 Git。

## 本地构建与依赖锁

固定上游版本和 SHA 见 third_party/docling.upstream.json；实际实现包名是 docling-slim，
不要从 packages/docling 构建仅依赖其他包的包装 wheel。

在专用 Python 构建环境安装 build、hatchling、uv 后执行：

```bash
bash scripts/lock-parser-dependencies.sh
bash scripts/prepare-parser-wheelhouse.sh
bash scripts/build-vendored-docling.sh
```

锁定 Linux x86_64 / Python 3.12 / CPU torch。requirements-parser.lock 不包含 PyPI
Docling 实现；Docling wheel 单独由完整本地源码构建并逐文件验证。
依赖 wheelhouse 需按锁文件下载、校验哈希，解析镜像随后以 no-index 安装。
parser-build.json 记录上游 SHA、实际源码摘要及锁文件摘要，不依赖镜像里的 .git。

## 手工准备模型与启用

只在维护阶段允许联网下载，执行器运行时禁止下载：

```bash
bash scripts/prepare-parser-models.sh models/docling
docker compose -f deployment/compose.yaml -f deployment/compose.docling.yaml --profile docling build parser
```

模型准备目录必须是不存在的专用目录，不覆盖现有模型。manifest.json 包含文件 SHA256、
上游模型来源和许可复核提示。RapidOCR 使用 ONNX Runtime 中文 small 模型；模型放在
模型根下 RapidOcr，布局和表格模型保留各自目录。模型许可须独立审核。

若解析服务器下载慢，可在维护机器准备同版本模型，然后把包含 manifest.json 的完整目录传入：

```bash
python scripts/prepare_parser_models.py models/docling-new --import-from /path/to/prepared-models
```

离线导入先校验每份文件及完整集合，拒绝覆盖旧目录；仅新模型目录设置为目录 0755、文件
0644，确保非 root 解析账号可读。挂载仍为只读，不要求修改宿主系统配置。

配置 worker 的 parsing.docling.enabled=true，按需要设置 routes，例如：

```json
{"parsing":{"routes":{"pdf":"docling","png":"docling","jpg":"docling","jpeg":"docling","tif":"docling","tiff":"docling"},"docling":{"enabled":true,"socket_path":"/run/knowseam-parser/parser.sock"}}}
```

model_path 指执行器 namespace，不要求 worker 能读模型。Office 可以按格式显式路由；
飞书原生 JSON 始终原生解析。明确启用增强但缺依赖、模型或服务时失败，不静默切换原生。
120 秒 / 200 页 / 50000 段上限及 30 MiB 输入限制不能被请求提高。
启用前确认 worker 和 parser 为 UID/GID 10001，模型目录只读可访问。
需要确认后的上线步骤是构建轻量镜像、解析镜像及更新 worker 配置；本次开发不代表已上线。

## 定制与上游更新

优先修改 src/knowseam/document_parsing/extensions；必要时直接改 third_party/docling。
源码定制必须提交，不在构建阶段重复应用补丁。上游升级要求干净工作树和独立分支：

```bash
bash scripts/docling-upstream-update.sh v2.135.0 2b3a00c5bfcd8b218d32e7e9c33d509aeaa0cb56
```

维护机器须具备 git subtree。升级不会自动推送、部署或重启；冲突需人工合并。
更新来源 JSON、依赖锁和模型清单，重新构建、验证后才可发布。
当前 Dockerfile 固定本地 wheel 名称；跨版本升级时也要同步该名称，不可继续安装旧 wheel。
普通使用者只需要 git pull，不执行 subtree 更新命令。

当前保留一处 Docling 源码兼容修改：RapidOCR 3.10 删除私有 PP_OCRV6_LANGS 常量后，
改用公共 list_supported_langs(REC, PPOCRV6, SMALL)，仍兼容旧常量存在的版本。
位置为 third_party/docling/docling/models/stages/ocr/rapid_ocr_model.py。
上游 SHA 表示导入基线，实际 runtime_source_sha256 同时包含该定制；升级时需核对补丁和回归。

结构规范化保留标题路径、worksheet 原始行/列起点、slide/page 和多级合并表头。
长表格行按字段区间切片，并重复该字段名和行标识，不能把不同列的长值混合为无归属正文。

## 失败、权限和回滚

日志 knowseam.sync 的 document_sync_failed 记录文档 ID、失败阶段、异常类，不输出原文、
凭据或第三方异常正文。解析失败保留旧活动版本并标为 stale；首次失败不发布空内容。
失败状态是否可检索继续服从当前 ACL。发布事务再次检查来源/文档可见性；撤权不能被恢复。

切回上一版本代码/配置后，重新处理**当前有效源 revision**。不直接激活过时索引，不回退
员工授权、不恢复已删除来源。版本变化可复用相同嵌入输入的向量缓存。
源码、依赖、模型、规范化代码、选项和路由变化进入 pipeline_id。

socket 启动只清理同 UID、未出现在 /proc/net/unix 且 inode 未变的旧 socket；活动、其他
UID、符号链接或普通文件一律拒绝。无法安全判断时由管理员核验，不能宽泛删除目录。

首版不承诺旧 .doc/.xls/.ppt、音视频、远程 VLM、Office 嵌入图片 OCR 或任意网页解析。
