/* DocQA's offline UI renders only checked, structured answer fields as text. */
const $ = (id) => document.getElementById(id);
const apiRoot = "/api/v1";
const finalStates = new Set(["succeeded", "failed", "cancelled"]);
const statusLabels = {
  answered: "回答已完成", partial: "部分要点有证据", insufficient_evidence: "证据不足",
  conflicting_evidence: "来源存在冲突", failed: "处理失败", cancelled: "已取消",
  queued: "等待处理", running: "正在处理", succeeded: "已完成",
};
const statusDescriptions = {
  partial: "仅展示得到来源支持的内容，未解决要点列在下方。",
  insufficient_evidence: "当前检索范围中没有足够的可用证据，系统未补造结论。",
  conflicting_evidence: "来源之间存在尚未解决的差异，请结合下列引文核对适用条件与版本。",
  failed: "后台处理发生故障；这不代表知识库中没有相关资料。",
  cancelled: "任务已取消，未发布未经检查的答案。",
};
const origins = { native: "原生文字", ocr: "OCR 转录", vl_generated: "图片释义" };
const stages = {
  queued: "已入队，等待处理", preparing: "正在准备问答", starting: "正在准备问答",
  running: "正在检索并检查证据", retrieving: "正在检索知识库", retrieval: "正在检索知识库",
  answering: "正在生成受控回答", composing: "正在组织回答", verifying: "正在检查回答与来源",
  validating: "正在核验引用", publishing: "正在保存已检查结果", succeeded: "处理完成",
  failed: "处理失败", cancelled: "任务已取消", cancelling: "正在申请取消",
};
const errorMessages = {
  WORKER_UNAVAILABLE: "问答工作进程尚未就绪，请稍后重试或检查后台服务。",
  WEB_WORKER_UNAVAILABLE: "问答工作进程尚未就绪，请稍后重试或检查后台服务。",
  MODEL_NOT_CONFIGURED: "真实模型服务尚未配置，请先完成后台模型配置。",
  MODEL_REQUEST_FAILED: "模型服务请求失败，请检查服务连通性后重试。",
  MODEL_DEADLINE_EXCEEDED: "问答处理超时，请稍后重试或缩小问题范围。",
  WEB_QUEUE_FULL: "当前任务较多，请等待已有任务完成后再试。",
  WEB_CSRF_FAILED: "页面会话已失效，请刷新页面后重试。",
  WEB_SESSION_INVALID: "页面会话已失效，请刷新页面后重试。",
  WEB_SESSION_EXPIRED: "页面会话已失效，请刷新页面后重试。",
  WEB_FORBIDDEN: "本次请求未通过页面访问检查，请刷新页面后重试。",
  WEB_JOB_INTERRUPTED: "后台在处理期间重启，请重新提交问题。",
  WEB_CANCELLED: "任务已取消。",
  INDEX_NOT_FOUND: "知识库尚未发布，请先完成文档解析与索引构建。",
  GENERATION_NOT_FOUND: "所选知识库版本不可用，请刷新页面后重试。",
  SOURCE_HASH_MISMATCH: "来源完整性检查未通过，系统已停止展示该资源。",
};
const state = {
  token: "", bootstrap: null, documents: [], busy: false, activeJob: null,
  currentAnswer: null, pollTimer: null, pollFailures: 0, requestSequence: 0,
  sourceSequence: 0, graphScale: 1, graphSize: null, toastTimer: null, lastSourceButton: null,
};

function element(tag, className, value) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (value !== undefined && value !== null) node.textContent = String(value);
  return node;
}

function text(id, value) { $(id).textContent = value === undefined || value === null ? "" : String(value); }
function clear(id) { $(id).replaceChildren(); }
function list(value) { return Array.isArray(value) ? value : []; }
function show(id, visible = true) { $(id).hidden = !visible; }
function cleanString(value) { return typeof value === "string" ? value : ""; }

function banner(message = "", kind = "warning") {
  text("status-banner", message);
  $("status-banner").classList.toggle("error", kind === "error");
  show("status-banner", Boolean(message));
}

function toast(message) {
  clearTimeout(state.toastTimer);
  text("toast", message);
  show("toast");
  state.toastTimer = setTimeout(() => show("toast", false), 4500);
}

class RequestError extends Error {
  constructor(code, status = 0) { super(code); this.code = code; this.status = status; }
}

