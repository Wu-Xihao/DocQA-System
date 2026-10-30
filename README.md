# DocQA System

本地多模态文档问答工作台：PDF / DOCX / PPTX / XLSX / CSV → 可溯源 Markdown → 三路检索 → 有引用的回答、表格、关系图、思维导图和演示文稿。

## Windows exe 便携版

生成文件位于 `dist/DocQA/DocQA.exe`，分发包为 `dist/DocQA-Windows-x64.zip`。
解压**整个文件夹**后双击 exe 即可打开网页，无须 Python/conda；保留 `_internal` 运行库。
首次启动自动创建本机 YAML 和 `configs/.env`，填写 API 配置后重启即可真实使用。
密钥、私有资料、历史聊天和现有索引不包含在分发包中；完整 OCR/Office 渲染另需 Tesseract 与 LibreOffice。

```powershell
.\DocQA.exe --demo          # 离线合成演示
.\DocQA.exe --check         # 本机依赖检查，不调用模型
.\DocQA.exe --port 8766     # 端口被占用时
.\DocQA.exe --cli --help    # 保留原有 CLI 功能，自动载入 configs/.env
```

使用说明随包提供。数据保存在 exe 旁的 `runtime` / `data/private`，可用 `--home D:\DocQAData` 指定目录。
Web 负责上传处理；持续目录监控仍可在独立终端运行
`DocQA.exe --cli service run --state runtime/service-user --config configs/local.service.yaml`。
关闭服务时在控制台按 Ctrl+C；关闭网页不会停止后台。

源码构建使用 Windows x64 Python 3.12：安装本页依赖后执行 `python scripts/build_exe.py`。
构建脚本收集应用与公开模板、检查本机依赖并生成校验摘要；若旧分发目录已产生用户数据，会拒绝覆盖。
程序未做商业代码签名。打包设计与验证范围见[系统设计说明](Document/系统设计说明.md)。

- [系统设计说明](Document/系统设计说明.md)：当前架构、数据流、存储、校验和实现边界。
- [知识消费约定](AGENTS.md)：先检索、按证据回答、强制引用与原图核对。

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

`requirements.txt` 完整的应用与测试依赖清单，`pyproject.toml` 从中读取依赖。完整图片解析另需 **Tesseract（chi_sim、eng 语言数据）与 LibreOffice**，加入 PATH，或在 YAML 填可执行文件的绝对路径；它们不是 pip 包。

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
- `validated_parts` 仅允许成功组件入库；后台保留 partial 和质量记录，失败图片不作证据。网页不显示覆盖缺口类提示；已生成的图片释义可随图片查看。

## 4. 入库与启动

将资料放入 `data/private/documents`，首次登记目录：

