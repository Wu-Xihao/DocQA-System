export const $ = (id) => document.getElementById(id);
export function el(tag, className = "", value) {
  const n = document.createElement(tag);
  n.className = className;
  if (value !== undefined) n.textContent = String(value);
  return n;
}
export function button(label, callback, className = "text-button") {
  const n = el("button", className, label);
  n.type = "button";
  n.addEventListener("click", callback);
  return n;
}
export function show(id, visible = true) {
  $(id).hidden = !visible;
}
export function text(id, value) {
  $(id).textContent = value ?? "";
}
export function clear(id) {
  $(id).replaceChildren();
}
export function recallLabel(routes) {
  if (!Array.isArray(routes)) return "检索路线未确认";
  const active = new Set(routes);
  if (
    active.size !== routes.length ||
    !active.has("dense") ||
    !active.has("bm25") ||
    routes.some((route) => !["dense", "bm25", "sparse"].includes(route))
  )
    return "检索路线未确认";
  return active.has("sparse") ? "三路检索" : "两路检索";
}
export const statusNames = {
  answered: "回答已完成",
  partial: "部分内容有据",
  insufficient_evidence: "证据不足",
  conflicting_evidence: "来源存在冲突",
  failed: "处理失败",
  cancelled: "已取消",
  succeeded: "已完成",
  queued: "等待处理",
  running: "正在处理",
};
export const stageNames = {
  queued: "等待处理",
  starting: "正在准备问答",
  running: "正在检索并检查证据",
  checking_result: "正在核对已保存回答与引用",
  planning: "正在理解问题与范围",
  retrieving: "正在检索所选资料",
  assessing: "正在检查证据覆盖",
  composing: "正在组织回答",
  streaming: "正在流式呈现已核验回答",
  validating: "正在核验原文引文",
  verifying: "正在检查回答支持",
  publishing: "正在保存结果",
  complete: "处理完成",
  cancelling: "正在取消",
};
export const errorNames = {
  CITATION_INVALID:
    "回答引用与本轮交付的证据不一致，未通过校验；本轮未发布未经核验的结论。",
  UNSUPPORTED_ITEMS_REMOVED:
    "已移除未通过原文支持检查的内容，当前回答只保留有据部分。",
  EVIDENCE_OMITTED_BY_BYTE_BUDGET:
    "部分候选片段受证据预算限制，回答仅依据实际交付的原文。",
  SESSION_EVIDENCE_BUDGET_EXHAUSTED:
    "本轮证据容量已用完，尚未交付的片段没有参与回答。",
  INDEX_BUILD_FAILED:
    "文档已解析，但知识库构建失败。请查看 Service 任务错误后重试解析。",
  CONVERSATION_BUSY: "当前对话仍在生成，请等待完成或取消。",
  SCOPE_EMPTY: "请至少选择一份可用资料。",
  SCOPE_CHANGED: "所选资料不属于当前对话版本，请使用新资料版本后重试。",
  CONVERSATION_CHANGED: "对话或资料版本已变化，请刷新后重试。",
  UPLOAD_NOT_CONFIGURED: "当前为独立查询模式，请配置 Service 索引后启用上传。",
  UPLOAD_ROOT_NOT_ALLOWED: "上传目录尚未加入后台允许范围，请检查本机配置。",
  SOURCE_ROOT_OVERLAP:
    "监控目录与已登记目录重叠，请选择原有根目录，或先停用并移除旧监控。",
  SOURCE_ACCESS_DENIED: "此路径不在后台允许的资料目录中。",
  SOURCE_ROOT_NOT_ALLOWED:
    "此目录不在后台 allowed_source_roots 中，请先在 Service 配置中授权。",
  DELETION_CLEANUP_PENDING: "记录已移除，后端文件仍在清理，将自动重试。",
  FORMAT_UNSUPPORTED: "请上传 PDF、DOCX 或 PPTX 文件。",
  BODY_TOO_LARGE: "文件超过大小限制。",
  UPLOAD_INCOMPLETE: "文件未传输完整，请重试。",
  ARTIFACT_EVIDENCE_MISSING: "当前回答没有可用于生成成果的已支持内容。",
  ARTIFACT_RELATIONS_MISSING:
    "当前回答还没有通过检查的关系，请先生成关系图回答。",
  MODEL_RESPONSE_INVALID: "模型返回的结构不合格，自动修复后仍未通过。",
  QUOTE_NOT_FOUND: "模型引文没有与原文逐字匹配，系统已阻止发布。",
  MODEL_NOT_CONFIGURED: "模型服务尚未完成配置。",
  MODEL_AUTH_FAILED: "模型认证失败，请检查本机密钥。",
  MODEL_SERVICE_UNAVAILABLE: "模型接口暂时不可用，请稍后重试。",
  RUNNER_TIMEOUT:
    "后台问答达到总处理时限，已停止本次任务。可重新发送；若持续出现，请检查后台阶段日志。",
  AGENT_DEADLINE_EXCEEDED:
    "本次检索与回答的时间预算已用尽，尚未完成校验的内容未发布。请重试。",
  MODEL_DEADLINE_EXCEEDED: "模型请求超过本次问答剩余时间，请重试。",
  MODEL_SERVICE_TIMEOUT:
    "模型接口响应超时，请稍后重试；持续超时请检查模型服务或网络。",
  SOURCE_HASH_MISMATCH: "来源完整性检查未通过，已停止展示。",
  PROFILE_MISMATCH: "索引与当前模型或入库策略配置不同。",
  WEB_QUEUE_FULL: "当前队列已满，请等待已有任务完成。",
  EXPORT_UNSUPPORTED: "这种成果不支持该导出格式。",
  EXPORT_NOT_READY: "文件仍在生成，请稍后再试。",
  ARTIFACT_TOO_LARGE: "内容过多，请缩小问题范围后导出。",
};
export function errorText(error) {
  return (
    errorNames[error.code] ||
    (error.name === "AbortError"
      ? "连接等待超时，请重试。"
      : `操作未完成（${error.code || error.message || "连接异常"}）`)
  );
}
export function notice(message, error = false) {
  text("status-banner", message);
  $("status-banner").classList.toggle("error", error);
  show("status-banner", !!message);
}
let toastTimer;
export function toast(message) {
  text("toast", message);
  show("toast");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => show("toast", false), 5000);
}
export function locationText(locators) {
  return (locators || [])
    .map(
      (l) =>
        l.display_label ||
        (l.slide
          ? `幻灯片 ${l.slide}`
          : l.page
            ? `第 ${l.page} 页`
            : "原生文档对象"),
    )
    .join("；");
}
export function sourceWarning(answer) {
  return (answer.warnings || []).some((w) =>
    w.startsWith("SOURCE_COVERAGE_PARTIAL:"),
  );
}
export function richText(text) {
  const root = el("div", "rich-text");
  for (const piece of String(text).split(/(```[\s\S]*?```)/g)) {
    if (piece.startsWith("```")) {
      const pre = el("pre");
      pre.append(
        el("code", "", piece.replace(/^```[^\n]*\n?/, "").replace(/```$/, "")),
      );
      root.append(pre);
      continue;
    }
    for (const line of piece.split("\n")) {
      if (!line.trim()) continue;
      const heading = /^(#{1,4})\s+/.exec(line);
      const n = el(heading ? `h${Math.min(heading[1].length + 1, 5)}` : "p");
      const content = heading ? line.slice(heading[0].length) : line;
      for (const part of content.split(/(\*\*[^*]+\*\*|`[^`]+`)/g))
        n.append(
          part.startsWith("**")
            ? el("strong", "", part.slice(2, -2))
            : part.startsWith("`")
              ? el("code", "", part.slice(1, -1))
              : document.createTextNode(part),
        );
      root.append(n);
    }
  }
  return root;
}
export async function copy(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast("已复制");
  } catch {
    toast("复制未获允许，请手动选择文字。");
  }
}
export function download(url, filename) {
  const a = el("a");
  a.href = url;
  a.download = filename || "";
  document.body.append(a);
  a.click();
  a.remove();
}