function describeError(error) {
  if (error.name === "AbortError") return "连接等待超时，请检查后台服务状态后重试。";
  if (error.code && errorMessages[error.code]) return errorMessages[error.code];
  if (error.status === 401 || error.status === 403) return "页面会话校验失败，请刷新页面后重试。";
  if (error.status === 422 || error.status === 400) return "输入未通过校验，请检查问题长度、要点数量与检索范围。";
  if (error.status === 429) return "请求过于频繁或任务队列已满，请稍后重试。";
  if (error.status === 404) return "该记录或来源资源已不可用，请刷新问答记录。";
  if (error.status === 503) return "后台依赖服务暂时不可用，请检查服务状态后重试。";
  if (error.status) return "后台处理请求失败，请查看服务日志中的错误编号。";
  return "无法连接本地后台，请确认 Web 服务仍在运行。";
}

async function request(path, { method = "GET", body, bootstrap = false } = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 20000);
  try {
    const headers = { Accept: "application/json" };
    if (!bootstrap) headers["X-DocQA-Token"] = state.token;
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const response = await fetch(`${apiRoot}${path}`, {
      method, headers, credentials: "same-origin", cache: "no-store", signal: controller.signal,
      ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
    });
    const payload = await response.json().catch(() => null);
    if (!response.ok) {
      const code = cleanString(payload?.error?.code) || `HTTP_${response.status}`;
      throw new RequestError(code, response.status);
    }
    if (!payload || typeof payload !== "object") throw new RequestError("INVALID_RESPONSE");
    return payload;
  } finally { clearTimeout(timer); }
}

function setBusy(busy) {
  state.busy = busy;
  $("ask-button").disabled = busy || !state.bootstrap?.worker_ready;
  $("ask-button").firstElementChild.textContent = busy ? "任务处理中" : "检索并回答";
  $("question-form").setAttribute("aria-busy", String(busy));
}

function selectedScope() {
  return {
    categories: $("category-select").value ? [$("category-select").value] : null,
    doc_ids: $("document-select").value ? [$("document-select").value] : null,
  };
}

function populateDocuments() {
  const category = $("category-select").value;
  const previous = $("document-select").value;
  clear("document-select");
  $("document-select").append(element("option", "", "范围内全部文档"));
  $("document-select").firstElementChild.value = "";
  state.documents.filter((doc) => !category || doc.category === category).forEach((doc) => {
    const option = element("option", "", doc.source_name || doc.doc_id);
    option.value = doc.doc_id;
    $("document-select").append(option);
  });
  if ([...$("document-select").options].some((option) => option.value === previous)) $("document-select").value = previous;
}

async function initialize() {
  try {
    const data = await request("/bootstrap", { bootstrap: true });
    if (!cleanString(data.csrf_token)) throw new RequestError("INVALID_BOOTSTRAP");
    state.token = data.csrf_token;
    state.bootstrap = data;
    state.bootstrap.worker_ready = data.worker_ready && data.ready !== false;
    state.documents = list(data.knowledge_base?.documents);
    const knowledge = data.knowledge_base || {};
    const generation = cleanString(knowledge.generation_id);
    text("generation-id", generation ? generation.slice(0, 12) : "尚未发布");
    $("generation-id").title = generation;
    const count = state.documents.length;
    text("knowledge-summary", `${count} 份已发布文档 · ${Number(knowledge.chunks) || 0} 个文本片段`);
    list(knowledge.categories).forEach((category) => {
      const option = element("option", "", category);
      option.value = category;
      $("category-select").append(option);
    });
    populateDocuments();
    $("connection-status").replaceChildren(element("i"), document.createTextNode(state.bootstrap.worker_ready ? "服务就绪" : "问答依赖未就绪"));
    $("connection-status").classList.add(state.bootstrap.worker_ready ? "ready" : "error");
    text("service-status", data.service ? "自动化后台已关联（只读）" : "问答后台独立运行");
    if (data.ready === false && list(data.errors).length) {
      banner(describeError(new RequestError(data.errors[0], 503)), "error");
    } else if (data.settings?.synthetic || knowledge.synthetic) {
      banner("当前为演示模式：使用合成模型，仅验证流程；回答不代表真实语义质量已验收。");
    } else if (!data.worker_ready) banner("后台问答工作进程尚未就绪，暂不能提交问题。", "error");
    setBusy(false);
    await refreshHistory();
  } catch (error) {
    text("knowledge-summary", "知识库读取失败");
    $("connection-status").replaceChildren(element("i"), document.createTextNode("连接失败"));
    $("connection-status").classList.add("error");
    banner(describeError(error), "error");
  }
}

