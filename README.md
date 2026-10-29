# DocQA System

本地多模态文档问答工作台：PDF / DOCX / PPTX → 可溯源 Markdown → 三路检索 → 有引用的回答、表格、关系图、思维导图和演示文稿。

- [系统设计说明](Document/系统设计说明.md)：当前架构、数据流、存储、校验和实现边界。
- [知识消费约定](AGENTS.md)：先检索、按证据回答、强制引用与原图核对。
- 配置只提交 `configs/*.example.yaml` 和 `configs/.env.example`；资料、密钥和运行数据不入库。

## 1. 安装环境

在项目根目录使用 **Python 3.12、64 位**。已有 conda 环境时直接激活，无须重建。

```powershell
conda create -n docqa python=3.12 -y
conda activate docqa
python -m pip install -r requirements.txt
python -m pip install -e . --no-deps
python scripts/check_environment.py
python -m pip check
```

`requirements.txt` 是唯一完整的应用与测试依赖清单，`pyproject.toml` 从中读取依赖。完整图片解析另需 **Tesseract（chi_sim、eng 语言数据）与 LibreOffice**，加入 PATH，或在 YAML 填可执行文件的绝对路径；它们不是 pip 包。

## 2. 离线演示

```powershell
python scripts/prepare_workspace_demo.py
python scripts/docqa.py web run --config configs/web.demo.example.yaml
```

打开 **http://127.0.0.1:8765**。演示使用隔离的 `runtime/workspace-demo-*`，不调用真实模型；`synthetic=true`，不代表真实语义效果。演示为两路，下面的真实配置为三路。切换前按 **Ctrl+C** 停止演示。

## 3. 配置真实模型

复制示例为本机文件，以下命令保留已有文件：

```powershell
$templates = @{
  'doc2md.example.yaml' = 'local.doc2md.yaml'
  'docbase.example.yaml' = 'local.database.yaml'
  'agent.example.yaml' = 'local.agent.yaml'
  'service.example.yaml' = 'local.service.yaml'
  'web.example.yaml' = 'local.web.yaml'
  '.env.example' = '.env'
}
foreach ($name in $templates.Keys) {
  $destination = Join-Path 'configs' $templates[$name]
  if (-not (Test-Path -LiteralPath $destination)) {
    Copy-Item -LiteralPath (Join-Path 'configs' $name) -Destination $destination
  }
}
New-Item -ItemType Directory -Force data/private/documents, data/private/web-uploads | Out-Null
```

在 `configs/.env` 填 `DASHSCOPE_API_KEY`、`DASHSCOPE_EMBEDDING_ENDPOINT`、`GITEE_AI_API_KEY`、`DEEPSEEK_API_KEY`。Endpoint 必须是控制台提供的**完整原生 text-embedding URL**，不能填 OpenAI 兼容 embeddings URL。环境文件不会自动载入，后面的真实命令统一通过 dotenv 启动。

| 配置 | 核对内容 |
| --- | --- |
| `local.doc2md.yaml` | 单独解析使用；OCR/Office 路径、语言数据、VL、授权目录 |
| `local.database.yaml` | 独立检索；dense＋BM25＋sparse、维度、重排与准入策略 |
| `local.service.yaml` | 授权根、类型与间隔；`parse` 为后台实际解析配置，`retrieval` 为建库配置 |
| `local.web.yaml` | Service 配置、索引/状态/上传目录、Chat、`agent.retrieval`、时间预算 |
| `local.agent.yaml` | 仅 CLI `ask` / 外部证据会话需要；网页使用 Web 内嵌 Agent |

示例沿用项目当前配置：百炼 `qwen3.7-text-embedding-flash` 联合 dense/sparse，模力方舟 `Qwen3-Reranker-8B` 重排、`Qwen3.6-27B` 视觉解析，DeepSeek `deepseek-flash` 负责回答编排和独立支持检查。**模型 ID、端点及权限以实际部署为准**；视觉产品名称不能直接替代平台接受的 model ID。

