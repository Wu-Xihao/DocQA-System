# DocQA Git 版本管理指南

> 日期：2026-10-24。  
> 项目目录：`E:\Documents\ChatGPTWorkSpace\DocQA System`。  
> 检查时 Git 版本为 `2.44.0.windows.1`，项目及父目录尚未发现 Git 仓库。  
> 本次已准备 `.gitignore`、`.gitattributes`；尚未初始化、提交、配置远程或推送。

## 1. 版本管理范围

| 应提交 | 默认不提交 |
| --- | --- |
| 系统设计、需求和使用文档 | `.env`、API key、真实凭证 |
| `Document/pict1.jpg`～`pict8.jpg` 设计参考图 | `runtime/` 中的原文快照、图片、向量、索引、注册库 |
| 源码、测试、工具脚本 | Conda/venv 环境目录、缓存、日志 |
| requirements 清单、经验证的项目锁文件 | `configs/local.yaml` 等本机真实配置 |
| 脱敏配置模板、prompt 模板 | `.design-work/` 临时设计笔记和验证输出 |
| 可公开或获准入仓的微型测试 fixtures | `data/private/` 中的业务文档 |
| 经审查的脱敏评估报告 | 评估原始敏感输出及机器环境快照 |

当前参考照片合计约 1.3 MiB，可作为设计资料提交。照片包含内部项目线索，远程建议使用允许存放这些资料的私有仓库。私有设置不替代组织对资料存放位置的要求。