async function submitQuestion(event) {
  event?.preventDefault();
  if (state.busy || !state.bootstrap?.worker_ready) return;
  const question = $("question-input").value.trim();
  const topics = $("topics-input").value.split(/\r?\n/).map((value) => value.trim()).filter(Boolean);
  if (!question) { $("question-input").focus(); return; }
  if (question.length > 4000 || topics.length > 12 || topics.some((topic) => topic.length > 2000)) {
    toast("问题最多 4000 字；要点最多 12 个，每个最多 2000 字。");
    return;
  }
  setBusy(true);
  closeSource(false);
  show("empty-state", false);
  show("answer-panel", false);
  showProgress({ state: "queued", stage: "queued" });
  try {
    const job = await request("/questions", {
      method: "POST",
      body: { question, ...selectedScope(), topics: topics.length ? topics : null, output_format: $("format-select").value },
    });
    state.activeJob = job.job_id;
    state.pollFailures = 0;
    state.requestSequence += 1;
    text("job-detail", "已固定本次检索版本，等待后台处理");
    await refreshHistory();
    startPolling(job.job_id, state.requestSequence);
  } catch (error) {
    show("job-progress", false);
    setBusy(false);
    banner(describeError(error), "error");
    show("empty-state", !state.currentAnswer);
  }
}

function showProgress(job) {
  show("job-progress");
  const stage = cleanString(job.stage) || job.state;
  text("job-stage", stages[stage] || (job.state === "queued" ? stages.queued : stages.running));
  text("job-detail", job.state === "queued" ? "任务已接收，请等待工作进程处理" : "只展示处理阶段，最终答案将在检查后发布");
  const cancelling = stage === "cancelling" || Boolean(job.cancel_requested);
  $("job-progress").classList.toggle("cancelling", cancelling);
  $("cancel-button").disabled = cancelling || finalStates.has(job.state);
  text("cancel-button", cancelling ? "取消申请中" : "取消任务");
}

function startPolling(jobId, sequence) {
  clearTimeout(state.pollTimer);
  state.pollTimer = setTimeout(() => pollJob(jobId, sequence), 650);
}

async function pollJob(jobId, sequence) {
  if (sequence !== state.requestSequence) return;
  try {
    const job = await request(`/jobs/${encodeURIComponent(jobId)}`);
    if (sequence !== state.requestSequence) return;
    state.pollFailures = 0;
    if (finalStates.has(job.state)) {
      show("job-progress", false);
      setBusy(false);
      state.activeJob = null;
      if (job.answer) renderAnswer(job.answer);
      else if (job.state === "succeeded" && job.answer_id) renderAnswer(await request(`/queries/${encodeURIComponent(job.answer_id)}`));
      else {
        state.currentAnswer = null;
        show("answer-panel", false);
        show("empty-state");
        banner(job.state === "cancelled" ? "任务已取消，未发布未经检查的答案。" : describeError(new RequestError(job.error_code || "JOB_FAILED", 500)), job.state === "cancelled" ? "warning" : "error");
      }
      await refreshHistory();
      return;
    }
    showProgress(job);
    state.pollTimer = setTimeout(() => pollJob(jobId, sequence), 1000);
  } catch (error) {
    if (sequence !== state.requestSequence) return;
    state.pollFailures += 1;
    if (error.status === 401 || error.status === 403 || error.status === 404 || state.pollFailures >= 5) {
      show("job-progress", false);
      setBusy(false);
      state.activeJob = null;
      banner(`${describeError(error)} 任务可能仍在后台运行，可刷新问答记录查看。`, "error");
      return;
    }
    text("job-detail", "连接暂时中断，正在重新读取任务状态…");
    state.pollTimer = setTimeout(() => pollJob(jobId, sequence), Math.min(1000 * state.pollFailures, 4000));
  }
}

async function cancelJob() {
  if (!state.activeJob || $("cancel-button").disabled) return;
  $("cancel-button").disabled = true;
  try {
    const job = await request(`/jobs/${encodeURIComponent(state.activeJob)}/cancel`, { method: "POST", body: {} });
    showProgress({ ...job, stage: job.state === "cancelled" ? "cancelled" : "cancelling" });
    toast(job.state === "cancelled" ? "任务已取消" : "已提交取消申请；当前模型请求结束后停止发布");
    clearTimeout(state.pollTimer);
    await pollJob(state.activeJob, state.requestSequence);
  } catch (error) { $("cancel-button").disabled = false; toast(describeError(error)); }
}

