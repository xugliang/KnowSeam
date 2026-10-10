# 来源与第三方组件

KnowSeam 的知识核心源自本项目所有者授权拆分的 Suite Knowledge 代码；没有搬入产品 A/B 的考核、客户业务代码，也没有复制其他 Harness 框架内部实现。SuiteHarness 集成仅引用其公开 Python 接口，不内嵌框架源码。

MCP 由官方 Python SDK 实现，不自行实现私有传输协议。PostgreSQL、pgvector、Jieba、Office/PDF 解析库、模型推理依赖通过包管理器安装，不在本仓分发其源码或权重。部署方需遵守各依赖及模型权重各自的许可和使用条件。

本仓 MIT 许可证只覆盖有权授权的本仓代码，不覆盖公司文档、飞书内容或第三方模型权重。

Docling v2.135.0 的完整 Git 源码通过 subtree 保存在 third_party/docling，固定来源提交
2b3a00c5bfcd8b218d32e7e9c33d509aeaa0cb56。版权所有 The Docling Contributors，MIT；
原文许可见 third_party/docling/LICENSE。相关外部依赖和 OCR、布局、表格模型不包含在该
源码快照内，模型许可在人工准备模型时单独记录和审核，不继承 KnowSeam 的 MIT。