- Service `retrieval`、Web `agent.retrieval` 和独立 DocBase 配置必须一致；YAML 不会自动同步或 include。
- 后台不自动读取 `local.doc2md.yaml`；修改解析参数时同步更新 Service `parse`。
- 示例授权根与 Web 状态路径按 YAML 所在目录解析；OCR/Office 使用 PATH 或绝对路径。
- `validated_parts` 仅允许成功组件入库；覆盖不全仍标记 partial，失败图片不作证据。

## 4. 入库与启动

将资料放入 `data/private/documents`，首次登记目录：

```powershell
python -m dotenv -f configs/.env run -- python scripts/docqa.py service source add data/private/documents --types pdf,docx,pptx --category auto --state runtime/service-user --config configs/local.service.yaml
```

终端 A 持续维护资料：

```powershell
python -m dotenv -f configs/.env run -- python scripts/docqa.py service run --state runtime/service-user --config configs/local.service.yaml
```

终端 B 启动网页：

```powershell
python -m dotenv -f configs/.env run -- python scripts/docqa.py web run --config configs/local.web.yaml
```

打开 **http://127.0.0.1:8765**，等首批解析和建库完成后提问。文件夹上传只上传文档副本；**浏览器不能持续监控原文件夹**。持续监控需新增服务器可访问的监控目录，且位于 Service 授权根内。

Web `service_mode: auto` 可驱动上传任务；`external` 需要终端 A 常驻；`disabled` 不自动摄入。普通目录定时维护推荐保留终端 A。进程通过持久队列和锁协调，不需要另装数据库服务。

## 5. 状态、重建与排错

```powershell
python -m dotenv -f configs/.env run -- python scripts/docqa.py service index status --state runtime/service-user --config configs/local.service.yaml
python -m dotenv -f configs/.env run -- python scripts/docqa.py service documents --state runtime/service-user --config configs/local.service.yaml
python -m dotenv -f configs/.env run -- python scripts/docqa.py service job list --state runtime/service-user --config configs/local.service.yaml

# 修改模型、维度、路线、切片或准入策略后，排队重建并执行任务
python -m dotenv -f configs/.env run -- python scripts/docqa.py service index build --state runtime/service-user --config configs/local.service.yaml
python -m dotenv -f configs/.env run -- python scripts/docqa.py service run --once --state runtime/service-user --config configs/local.service.yaml
```

`queued/running` 都不是完成，按返回 job ID 查询，直到 `succeeded`。一次 `run --once` 不保证整个队列清空，未完成则继续运行 Service。索引与配置不一致时同步三处检索配置、完成重建，再重启 Web；历史对话固定旧索引，请升级资料版本或新建对话。

网页无响应先检查终端并 **Ctrl+F5**；`documents=0` 查看文档状态和解析质量；`CITATION_INVALID` 表示草稿引用未通过校验，不应关闭校验。完整解析缺工具或 VL 时补齐后重新解析，详见[设计说明](Document/系统设计说明.md)。

## 6. 开发与数据

```powershell
python -m ruff check src tests scripts
python -m pytest -q
```

Windows 测试使用短临时路径，避免超过路径长度限制。`tests/browser` 为可选 Playwright 浏览器验收，需可用的 Chrome 或另外安装的 Chromium；Node.js 用于前端语法自检。主测试集使用合成资料及模拟接口，不应改为发送私有资料到真实模型。

后台 Markdown 位于 `runtime/service-user/parsed/artifacts/<artifact_id>/document.md`，原始快照、图片、映射和质量报告一同保留。`runtime`、`data/private`、`.env`、本机配置和 `.local-archive` 不提交。旧文档只在当前版本移除，Git 历史未重写。

历史兼容的独立本地 BGE-M3 编码服务不属于当前百炼部署；按需 `pip install -e ".[legacy-sparse]"` 安装额外依赖，无须另一份 requirements 文件。