async function refreshHistory() {
  if (!state.token) return;
  $("refresh-history").disabled = true;
  try {
    const data = await request("/jobs");
    const jobs = list(data.jobs);
    clear("history-list");
    if (!jobs.length) $("history-list").append(element("li", "subtle", "还没有问答记录"));
    jobs.forEach((job) => {
      const li = element("li");
      const button = element("button", "history-button");
      button.type = "button";
      button.dataset.jobId = job.job_id;
      button.title = cleanString(job.question);
      if (job.job_id === state.activeJob || job.answer_id === state.currentAnswer?.answer_id) button.classList.add("active");
      button.append(element("span", "history-question", job.question || "文档问题"));
      const meta = element("span", "history-meta");
      meta.append(element("span", "", statusLabels[job.answer_status || job.state] || "任务记录"));
      const timestamp = job.created_at ? new Date(job.created_at) : null;
      if (timestamp && !Number.isNaN(timestamp.getTime())) meta.append(element("time", "", timestamp.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" })));
      button.append(meta);
      button.addEventListener("click", () => openHistory(job));
      li.append(button);
      $("history-list").append(li);
    });
  } catch (error) { toast(describeError(error)); }
  finally { $("refresh-history").disabled = false; }
}

async function openHistory(summary) {
  if (state.busy && summary.job_id !== state.activeJob) {
    toast("请先等待当前任务结束或取消，再切换记录。");
    return;
  }
  try {
    const sequence = ++state.requestSequence;
    clearTimeout(state.pollTimer);
    const job = await request(`/jobs/${encodeURIComponent(summary.job_id)}`);
    if (sequence !== state.requestSequence) return;
    closeSource(false);
    if (job.answer) renderAnswer(job.answer);
    else if (job.state === "succeeded" && job.answer_id) renderAnswer(await request(`/queries/${encodeURIComponent(job.answer_id)}`));
    else if (!finalStates.has(job.state)) {
      state.activeJob = summary.job_id;
      state.pollFailures = 0;
      setBusy(true);
      show("empty-state", false);
      show("answer-panel", false);
      showProgress(job);
      startPolling(summary.job_id, sequence);
    } else {
      state.currentAnswer = null;
      show("answer-panel", false);
      show("empty-state");
      banner(job.state === "cancelled" ? "该任务已取消，没有已发布答案。" : describeError(new RequestError(job.error_code || "JOB_FAILED", 500)), job.state === "cancelled" ? "warning" : "error");
    }
    document.querySelectorAll(".history-button").forEach((button) => button.classList.toggle("active", button.dataset.jobId === summary.job_id));
  } catch (error) { banner(describeError(error), "error"); }
}

function statusClass(status) {
  if (["partial", "insufficient_evidence"].includes(status)) return "warning";
  if (["failed", "conflicting_evidence"].includes(status)) return "error";
  return status === "cancelled" ? "neutral" : "";
}

function citationButton(citation, className = "citation-chip", label = null) {
  const button = element("button", className, label || `[${citation.citation_id}] ${citation.source_name || "来源"}`);
  button.type = "button";
  button.dataset.citationId = citation.citation_id;
  button.setAttribute("aria-controls", "source-drawer");
  button.setAttribute("aria-expanded", "false");
  button.addEventListener("click", () => openSource(citation, button));
  return button;
}

function claimSources(claim, citations) {
  const wrapper = element("div", "claim-citations");
  citations.filter((citation) => list(citation.claim_ids).includes(claim.claim_id)).forEach((citation) => wrapper.append(citationButton(citation, "citation-chip", citation.citation_id)));
  return wrapper;
}

function renderClaims(answer) {
  clear("claims-list");
  const claims = list(answer.claims);
  const citations = list(answer.citations);
  if (!claims.length) {
    $("claims-list").append(element("p", "no-content", "没有可发布的支持性结论。请查看处理说明，或调整问题与检索范围。"));
    return;
  }
  if (answer.requested_format === "table") {
    const wrapper = element("div", "claims-table-wrap");
    const table = element("table", "claims-table");
    const caption = element("caption", "sr-only", "经过来源检查的结构化回答");
    const head = element("thead");
    const headRow = element("tr");
    ["序号", "来源支持的内容", "性质 / 引用"].forEach((value) => { const th = element("th", "", value); th.scope = "col"; headRow.append(th); });
    head.append(headRow);
    const body = element("tbody");
    claims.forEach((claim, index) => {
      const row = element("tr");
      row.append(element("td", "", String(index + 1)), element("td", "", claim.text));
      const sourceCell = element("td");
      sourceCell.append(element("span", "claim-kind", kindLabel(claim.kind)), claimSources(claim, citations));
      row.append(sourceCell);
      body.append(row);
    });
    table.append(caption, head, body);
    wrapper.append(table);
    $("claims-list").append(wrapper);
    return;
  }
  claims.forEach((claim) => {
    const item = element("section", "claim-item");
    if (claim.kind !== "fact") item.append(element("span", "claim-kind", kindLabel(claim.kind)));
    item.append(element("p", "claim-text", claim.text), claimSources(claim, citations));
    $("claims-list").append(item);
  });
}

function kindLabel(kind) { return { fact: "有据事实", quotation: "逐字引文", inference: "基于证据的归纳" }[kind] || "来源支持的内容"; }

function fillList(id, items, format = (item) => item) {
  clear(id);
  items.forEach((item) => $(id).append(element("li", "", format(item))));
}

function renderAnswer(answer) {
  state.currentAnswer = answer;
  state.activeJob = null;
  setBusy(false);
  show("job-progress", false);
  show("empty-state", false);
  show("answer-panel");
  closeSource(false);
  if (state.bootstrap?.settings?.synthetic || state.bootstrap?.knowledge_base?.synthetic) banner("当前为演示模式：使用合成模型，仅验证流程；回答不代表真实语义质量已验收。");
  else banner();
  text("answer-question", answer.question);
  text("answer-status", statusLabels[answer.status] || "已返回结果");
  $("answer-status").className = `badge ${statusClass(answer.status)}`;
  clear("verification-strip");
  const labels = [];
  if (answer.synthetic) labels.push(["demo", "合成演示 · 未经真实语义验收"]);
  else if (answer.verified && answer.verification === "semantic_checked") labels.push(["", "已执行语义支持检查"]);
  else labels.push(["demo", "未通过真实语义支持检查"]);
  if (answer.degraded) labels.push(["demo", "存在检索降级"]);
  if (answer.production_ready && !answer.synthetic && answer.verified) labels.push(["", "本次结果满足系统发布检查"]);
  labels.forEach(([type, value]) => $("verification-strip").append(element("span", `verification-pill ${type}`, value)));
  text("answer-facts", `检索 ${Number(answer.queries_used) || 0} 次 · ${Number(answer.rounds_used) || 0} 轮 · ${list(answer.claims).length} 条支持内容`);
  document.querySelectorAll(".answer-status-description").forEach((node) => node.remove());
  if (statusDescriptions[answer.status]) $("answer-facts").after(element("p", "answer-status-description", statusDescriptions[answer.status]));
  renderClaims(answer);
  const conflicts = list(answer.conflicts);
  show("conflicts-section", conflicts.length > 0);
  fillList("conflicts-list", conflicts, (item) => item.description || "存在来源冲突，请核对相关引文。");
  const coverage = list(answer.coverage);
  clear("coverage-list");
  show("coverage-section", coverage.length > 0);
  coverage.forEach((item, index) => {
    const li = element("li", "coverage-item");
    const label = { supported: "已有支持", partial: "部分支持", missing: "缺少证据", conflicting: "存在冲突" }[item.state] || "待核对";
    li.append(element("span", `coverage-state ${item.state}`, label), element("span", "", `${answer.topic_texts?.[item.topic_id] || `要点 ${index + 1}`} ${item.reason ? ` · ${item.reason}` : ""}`));
    $("coverage-list").append(li);
  });
  const unanswered = list(answer.unanswered_topics);
  show("unanswered-section", unanswered.length > 0);
  fillList("unanswered-list", unanswered, (item) => answer.topic_texts?.[item] || item);
  const warnings = list(answer.warnings).map((item) => item.startsWith("SYNTHETIC_DEMO") ? "演示摘录仅用于验证流程，尚未进行真实模型语义验收。" : item);
  if (answer.stop_reason && !["complete", "topics_covered", "coverage_complete", "answered"].includes(answer.stop_reason)) warnings.push(stopReason(answer.stop_reason));
  show("warnings-section", warnings.length > 0);
  fillList("warnings-list", [...new Set(warnings)]);
  clear("citations-list");
  const citations = list(answer.citations);
  text("citations-count", `${citations.length} 处引用`);
  if (!citations.length) $("citations-list").append(element("p", "subtle field-help", "本次没有已发布引用。"));
  citations.forEach((citation) => {
    const card = citationButton(citation, "citation-card", "");
    card.replaceChildren();
    const title = element("span", "citation-card-title");
    title.append(element("b", "", citation.citation_id), element("span", "", citation.source_name || "来源文档"));
    card.append(title, element("span", "citation-card-meta", `${origins[citation.origin] || "来源内容"} · ${locatorSummary(citation.locators) || "查看定位"}`), element("span", "citation-preview", citation.quote));
    $("citations-list").append(card);
  });
  text("answer-version", `版本 ${(answer.generation_id || "").slice(0, 12)} · 答案 ${(answer.answer_id || "").slice(0, 10)}`);
  renderGraph(answer);
}

function stopReason(reason) {
  return {
    no_new_evidence: "停止原因：补查未获得新证据。", evidence_sufficient: "停止原因：已有证据覆盖问题要点。",
    no_eligible_documents: "停止原因：所选范围没有可检索文档。", no_candidates: "停止原因：未找到可用候选。",
    max_rounds: "停止原因：达到补查轮次上限。", max_queries: "停止原因：达到查询次数上限。",
    deadline: "停止原因：达到处理时限。", model_budget: "停止原因：达到模型调用预算。",
    conflicting_evidence: "停止原因：发现需要保留的来源冲突。", failed: "停止原因：处理故障。",
  }[reason] || `处理停止标识：${reason}`;
}

function locatorSummary(locators) {
  return list(locators).map((locator) => locator.display_label || (locator.locator_type === "pptx_shape" ? `幻灯片 ${locator.slide}` : locator.page ? `物理页 ${locator.page}` : "文档对象")).join("；");
}

function resourceUrl(value) {
  if (typeof value !== "string" || !value.startsWith("/api/v1/queries/")) return null;
  try {
    const url = new URL(value, location.origin);
    if (url.origin !== location.origin || !url.pathname.startsWith("/api/v1/queries/")) return null;
    return url.pathname + url.search;
  } catch { return null; }
}

function closeSource(restoreFocus = true) {
  state.sourceSequence += 1;
  show("source-drawer", false);
  document.querySelectorAll("[data-citation-id]").forEach((button) => button.setAttribute("aria-expanded", "false"));
  if (restoreFocus && state.lastSourceButton?.isConnected) state.lastSourceButton.focus();
}

async function openSource(citation, trigger) {
  if (!state.currentAnswer) return;
  const sequence = ++state.sourceSequence;
  state.lastSourceButton = trigger;
  show("source-drawer");
  text("source-title", `${citation.citation_id} · 原文核对`);
  text("source-meta", "正在读取并校验引用来源…");
  text("source-quote", citation.quote);
  clear("source-locators"); clear("source-links"); clear("source-images");
  document.querySelectorAll("[data-citation-id]").forEach((button) => button.setAttribute("aria-expanded", String(button.dataset.citationId === citation.citation_id)));
  $("close-source").focus({ preventScroll: true });
  try {
    const answerId = state.currentAnswer.answer_id;
    const data = await request(`/queries/${encodeURIComponent(answerId)}/citations/${encodeURIComponent(citation.citation_id)}`);
    if (sequence !== state.sourceSequence) return;
    const source = data.citation || data;
    const resources = source.resources || data.resources || citation.resources || {};
    text("source-meta", `${source.source_name || citation.source_name || "来源文档"}\n${origins[source.origin || citation.origin] || "来源内容"} · 固定版本 ${(source.generation_id || state.currentAnswer.generation_id || "").slice(0, 12)}`);
    text("source-quote", source.quote ?? citation.quote);
    list(source.locators || citation.locators).forEach((locator) => {
      const p = element("p", "", locator.display_label || locatorSummary([locator]));
      if (locator.locator_type === "docx_object") p.append(document.createTextNode("（Word 原生对象，无固定页码）"));
      else if (locator.locator_type === "pdf_page") p.append(document.createTextNode(`（PDF 物理页 ${locator.page}）`));
      else if (locator.locator_type === "pptx_shape") p.append(document.createTextNode(`（幻灯片 ${locator.slide}）`));
      $("source-locators").append(p);
    });
    [["markdown", "下载 Markdown 原文"], ["snapshot", "下载原始文档快照"]].forEach(([key, label]) => {
      const url = resourceUrl(resources[key]);
      if (!url) return;
      const anchor = element("a", "", label);
      anchor.href = url;
      anchor.download = "";
      $("source-links").append(anchor);
    });
    list(resources.images).forEach((imageResource, index) => {
      const url = resourceUrl(typeof imageResource === "string" ? imageResource : imageResource?.url);
      if (!url) return;
      const figure = element("figure");
      const image = element("img");
      image.src = url;
      image.alt = `引用 ${citation.citation_id} 的来源图片 ${index + 1}`;
      image.loading = "lazy";
      image.addEventListener("error", () => { image.replaceWith(element("p", "field-help", "图片未能加载或完整性检查未通过，请检查后台来源资源。")); }, { once: true });
      const anchor = element("a");
      anchor.href = url;
      anchor.target = "_blank";
      anchor.rel = "noopener noreferrer";
      anchor.append(image);
      figure.append(anchor, element("figcaption", "", `来源图片 ${index + 1} · 点击放大预览`));
      $("source-images").append(figure);
    });
  } catch (error) {
    if (sequence !== state.sourceSequence) return;
    text("source-meta", describeError(error));
    text("source-quote", "本次引用回源检查未成功，请先解决来源读取问题，再核对原文。");
  }
}

const svgNS = "http://www.w3.org/2000/svg";
function svgElement(tag, attrs = {}, value) {
  const node = document.createElementNS(svgNS, tag);
  Object.entries(attrs).forEach(([key, attribute]) => node.setAttribute(key, String(attribute)));
  if (value !== undefined) node.textContent = String(value);
  return node;
}

function wrapLabel(label, length = 13) {
  const chars = [...cleanString(label).replace(/\s+/g, " ")];
  const lines = [];
  for (let index = 0; index < Math.min(chars.length, length * 3); index += length) lines.push(chars.slice(index, index + length).join(""));
  if (chars.length > length * 3) lines[lines.length - 1] = `${lines.at(-1).slice(0, -1)}…`;
  return lines.length ? lines : ["节点"];
}

function renderGraph(answer) {
  clear("graph-canvas"); clear("graph-relations");
  state.graphScale = 1;
  const edges = list(answer.diagram_edges).slice(0, 50);
  show("graph-section", edges.length > 0);
  if (!edges.length) {
    if (answer.requested_format === "mermaid" && list(answer.claims).length) {
      $("claims-list").prepend(element("p", "field-help", "没有通过检查的确定关系可供绘图，保留文字回答与来源。"));
    }
    return;
  }
  const nodes = [...new Set(edges.flatMap((edge) => [edge.source_label, edge.target_label]))];
  const columns = nodes.length > 3 ? 3 : Math.max(nodes.length, 1);
  const rows = Math.ceil(nodes.length / columns);
  const width = Math.max(columns * 240 + 30, 330);
  const height = Math.max(rows * 145 + 65, 205);
  state.graphSize = { width, height };
  const svg = svgElement("svg", { xmlns: svgNS, viewBox: `0 0 ${width} ${height}`, width, height, role: "img", "aria-labelledby": "docqa-graph-title docqa-graph-desc" });
  svg.append(svgElement("title", { id: "docqa-graph-title" }, "依据来源整理的文档关系图"), svgElement("desc", { id: "docqa-graph-desc" }, "箭头仅用于确定方向的关系，虚线表示方向未知。下方提供关系文字与来源入口。"));
  const defs = svgElement("defs");
  const marker = svgElement("marker", { id: "docqa-arrow", markerWidth: 10, markerHeight: 8, refX: 9, refY: 4, orient: "auto", markerUnits: "userSpaceOnUse" });
  marker.append(svgElement("path", { d: "M 0 0 L 9 4 L 0 8 Z", fill: "#568b95" }));
  defs.append(marker); svg.append(defs);
  const positions = new Map();
  nodes.forEach((label, index) => positions.set(label, { x: 140 + (index % columns) * 240, y: 95 + Math.floor(index / columns) * 145 }));
  edges.forEach((edge) => {
    const start = positions.get(edge.source_label);
    const end = positions.get(edge.target_label);
    const directed = edge.direction === "directed";
    const attrs = { fill: "none", stroke: "#79a0a8", "stroke-width": 1.8 };
    if (directed) attrs["marker-end"] = "url(#docqa-arrow)";
    if (edge.direction === "unknown") attrs["stroke-dasharray"] = "5 5";
    let path;
    if (start === end) {
      path = svgElement("path", { ...attrs, d: `M ${start.x - 28} ${start.y - 39} C ${start.x - 90} ${start.y - 110},${start.x + 90} ${start.y - 110},${start.x + 28} ${start.y - 39}` });
    } else {
      const dx = end.x - start.x;
      const dy = end.y - start.y;
      const boundScale = Math.max(Math.abs(dx) / 91, Math.abs(dy) / 42);
      const ex = end.x - dx / boundScale;
      const ey = end.y - dy / boundScale;
      const sx = start.x + dx / boundScale;
      const sy = start.y + dy / boundScale;
      path = svgElement("path", { ...attrs, d: `M ${sx} ${sy} L ${ex} ${ey}` });
    }
    path.append(svgElement("title", {}, relationText(edge)));
    svg.append(path);
    const li = element("li", "", relationText(edge));
    const related = list(answer.citations).filter((citation) => list(edge.evidence_ids).includes(citation.evidence_id) || list(citation.claim_ids).some((id) => list(edge.claim_ids).includes(id)));
    related.forEach((citation) => li.append(citationButton(citation, "citation-chip", citation.citation_id)));
    $("graph-relations").append(li);
  });
  nodes.forEach((label) => {
    const position = positions.get(label);
    const group = svgElement("g");
    group.append(svgElement("rect", { x: position.x - 90, y: position.y - 39, width: 180, height: 78, rx: 11, fill: "#ffffff", stroke: "#b4d4d3", "stroke-width": 1.3 }));
    const lines = wrapLabel(label);
    const labelNode = svgElement("text", { x: position.x, y: position.y - (lines.length - 1) * 9 + 4, "text-anchor": "middle", fill: "#254b5e", "font-family": "Segoe UI, Microsoft YaHei, sans-serif", "font-size": 12 });
    lines.forEach((line, index) => labelNode.append(svgElement("tspan", { x: position.x, dy: index ? 18 : 0 }, line)));
    group.append(labelNode, svgElement("title", {}, label));
    svg.append(group);
  });
  $("graph-canvas").append(svg);
  setGraphScale(1);
}

function relationText(edge) {
  const symbol = { directed: " → ", undirected: " — ", unknown: " ⋯ " }[edge.direction] || " ⋯ ";
  return `${edge.source_label}${symbol}${edge.target_label}${edge.direction === "unknown" ? "（方向未知）" : edge.direction === "undirected" ? "（无方向关系）" : ""}`;
}

function setGraphScale(value) {
  state.graphScale = Math.max(0.4, Math.min(2.5, value));
  const svg = $("graph-canvas").querySelector("svg");
  if (!svg || !state.graphSize) return;
  svg.setAttribute("width", String(Math.round(state.graphSize.width * state.graphScale)));
  svg.setAttribute("height", String(Math.round(state.graphSize.height * state.graphScale)));
  text("graph-reset", `${Math.round(state.graphScale * 100)}%`);
}

function downloadGraph() {
  const svg = $("graph-canvas").querySelector("svg");
  if (!svg) return;
  const copy = svg.cloneNode(true);
  copy.setAttribute("width", String(state.graphSize.width));
  copy.setAttribute("height", String(state.graphSize.height));
  const blob = new Blob([new XMLSerializer().serializeToString(copy)], { type: "image/svg+xml;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = element("a");
  anchor.href = url; anchor.download = "DocQA-来源关系图.svg";
  document.body.append(anchor); anchor.click(); anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}

async function copyAnswer() {
  if (!state.currentAnswer) return;
  const answer = state.currentAnswer;
  const paragraphs = [answer.question, statusLabels[answer.status] || answer.status];
  if (answer.synthetic) paragraphs.push("合成演示：未经真实语义验收。");
  else paragraphs.push(answer.verified && answer.verification === "semantic_checked" ? "已执行语义支持检查。" : "未经真实语义支持检查。");
  if (answer.degraded) paragraphs.push("本次回答存在检索降级。");
  list(answer.claims).forEach((claim) => {
    const refs = list(answer.citations).filter((citation) => list(citation.claim_ids).includes(claim.claim_id)).map((citation) => `[${citation.citation_id}]`).join(" ");
    paragraphs.push(`${claim.text} ${refs}`.trim());
  });
  list(answer.conflicts).forEach((conflict) => paragraphs.push(`来源冲突：${conflict.description}`));
  list(answer.unanswered_topics).forEach((topic) => paragraphs.push(`待解决：${topic}`));
  list(answer.citations).forEach((citation) => paragraphs.push(`[${citation.citation_id}] ${citation.source_name} · ${locatorSummary(citation.locators)}\n${citation.quote}`));
  try { await navigator.clipboard.writeText(paragraphs.join("\n\n")); toast("已复制回答及来源摘要"); }
  catch { toast("浏览器未允许复制，请手动选择回答文字复制。"); }
}

$("question-form").addEventListener("submit", submitQuestion);
$("question-input").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey) && !event.isComposing) { event.preventDefault(); submitQuestion(); }
});
$("category-select").addEventListener("change", populateDocuments);
$("reset-filters").addEventListener("click", () => { $("category-select").value = ""; $("document-select").value = ""; populateDocuments(); });
$("refresh-history").addEventListener("click", refreshHistory);
$("cancel-button").addEventListener("click", cancelJob);
$("close-source").addEventListener("click", () => closeSource());
$("graph-zoom-in").addEventListener("click", () => setGraphScale(state.graphScale + 0.2));
$("graph-zoom-out").addEventListener("click", () => setGraphScale(state.graphScale - 0.2));
$("graph-reset").addEventListener("click", () => setGraphScale(1));
$("graph-download").addEventListener("click", downloadGraph);
$("copy-answer").addEventListener("click", copyAnswer);
document.addEventListener("keydown", (event) => { if (event.key === "Escape" && !$("source-drawer").hidden) closeSource(); });
window.addEventListener("pagehide", () => clearTimeout(state.pollTimer));
initialize();
