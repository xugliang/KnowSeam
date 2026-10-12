# 可选视觉检索：独立离线部署

本能力返回页图证据，不负责最终问答。默认关闭；当前尚未完成真实权重推理与中文质量验收。
以下是部署配方，**不是已在生产启用的记录**。

## 组件和数据流

API → 私有 Unix socket → visual-runtime → 多向量。
visual-worker → 已归档 PDF/图片 → 受限页图 → 同一 runtime → PostgreSQL 资产/编码。
搜索在授权范围内粗召回，最多 100 份资产做 MaxSim；下载页图时再次检查权限/当前版本。
图片通过鉴权 HTTP 或 MCP 受控资源返回，无永久公开链接。

推理容器不联网、不挂载数据库/飞书配置、不挂载 Docker socket、不默认请求 GPU。
渲染 worker 的 pypdfium2/Pillow 属 visual-render extra，模型依赖不装进文本环境。

## 依赖和许可证

- 依赖基线：colpali-engine 0.3.13、torch 2.8.0+cpu / torchvision 0.23.0+cpu、
  transformers 4.57.3、peft 0.17.1。完整传递依赖及哈希在 requirements-visual.lock。
- 锁文件已通过解析器生成；**这不等于包安装、模型加载和镜像构建已验证**。
- 模型为 vidore/colqwen2-v1.0 路线，需要完整的本地 merged checkpoint，
  不接受指向远程基座的 adapter_config.json。权重不随仓库分发。
- colpali-engine 为 MIT；模型卡声明基座 Apache-2.0、适配器 MIT。部署方须保留
  模型原始许可证及所锁各依赖许可证，不把它们全部改成 KnowSeam 的 MIT。
- 0.3.13 的默认查询前缀与该模型训练格式不同，因此代码显式使用 Query: 空格，
  并把该格式纳入指纹。模型训练检索资料主要为英文，中文资料效果必须单独验证。
  依据：[实现依赖](https://github.com/illuin-tech/colpali/blob/v0.3.13/pyproject.toml)、
  [模型卡](https://huggingface.co/vidore/colqwen2-v1.0)。

## 1. 经批准准备 wheelhouse

在独立 Linux/Python 3.12 环境，不在文本推理环境执行：

~~~sh
python -m pip download --require-hashes -r deployment/requirements-visual.lock \
  --extra-index-url https://download.pytorch.org/whl/cpu \
  --dest .build-visual/wheelhouse
docker build -f deployment/Dockerfile.visual -t knowseam-visual-runtime:0.1.0 .
~~~

构建仅从 wheelhouse 安装且校验哈希。若平台/CPU wheel 不匹配，重新批准平台并生成新锁，
不要去掉 --require-hashes 或把文本模型环境升级为视觉环境。

## 2. 配置与指纹

secrets/visual.json 只包含 VisualConfig 字段：

~~~json
{
  "enabled": true,
  "model_id": "vidore/colqwen2-v1.0",
  "model_revision": "部署时填写真实不可变版本",
  "model_path": "/models/visual",
  "socket_path": "/run/knowseam-visual/visual.sock",
  "model_sha256": "替换为64位权重指纹",
  "processor_sha256": "替换为64位处理器指纹",
  "dependency_sha256": "替换为64位运行依赖指纹",
  "device": "cpu",
  "dtype": "float32",
  "dimensions": 128,
  "timeout_seconds": 120,
  "lease_seconds": 900,
  "allow_online": false
}
~~~

示意值不是有效配置。可先使用 64 位十六进制占位串，通过镜像运行
python -m knowseam.visual.runtime --config /private/visual.json --inspect
计算已挂载本地文件及已安装包的实际指纹；只读、不下载、不执行模型。
随后将三个真实 hash 复制到运行配置以及 core.json 的 visual 段，两边必须一致。
API/worker 不需要挂载模型目录；runtime 不需要 core.json。

## 3. 显式资源与 socket 目录

由管理员为该部署选择一个私有目录，所有挂载进程使用 UID/GID 10001，
目录权限 0700。不可用 /tmp 公共目录或把 socket 权限改成 0777。
配置以下非凭据部署变量：

- KNOWSEAM_VISUAL_SOCKET_DIR、KNOWSEAM_VISUAL_MODEL_DIR。
- KNOWSEAM_VISUAL_WORKER_MEMORY / CPUS。
- KNOWSEAM_VISUAL_RUNTIME_MEMORY / CPUS。

没有默认资源承诺；示例 overlay 在变量缺失时直接报错。
先用 docker compose -f deployment/compose.yaml -f deployment/compose.visual.yaml config 检查，
经批准再 build/up。现有 worker/API 的 core.json 同时启用 features.visual、visual.enabled、
permissions.enabled。受限资料的权限刷新不能省略。

停止先停消费 worker，再停 runtime。异常退出留下 socket 时，runtime 默认拒绝覆盖。
只在确认该 runtime 已停止、路径确为该私有目录内的 socket 后清理该 socket，再重启；
不要递归清理目录或杀死其他进程。模型子进程超时会由自身 supervisor 终止并重新创建。

## 4. 验证门槛

1. 纯协议/权限/版本/租约及真实 PostgreSQL 回归通过。
2. 在明确批准的推理资源上设置 KNOWSEAM_VISUAL_TEST_CONFIG，执行
   pytest -q tests/test_visual_real.py；没有该配置会明确跳过，不能计作通过。
3. 独立中文合成表格/图表/扫描件集合，人工标注查询和页图证据；
   比较授权全集精确 MaxSim 和粗召回+MaxSim，记录 Recall、数值保真与耗时。
4. 验证撤权、版本切换、TTL 过期后资源不可下载；再测试受控 MCP image resource。
5. 当前没有经过真实模型的中文质量与资源曲线报告，因此不得宣称第三批视觉已验收。