```powershell
python -m dotenv -f configs/.env run -- python scripts/docqa.py service source add data/private/documents --types pdf,docx,pptx,xlsx,csv --category auto --state runtime/service-user --config configs/local.service.yaml
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

聊天输入区默认“文档检索”，沿用资料范围和引用校验；选择“正常聊天”可直接与 `local.web.yaml` 中 `agent.generator` 的 API 会话，无须先建库。正常聊天只携带同一对话近期的正常聊天上下文，不检索资料、不自动带入文档内容，也不生成文档引用或来源成果；切换模式后的下一条消息生效，重新生成沿用原消息模式。无需新增模型密钥。

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

网页无响应先检查终端并 **Ctrl+F5**；

详见[设计说明](Document/系统设计说明.md)。

内置文档回答使用原文片段编号选择机制，系统填入实际引文，减少模型复写造成的 `QUOTE_NOT_FOUND`。更新后重启 Web 并重新生成即可，无须重建索引；外部草稿仍须提供逐字原文，独立支持检查继续执行。

问答缓存、模型结构修复和图表裁剪已改进，见设计说明 11.9。重启 Service/Web 后缓存提速直接适用于旧索引；图表裁剪只影响新解析版本，旧文档需重新解析并发布后才会更新。真实配置示例使用 `image_workers: 2`、VL 超时 60 秒/网络重试 0；失败组件仍保留质量记录，不作为回答证据。

### 片段检索与统计面板

顶栏 **检索** 打开片段检索：直接返回三路召回＋重排后的原文片段、定位（PDF 页、PPT 幻灯片、Excel“工作表＋单元格区域”）和得分，可一键打开原文与 Markdown。结果是检索候选，不是已校验答案。组合条件同时满足，同类条件可多选：文档类型、内容类型（正文/表格/图片）、内容来源（原生/OCR/图片释义）、必须包含的词、文件名、章节标题、页/幻灯片范围；默认沿用左侧资料范围。

```powershell
# CLI 同样支持组合条件：仅在 Excel/CSV 表格中检索，且必须包含 SD5993
python scripts/docqa.py docbase query "SD5993 心跳周期" --index runtime/service-user/index --config configs/local.database.yaml --type xlsx --type csv --content table --must SD5993
```

HTTP：`POST /api/v1/search`，请求体 `{"question": "...", "top_k": 8, "filters": {"doc_types": ["xlsx"], "must_contain": ["SD5993"]}}`。

顶栏 **统计** 打开知识库统计面板：文档格式/分类/入库方式、分块内容类型与来源、后台解析状态、回答状态与完整回答率、端到端 P50/P95、近 14 天提问量、片段检索耗时。只读取本机元数据，不含问题或资料正文。

已有资料目录若在登记时只选了 pdf/docx/pptx，网页上传 Excel/CSV 时会自动为上传目录追加类型；监控目录需在“维护”中重新登记或用上面的 `--types` 再次添加，类型只会扩大不会缩小。

## 6. 开发与数据

```powershell
python -m ruff check src tests scripts
python -m pytest -q
```

```powershell
# 离线规模与健壮性压测：生成 DOCX/XLSX/CSV 混合资料，测解析吞吐、建库时间、
# 检索 P50/P95（顺序、并发、组合条件），并验证损坏/伪装/压缩炸弹/路径穿越文件被明确拒绝
python scripts/docqa.py quality stress --documents 300 --rows 200 --queries 100 --workers 8
```

`.github/workflows/ci.yml` 在 Windows 上执行 Ruff、带覆盖率的全量测试和压测冒烟。

Windows 测试使用短临时路径，避免超过路径长度限制。`tests/browser` 为可选 Playwright 浏览器验收，需可用的 Chrome 或另外安装的 Chromium；Node.js 用于前端语法自检。主测试集使用合成资料及模拟接口，不应改为发送私有资料到真实模型。

后台 Markdown 位于 `runtime/service-user/parsed/artifacts/<artifact_id>/document.md`，原始快照、图片、映射和质量报告一同保留。`runtime`、`data/private`、`.env`、本机配置和 `.local-archive` 不提交。旧文档只在当前版本移除，Git 历史未重写。

## 7. 本轮优化与验证

Web 示例及当前本机配置已启用 `query_workers: 2`、`reuse_workers: true` 和 `agent.generator.stream: true`。**重启 Web 后生效，无须重建索引**。正常聊天实时接收模型流；文档检索仍在引用与支持校验后发布内容。增加并发会增加模型并发与内存占用，可将 `query_workers` 调回 1。

- 相关图片新增“看图复核（VL）”：显式使用 Service 的独立视觉模型读取图片像素，不自动改变原回答的验证状态。
- 成果预览新增“编辑编排”：调整标题、思维导图/演示文稿分组、要点标题与顺序，另存版本，保留事实与引用。
- PDF 增加跨页续表候选关联；资料列表每次展示 80 条，可加载更多。

```powershell
# 不访问真实模型、仅生成合成资料，比较冷启动与进程复用
python scripts/docqa.py quality benchmark --output runtime/benchmarks --documents 4 --repeat 3
# 汇总错误、引用校验规则及阶段耗时，不输出问题或资料原文
python scripts/docqa.py quality diagnostics --state runtime/web-real --limit 100
```

标注评测集与质量门禁见[设计说明第 11 节](Document/系统设计说明.md#11-本轮三阶段优化与操作验收)。真实模型评测必须显式传 `--allow-http`，并先确认样本允许发送至已配置接口。

```powershell
# 先停止 Web 和 Service，再执行离线维护。备份目录必须是新目录。
python scripts/docqa.py maintenance backup --service-state runtime/service-user --web-state runtime/web-real --output runtime/backups/backup-20261029
python scripts/docqa.py maintenance verify-backup runtime/backups/backup-20261029
# 默认仅预览：只列出超过24小时且没有记录引用的 Web 派生文件
python scripts/docqa.py maintenance gc --web-state runtime/web-real
# 核对预览后才执行
python scripts/docqa.py maintenance gc --web-state runtime/web-real --apply
```

备份包含状态库、快照、图片、成果及浏览器会话密钥，按私有资料保管；不包含外部资料目录、本机 YAML 和 `.env`。恢复命令为 `maintenance restore <备份目录> --service-state <原Service路径> --web-state <原Web路径>`，要求两个原路径均不存在；先妥善移走现有状态，不覆盖现有目录。历史索引代不在清理范围内。


