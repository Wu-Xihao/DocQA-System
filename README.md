# DocQA System

本地多模态文档问答系统，已交付 **doc2md v0.1**、**DocBase v0.1**、**AGENTS 知识消费 v0.1**、**Service 自动化运维 v0.1** 和 **Web 工作台 v0.1**。

- **[系统快速启动](Document/系统快速启动指南.md)**：先跑演示，再接入自己的文档与模型。
- [系统详细架构](Document/DocQA系统详细架构设计与实施方案.md)
- [doc2md 实现与手动测试](Document/doc2md模块设计实现与测试指南.md)
- [DocBase 实现与手动测试](Document/DocBase模块设计实现与测试指南.md)
- [AGENTS 知识消费、问答及手动测试](Document/AGENTS知识消费模块设计实现与测试指南.md)
- [Service 自动化运维、配置及手动测试](Document/Service自动化运维模块设计实现与测试指南.md)
- [Web 工作台、无头问答及手动测试](Document/Web应用模块设计实现与测试指南.md)
- [CodeAgent 消费约定](AGENTS.md)（[AGENT.md 兼容入口](AGENT.md)）
- [环境安装](Document/环境配置与依赖安装指南.md)

在项目根目录、Python 3.12 的 docqa 环境执行：

```text
python scripts/docqa.py doc2md --help
python scripts/generate_doc2md_samples.py --output runtime/manual-input
python scripts/docqa.py doc2md parse runtime/manual-input/text.pdf --output runtime/doc2md --strict
```

结果 JSON 的 `markdown_path` 指向 Markdown。`complete` 与 `review_required/partial` 分别表示完整解析和待复核；图片未配置 VL、OCR/Office 工具缺失等情况会明确报告，不能当作完整多模态验收通过。

运行测试（`--basetemp` 使用全新、专用于测试的目录）：

```text
python -m pytest -q --basetemp runtime/pytest-manual-001
```

DocBase 离线机制验收（显式 synthetic，不代表真实模型效果）：

```text
python scripts/generate_docbase_samples.py --output runtime/docbase-manual
python scripts/docqa.py docbase build runtime/docbase-manual/sources.json --output runtime/docbase --config configs/docbase.demo.yaml
python scripts/docqa.py docbase query "SD5993 的 FE_HEART_Start 如何启动心跳？" --index runtime/docbase --config configs/docbase.demo.yaml
python scripts/docqa.py docbase test runtime/docbase-manual/evaluation.json --index runtime/docbase --config configs/docbase.demo.yaml --top-k 2
```

内置问答与原文回源（复用上面的演示索引）：

```text
python scripts/docqa.py ask "SD5993 的 FE_HEART_Start 如何启动心跳？" --index runtime/docbase --state runtime/agent --config configs/agent.demo.yaml
python scripts/docqa.py source resolve --answer <返回的answer_id> --citation C0001 --state runtime/agent
```

`ask` 返回 answer_markdown、状态、引用与验证标识；`agent session` / `answer validate` 为外部 CodeAgent 提供固定版本的证据与草稿校验。demo 始终 synthetic=true、verified=false，不能当作真实语义验收。详细手动测试见 AGENTS 模块指南 M01～M17。

按 Service 指南生成并授权测试来源后，可运行自动维护：

```text
python scripts/docqa.py service source add runtime/service-input-user --types docx --state runtime/service-user --config configs/service.demo.yaml
python scripts/docqa.py service run --max-ticks 5 --poll-seconds 1 --state runtime/service-user --config configs/service.demo.yaml
python scripts/docqa.py service status --state runtime/service-user --config configs/service.demo.yaml
```

Service 提供定时/增量摄入、文档/索引/图片管理、持久任务与重启恢复。持续运行使用 `service run`，另一个终端用同一 state 的 `service stop` 协作停止；未自动安装系统服务或计划任务。

网页工作台（复用前面构建的演示索引 `runtime/docbase`）：

```text
python -m pip install -r requirements-web.txt
python scripts/docqa.py web doctor --config configs/web.demo.yaml
python scripts/docqa.py web run --config configs/web.demo.yaml
```

浏览器打开 `http://127.0.0.1:8765`。后台通过固定 Python 无头子进程调用内置受控 Agent，提供范围选择、排队/取消、历史、表格/关系图、引用与原文下载。只监听本机；演示始终明确标记 synthetic。全新目录和真实服务配置见 Web 指南 W01～W08。

真实 OCR、Office、VL、Embedding / BGE-M3 / Reranker 以及 Chat generator/verifier 需配置外部工具或服务。默认 HTTP 缺配置会失败；Mock 通过不代表真实服务已接通。系统级 GC/迁移与真实外部 CodeAgent 宿主接入尚待完成。