配置中的秘密只通过环境变量/本机配置读取，仓库只留占位模板。`.gitignore` 只影响未跟踪文件；已经提交的秘密不会因后来加入忽略规则而从历史消失。[Git ignore 官方说明](https://git-scm.com/docs/gitignore)

## 2. 初始化本地仓库

在 PowerShell 中执行：

```powershell
Set-Location -LiteralPath 'E:\Documents\ChatGPTWorkSpace\DocQA System'
git init -b main
```

使用 Anaconda Prompt/CMD 时，第一行替换为：

```bat
cd /d "E:\Documents\ChatGPTWorkSpace\DocQA System"
```

Git 仓库建在项目根目录，不建在整个 `ChatGPTWorkSpace` 中，也不需要把 Conda 环境放入项目。[git init 官方说明](https://git-scm.com/docs/git-init)

## 3. 配置提交身份

将下列姓名、邮箱替换为你希望显示在提交记录中的实际信息：

```text
git config --local user.name "你的姓名或昵称"
git config --local user.email "你的提交邮箱"
git config --local core.quotepath false
git config --local --get user.name
git config --local --get user.email
```

`--local` 仅设置当前仓库。提交邮箱会进入提交历史，可使用托管平台提供的隐私邮箱。该身份用于作者记录，不是远程登录凭证。[Git config 官方说明](https://git-scm.com/docs/git-config)

`.gitattributes` 已规定源码/文档使用 LF、BAT/CMD 使用 CRLF、图片等按二进制处理；Markdown 行尾双空格可用于硬换行，因此仅对 Markdown 关闭行尾空格告警。不需要为此修改全局换行配置。[Git attributes 官方说明](https://git-scm.com/docs/gitattributes)

## 4. 检查文件并首次提交

先查看候选和忽略项：

```text
git status --short
git status --short --ignored
```

首次提交显式添加当前项目文件：

```text
git add .gitignore .gitattributes Document scripts requirements.txt requirements-dev.txt requirements-web.txt requirements-service.txt
git diff --cached --stat
git diff --cached --check
git diff --cached
```

检查暂存文件确实是设计资料、脚本和依赖清单，没有密码、业务数据或运行产物。图片是二进制，需在文件列表中确认名称并按需要打开审阅。

确认后提交：

```text
git commit -m "chore: initialize DocQA project baseline"
git log --oneline -5
git status
```

首次提交固定的是“设计与环境准备基线”，不能把尚未实现的解析/检索功能描述为已完成。工作区干净且能看到提交即完成本地版本管理初始化。

## 5. 关联远程仓库，可选

在选定的托管服务中创建空仓库，例如命名为 `docqa-system`。为直接接收现有本地历史，新建时不自动生成 README、LICENSE 或其他首个提交；复制其 HTTPS 或 SSH 克隆地址。

将 `REMOTE_URL` 替换为实际地址，保留引号：

```text
git remote add origin "REMOTE_URL"
git remote -v
git push -u origin main
```

远程认证按实际平台处理，不将 token 写进 remote URL 或项目文档。若远程已存在提交，先获取并检查历史再整合，不以强制推送覆盖未知内容。

本地 commit 保存历史，push 才把已提交历史同步到远程；源码仓库不能代替 runtime 知识库与原文资产的独立备份。

## 6. 日常开发方式

当前单人项目采用 `main + 短期功能分支` 即可。main 保留已验证基线，每个分支对应一个明确任务或可验证小切片。

开始开发：

```text
git switch main
git status
git switch -c feat/s0-config-contracts
```

如果已配置远程且工作区干净，在创建分支前先执行 `git pull --ff-only`。尚未设置远程时不需要 pull。

改动完成后：

```text
git status --short
git diff
git add PATH_TO_CHANGED_FILE
git diff --cached
git diff --cached --check
git commit -m "feat: add S0 configuration contracts"
```

`PATH_TO_CHANGED_FILE` 替换为实际文件或目录；按改动范围添加。涉及项目代码时执行对应验证命令，记录测试结果；没有测试文件的阶段不把“没有发现测试”称为测试通过。

单人确认功能分支可合入后：

```text
git switch main
git merge --ff-only feat/s0-config-contracts
```

若 main 已前进而无法快进，先比较双方改动并解决整合问题；`--ff-only` 会安全停止，不会自动制造合并。配置远程后执行 `git push origin main`。多人协作时推送功能分支并走 PR/MR 审阅。

## 7. 提交粒度和命名

| 前缀 | 使用情况 | 示例 |
| --- | --- | --- |
| docs | 需求/设计/操作说明 | `docs: clarify source locator precision` |
| chore | 工程、依赖、配置准备 | `chore: add dependency bootstrap files` |
| feat | 新增能力 | `feat: add PDF native text parser` |
| fix | 修复缺陷 | `fix: retain document target after parse failure` |
| test | 新增有效验证 | `test: cover generation publish interruption` |
| refactor | 行为不变的结构调整 | `refactor: separate source resolver port` |
| perf | 经测量的性能优化 | `perf: batch dense encoding requests` |

优先一个提交表达一个可说明的目的，任务编号如 S1-02 放入提交正文或相关记录。不要只写“更新”“修改”“最终版”。环境安装快照不等于经验证项目锁文件；后者形成后应入仓。

## 8. 查看历史与定位问题

```text
git status
git diff
git diff --cached
git log --oneline --graph --decorate -15
git show COMMIT_ID
```

`git diff` 看未暂存改动，`git diff --cached` 看将要提交的内容。已经共享的错误提交优先用新的修复提交或 `git revert COMMIT_ID` 撤销其影响；revert 也会修改工作区，应在清楚目标且工作区合适时执行。

若已跟踪文件需要退出版本管理，先明确文件和影响，再使用 `git rm --cached -- PATH` 并提交；它保留本机文件，但不会清除旧历史。真实密钥误提交时还需立即失效/轮换密钥并处理历史，不能仅删当前文件。

## 9. 阶段版本与验收记录

S0、S1 等阶段验收通过后，在对应已验证提交上创建附注标签，例如：

```text
git tag -a v0.1.0 -m "Validated DocQA core milestone"
git push origin v0.1.0
```

只有该里程碑真实通过时才使用示例标签；当前设计基线不等于核心系统已实现。每份验收报告记录 commit、配置/profile、corpus/generation、模型版本和结果。Git 记录实现与文档历史，运行数据版本继续使用架构规定的 revision/artifact/generation 机制。
