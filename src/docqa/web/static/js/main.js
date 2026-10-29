import {
  $,
  el,
  button,
  show,
  text,
  clear,
  recallLabel,
  statusNames,
  stageNames,
  errorText,
  notice,
  toast,
  locationText,
  sourceWarning,
  richText,
  copy,
  download,
} from "./dom.js";
import { api, setToken, plain, uploadBytes, watchConversation } from "./api.js";
import { openSourceReader, sourcePending } from "./source-reader.js";
import { icon, mountIcons } from "./icons.js";
import {
  bindMaintenance,
  showImageAnalysis,
  retryImageAnalysis,
} from "./maintenance.js";
import {
  artifactTypes,
  answerContent,
  renderArtifact,
  messageHeader,
  refs,
} from "./renderers.js";

const finalStates = new Set(["succeeded", "failed", "cancelled"]);
const state = {
  types: null,
  conversation: null,
  documents: [],
  libraryDocuments: [],
  documentSequence: 0,
  uploads: [],
  selected: new Set(),
  scopeMode: "all",
  category: "",
  messages: [],
  activeAnswer: null,
  history: [],
  historyCursor: null,
  archived: false,
  messageCursor: null,
  epoch: 0,
  stream: null,
  poll: null,
  latestGeneration: null,
  images: [],
  imageCursor: null,
  imageIndex: 0,
  imageScale: 1,
  preview: null,
  localUploads: new Map(),
  artifactJobs: new Map(),
  pendingRelation: false,
  capabilities: null,
  drafts: new Map(),
  submitting: false,
  requests: new Map(),
};
const safe =
  (fn) =>
  (...args) => {
    try {
      return Promise.resolve(fn(...args)).catch((e) =>
        notice(errorText(e), true),
      );
    } catch (e) {
      notice(errorText(e), true);
    }
  };
const id = () => crypto.randomUUID().replaceAll("-", "");
function prefs() {
  try {
    const value = JSON.parse(localStorage.getItem("docqa-ui-v1") || "{}");
    return value && typeof value === "object" && !Array.isArray(value)
      ? value
      : {};
  } catch {
    return {};
  }
}
function savePrefs(value) {
  try {
    localStorage.setItem(
      "docqa-ui-v1",
      JSON.stringify({ ...prefs(), ...value }),
    );
  } catch {
    /* UI preferences are optional. */
  }
}
function chooseTab(name) {
  for (const b of document.querySelectorAll("[data-tab]")) {
    b.classList.toggle("active", b.dataset.tab === name);
    b.setAttribute("aria-selected", String(b.dataset.tab === name));
  }
  for (const tab of ["sources", "studio", "images"])
    show("tab-" + tab, tab === name);
  $("workspace").classList.remove("right-hidden");
  $("workspace").classList.add("right-open");
  if (innerWidth < 960) $("workspace").classList.remove("left-open");
  syncPanels();
}
function syncPanels() {
  for (const panel of ["left", "right"]) {
    const drawer = innerWidth < (panel === "left" ? 960 : 1280);
    const expanded = drawer
      ? $("workspace").classList.contains(panel + "-open")
      : !$("workspace").classList.contains(panel + "-hidden");
    $("toggle-" + panel).setAttribute("aria-expanded", String(expanded));
  }
}
function togglePanel(panel) {
  const workspace = $("workspace");
  const drawer = innerWidth < (panel === "left" ? 960 : 1280);
  if (drawer) {
    workspace.classList.remove(panel + "-hidden");
    workspace.classList.toggle(panel + "-open");
    if (innerWidth < 960)
      workspace.classList.remove(
        (panel === "left" ? "right" : "left") + "-open",
      );
  } else {
    workspace.classList.remove(panel + "-open");
    workspace.classList.toggle(panel + "-hidden");
    savePrefs({
      [panel + "Hidden"]: workspace.classList.contains(panel + "-hidden"),
    });
  }
  syncPanels();
}
function activeJob() {
  return state.messages.find((m) => m.job && !finalStates.has(m.job.state))
    ?.job;
}
function effectiveDocuments() {
  return state.documents.filter(
    (d) =>
      (!state.category || d.category === state.category) &&
      (state.types === null ||
        state.types.includes(d.source_name.split(".").at(-1).toLowerCase())) &&
      (state.scopeMode === "all" || state.selected.has(d.doc_id)),
  );
}
function updateScope() {
  const selected = effectiveDocuments(),
    gaps = selected.filter((d) =>
      ["native_only", "validated_parts"].includes(d.quality),
    ).length;
  const summary = `${state.types === null ? "全部类型" : state.types.map((x) => x.toUpperCase()).join(" + ") || "未选类型"} · ${selected.length} 份资料${gaps ? ` · ${gaps} 份有覆盖缺口` : ""}`;
  text("scope-summary", summary);
  text("composer-scope", `${selected.length} 份资料`);
  text(
    "scope-count",
    state.scopeMode === "all" ? "全部" : String(selected.length),
  );
  text(
    "scope-help",
    selected.length
      ? "只查找选中资料；运行中调整只影响下一轮。"
      : "当前未选择资料，发送前请至少勾选一份。",
  );
  $("ask-button").disabled =
    !selected.length || !!activeJob() || state.submitting;
  const generation = state.conversation?.generation_id;
  text("generation-id", generation ? generation.slice(0, 8) : "");
  show(
    "upgrade-version",
    !!generation &&
      !!state.latestGeneration &&
      generation !== state.latestGeneration,
  );
  const changed =
    !!generation &&
    !!state.latestGeneration &&
    generation !== state.latestGeneration;
  show("library-version-note", changed);
  $("use-latest-library").disabled = !!activeJob() || state.submitting;
  $("use-latest-library").title = activeJob()
    ? "请先等待或取消当前回答"
    : "仅下一轮切换，历史回答保留原版来源";
}

function menu(items) {
  const details = el("details", "row-menu"),
    summary = el("summary", "", "⋯");
  summary.setAttribute("aria-label", "更多操作");
  const nav = el("nav");
  for (const [label, fn] of items)
    nav.append(
      button(
        label,
        () => {
          details.open = false;
          safe(fn)();
        },
        "",
      ),
    );
  details.append(summary, nav);
  return details;
}
function drawDocuments() {
  clear("documents-list");
  const query = $("document-search").value.trim().toLowerCase();
  const current = new Map(state.documents.map((d) => [d.doc_id, d]));
  const latest = new Set(state.libraryDocuments.map((d) => d.doc_id));
  const rows = [
    ...state.libraryDocuments,
    ...state.documents.filter((d) => !latest.has(d.doc_id)),
  ];
  for (const doc of rows.filter(
    (d) =>
      (!state.category || d.category === state.category) &&
      (state.types === null ||
        state.types.includes(d.source_name.split(".").at(-1).toLowerCase())) &&
      d.source_name.toLowerCase().includes(query),
  )) {
    const row = el("div", "document-row"),
      input = el("input");
    row.dataset.docId = doc.doc_id;
    const available = current.has(doc.doc_id);
    const updated =
      available && current.get(doc.doc_id).artifact_id !== doc.artifact_id;
    row.dataset.inConversation = String(available);
    input.type = "checkbox";
    input.disabled = !available;
    if (!available) input.title = "已入库；使用新资料版本后可选择";
    input.setAttribute("aria-label", "检索资料：" + doc.source_name);
    input.checked =
      available &&
      (state.scopeMode === "all" || state.selected.has(doc.doc_id));
    input.addEventListener("change", () => {
      if (state.scopeMode === "all")
        state.selected = new Set(state.documents.map((d) => d.doc_id));
      state.scopeMode = "selected";
      input.checked
        ? state.selected.add(doc.doc_id)
        : state.selected.delete(doc.doc_id);
      updateScope();
    });
    const info = el("div", "document-info");
    info.append(
      el("div", "document-name", doc.source_name),
      el(
        "small",
        "document-meta",
        !available
          ? "已入库 · 升级对话版本后可选"
          : !latest.has(doc.doc_id)
            ? "仅在当前对话旧版中可用"
            : updated
              ? "资料已更新 · 当前对话使用旧版"
              : ["native_only", "validated_parts"].includes(doc.quality)
                ? "正文可用 · 解析有缺口"
                : "已入库 · 可用于提问",
      ),
    );
    const actions = [["查看原文与 Markdown", () => previewDocument(doc)]];
    info.querySelector(".document-name").classList.add("document-open");
    info.querySelector(".document-name").tabIndex = 0;
    info.querySelector(".document-name").setAttribute("role", "button");
    info
      .querySelector(".document-name")
      .addEventListener("click", () => safe(previewDocument)(doc));
    info.querySelector(".document-name").addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        safe(previewDocument)(doc);
      }
    });
    info.querySelector(".document-name").title = doc.source_name;
    if (latest.has(doc.doc_id) && state.capabilities?.uploads_enabled)
      actions.push(
        ["图片解析状态", () => showImageAnalysis(doc.doc_id)],
        ["重新解析图片", () => retryImageAnalysis(doc.doc_id)],
        ["重新解析", () => reprocess(doc.doc_id)],
        ["移出知识库", () => removeDocument(doc)],
      );
    row.append(
      input,
      el(
        "span",
        `file-icon ${doc.source_name.split(".").at(-1).toLowerCase()}`,
        doc.source_name.split(".").at(-1).toUpperCase().slice(0, 4),
      ),
      info,
      menu(actions),
    );
    $("documents-list").append(row);
  }
  if (!$("documents-list").children.length)
    $("documents-list").append(
      el(
        "p",
        "subtle",
        rows.length ? "没有匹配的资料。" : "还没有可用资料，先上传一份文档。",
      ),
    );
  text("document-count", state.libraryDocuments.length);
  $("document-count").title =
    `最新资料库 ${state.libraryDocuments.length} 份；当前对话范围 ${state.documents.length} 份`;
  updateScope();
}
const uploadLabels = {
  selected: "等待上传",
  uploading: "正在上传",
  received: "上传完成",
  committed: "准备入库",
  queued: "等待解析",
  parsing: "正在解析资料",
  indexing: "正在构建知识库",
  ready: "已入库",
  ready_with_gaps: "正文已入库 · 有解析缺口",
  failed: "处理失败",
  review_required: "需复核，尚未入库",
  upload_failed: "上传失败",
  cancelled: "已取消",
};
function drawUploads() {
  clear("uploads-list");
  const query = $("document-search").value.trim().toLowerCase();
  const hiddenByFilter = (u) => {
    const doc = state.libraryDocuments.find((d) => d.doc_id === u.doc_id);
    return (
      doc &&
      ((state.category && state.category !== doc.category) ||
        (state.types !== null &&
          !state.types.includes(
            doc.source_name.split(".").at(-1).toLowerCase(),
          )) ||
        !doc.source_name.toLowerCase().includes(query))
    );
  };
  const records = new Map(
    state.uploads
      .filter(
        (u) =>
          u.state !== "cancelled" &&
          (!["ready", "ready_with_gaps"].includes(u.state) ||
            hiddenByFilter(u)),
      )
      .map((u) => [u.id, u]),
  );
  for (const [k, v] of state.localUploads) records.set(k, v);
  for (const u of [...records.values()].slice(0, 12)) {
    const row = el("div", "upload-row");
    row.dataset.uploadId = u.id;
    const filteredReady =
      ["ready", "ready_with_gaps"].includes(u.state) && hiddenByFilter(u);
    row.append(
      el("strong", "", u.name),
      el(
        "small",
        "",
        u.error_code
          ? errorText({ code: u.error_code })
          : (uploadLabels[u.state] || u.state) +
              (filteredReady ? "（被当前筛选隐藏）" : ""),
      ),
    );
    if (filteredReady)
      row.append(
        button("显示此资料", () => {
          $("document-search").value = "";
          $("category-select").value = "";
          state.category = "";
          state.types = null;
          $("types-all").checked = true;
          document.querySelectorAll("[data-doc-type]").forEach((b) => {
            b.checked = true;
          });
          drawDocuments();
          updateScope();
          drawUploads();
          document
            .querySelector(`[data-doc-id="${u.doc_id}"]`)
            ?.scrollIntoView({ block: "nearest" });
        }),
      );
    if (u.state === "uploading") {
      const progress = el("progress");
      progress.max = u.size;
      progress.value = u.received || 0;
      row.append(progress);
    }
    if (u.doc_id && u.state === "failed")
      row.append(button("重新处理", () => safe(reprocess)(u.doc_id)));
    if (["selected", "received", "upload_failed"].includes(u.state)) {
      if (u.state === "received")
        row.append(
          button("继续入库", () =>
            safe(async () => {
              await api(`/uploads/${u.id}/commit`, {
                method: "POST",
                body: {},
              });
              await loadDocuments();
            })(),
          ),
        );
      row.append(
        button("取消记录", () =>
          safe(async () => {
            await api(`/uploads/${u.id}`, { method: "DELETE" });
            state.localUploads.delete(u.id);
            await loadDocuments();
          })(),
        ),
      );
    }
    $("uploads-list").append(row);
  }
}
async function loadDocuments() {
  const epoch = state.epoch;
  const sequence = ++state.documentSequence;
  const path =
    "/documents" +
    (state.conversation ? "?conversation_id=" + state.conversation.id : "");
  const data = await api(path);
  if (epoch !== state.epoch || sequence !== state.documentSequence) return;
  state.documents = data.documents;
  state.libraryDocuments = data.library_documents ?? data.documents;
  state.uploads = data.uploads || [];
  for (const upload of state.uploads) {
    const local = state.localUploads.get(upload.id);
    if (
      local &&
      local.state !== "uploading" &&
      !["selected", "uploading"].includes(upload.state)
    )
      state.localUploads.delete(upload.id);
  }
  state.latestGeneration = data.latest_generation_id ?? data.generation_id;
  const old = state.category;
  clear("category-select");
  $("category-select").append(el("option", "", "全部分类"));
  $("category-select").firstChild.value = "";
  for (const category of [
    ...new Set(
      [...state.libraryDocuments, ...state.documents].map((d) => d.category),
    ),
  ]) {
    const o = el("option", "", category);
    o.value = category;
    $("category-select").append(o);
  }
  if ([...$("category-select").options].some((o) => o.value === old))
    $("category-select").value = old;
  else state.category = "";
  drawDocuments();
  drawUploads();
  text("library-refresh-status", "");
  text(
    "knowledge-summary",
    `资料库 ${state.libraryDocuments.length} 份 · 本轮 ${state.documents.length} 份 · ${recallLabel(data.recall_routes)}`,
  );
}

function historyGroup(timestamp, pinned) {
  if (pinned) return "置顶";
  const now = new Date(),
    d = new Date(timestamp * 1000);
  const day = new Date(
      now.getFullYear(),
      now.getMonth(),
      now.getDate(),
    ).getTime(),
    t = new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime(),
    delta = Math.round((day - t) / 86400000);
  return delta === 0
    ? "今天"
    : delta === 1
      ? "昨天"
      : delta < 7
        ? "近 7 天"
        : "更早";
}
async function refreshHistory(append = false) {
  const query = $("history-search").value;
  const data = await api(
    `/conversations?q=${encodeURIComponent(query)}&archived=${state.archived}&cursor=${append ? state.historyCursor || 0 : 0}`,
  );
  state.history = append ? [...state.history, ...data.items] : data.items;
  state.historyCursor = data.next_cursor;
  clear("history-list");
  let group;
  for (const c of state.history) {
    const name = historyGroup(c.updated, c.pinned);
    if (name !== group) {
      $("history-list").append(el("div", "history-group", name));
      group = name;
    }
    const row = el(
      "div",
      "history-row" + (c.id === state.conversation?.id ? " active" : ""),
    );
    row.append(
      button(c.title, () => safe(openConversation)(c.id), "history-open"),
      deleteControl(
        `删除聊天：${c.title}`,
        () => removeConversation(c),
        "history-delete",
      ),
      menu([
        ["重命名", () => rename(c)],
        [c.pinned ? "取消置顶" : "置顶", () => patch(c, { pinned: !c.pinned })],
        [
          c.archived ? "取消归档" : "归档",
          () => patch(c, { archived: !c.archived }),
        ],
      ]),
    );
    $("history-list").append(row);
  }
  if (!state.history.length)
    $("history-list").append(el("p", "subtle", "还没有聊天记录。"));
  show("more-history", data.next_cursor !== null);
}
async function patch(c, changes) {
  await api("/conversations/" + c.id, {
    method: "PATCH",
    body: { ...changes, version: c.version },
  });
  if (c.id === state.conversation?.id) {
    state.conversation = await api("/conversations/" + c.id);
    text("conversation-title", state.conversation.title);
  }
  await refreshHistory();
}
async function rename(c) {
  if (!c) return;
  const title = prompt("对话名称", c.title);
  if (title?.trim()) await patch(c, { title: title.trim().slice(0, 100) });
}
async function removeConversation(c) {
  if (
    !confirm(
      "永久删除这条聊天及其全部问答、成果和导出文件？此操作不可恢复，资料库原始文档保留。",
    )
  )
    return;
  const result = await api("/conversations/" + c.id, { method: "DELETE" });
  if (state.conversation?.id === c.id) {
    stopWatching();
    $("question-input").value = "";
    await newChat();
    closeDeletedPreview();
  }
  state.drafts.delete(c.id);
  await refreshHistory();
  await finishDeletion(result);
}
function deleteControl(label, action, className = "") {
  const control = button(
    "删除",
    async () => {
      control.disabled = true;
      try {
        await safe(action)();
      } finally {
        control.disabled = false;
      }
    },
    "delete-control " + className,
  );
  control.setAttribute("aria-label", label);
  control.title = label;
  return control;
}
function closeDeletedPreview() {
  if ($("preview-dialog").open) $("preview-dialog").close();
  state.preview = null;
  clear("preview-content");
  clear("preview-sources");
  clear("preview-actions");
}
async function finishDeletion(result) {
  if (result.state !== "complete")
    toast("已移除记录，正在停止相关任务并清理后端文件…");
  for (let i = 0; result.state !== "complete" && i < 40; i++) {
    await new Promise((resolve) => setTimeout(resolve, 350));
    result = await api("/deletions/" + result.id);
  }
  toast(
    result.state === "complete"
      ? "已永久删除，后端相关内容已清理"
      : "记录已移除，后端仍在清理，将自动重试；文件占用解除后即可完成。",
  );
}
async function removeArtifact(artifact) {
  if (
    !confirm(
      `永久删除“${artifact.title}”及其后端内容、导出文件？此操作不可恢复，原聊天和其他成果版本保留。`,
    )
  )
    return;
  const result = await api(`/artifacts/${artifact.id}`, { method: "DELETE" });
  if (state.preview?.id === artifact.id) closeDeletedPreview();
  await loadArtifacts();
  await finishDeletion(result);
}
function stopWatching() {
  state.stream?.abort();
  state.stream = null;
  clearTimeout(state.poll);
}
async function newChat() {
  state.drafts.set(state.conversation?.id || "new", $("question-input").value);
  stopWatching();
  state.epoch++;
  state.conversation = null;
  state.messages = [];
  state.activeAnswer = null;
  state.pendingRelation = false;
  clear("messages");
  clear("citations-list");
  clear("source-detail");
  clear("artifacts-list");
  clear("images-list");
  text("conversation-title", "新的研究，从这里开始");
  text("sources-count", "0");
  show("sources-empty");
  show("empty-state");
  $("question-input").value = state.drafts.get("new") || "";
  state.scopeMode = "all";
  state.selected.clear();
  await loadDocuments();
  await refreshHistory();
  $("question-input").focus();
}
async function openConversation(identity) {
  state.drafts.set(state.conversation?.id || "new", $("question-input").value);
  stopWatching();
  const epoch = ++state.epoch;
  const c = await api("/conversations/" + identity);
  if (epoch !== state.epoch) return;
  state.conversation = c;
  state.messages = [];
  state.activeAnswer = null;
  state.pendingRelation = false;
  clear("citations-list");
  clear("source-detail");
  clear("images-list");
  text("sources-count", "0");
  show("sources-empty");
  $("question-input").value = state.drafts.get(identity) || "";
  state.scopeMode = "all";
  state.selected.clear();
  state.images = [];
  text("conversation-title", c.title);
  $("workspace").classList.remove("left-open");
  await Promise.all([loadDocuments(), loadMessages(), loadArtifacts()]);
  await refreshHistory();
}
async function loadMessages(older = false) {
  if (!state.conversation) return;
  const epoch = state.epoch,
    convo = state.conversation.id;
  const data = await api(
    `/conversations/${convo}/messages?cursor=${older ? state.messageCursor || 0 : 0}`,
  );
  if (epoch !== state.epoch) return;
  state.messages = older ? [...data.items, ...state.messages] : data.items;
  state.messageCursor = data.next_cursor;
  show("more-messages", data.next_cursor !== null);
  drawMessages(older);
  const answers = state.messages.filter((m) => m.job?.answer);
  const current = answers.find(
    (m) => m.job.answer.answer_id === state.activeAnswer?.answer_id,
  );
  if (current) state.activeAnswer = current.job.answer;
  else if (answers.length) focusAnswer(answers.at(-1).job.answer);
  updateScope();
  if (activeJob()) startWatching();
  const pending = state.pendingRelation;
  if (pending?.conversation === convo) {
    const completed = state.messages.find(
      (m) => m.job?.job_id === pending.job,
    )?.job;
    if (completed && finalStates.has(completed.state)) {
      state.pendingRelation = false;
      if (completed.answer?.diagram_edges?.length)
        safe(generateArtifact)("relation", completed.answer);
      else toast("本轮没有得到可核验的关系，未补造关系图。");
    }
  }
}
function drawMessages(keepScroll = false) {
  const scroll = $("chat-scroll"),
    atBottom =
      scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 130;
  clear("messages");
  show("empty-state", !state.messages.length);
  for (const m of state.messages) {
    const row = el(
      "article",
      "message " + (m.role === "user" ? "user-message" : "assistant-message"),
    );
    row.dataset.messageId = m.id;
    if (m.role === "user") row.append(el("div", "user-bubble", m.content));
    else {
      const job = m.job;
      if (!job) continue;
      row.dataset.jobId = job.job_id;
      if (job.answer) {
        const answer = job.answer;
        row.append(
          messageHeader(answer),
          answerContent(answer, (ids) => {
            focusAnswer(answer);
            if (ids[0]) safe(openSource)(answer, ids[0]);
          }),
        );
        const actions = el("div", "message-actions");
        actions.append(
          button("查看来源", () => {
            focusAnswer(answer);
            chooseTab("sources");
          }),
          button("相关图片", () => {
            focusAnswer(answer);
            chooseTab("images");
            safe(loadImages)();
          }),
          button("生成成果", () => {
            focusAnswer(answer);
            chooseTab("studio");
          }),
          button("复制", () =>
            copy((answer.claims || []).map((c) => c.text).join("\n\n")),
          ),
          button("重新生成", () => safe(retry)(m)),
        );
        ["source", "image", "report", "copy", "refresh"].forEach((name, i) =>
          actions.children[i].prepend(icon(name, 15)),
        );
        row.append(actions);
        row.addEventListener("click", (e) => {
          if (e.target === row) focusAnswer(answer);
        });
      } else if (job.streaming_answer) {
        row.append(messageHeader(job.streaming_answer));
        const content = el("div", "stream-answer-content");
        content.append(
          answerContent(job.streaming_answer, (ids) => {
            if (ids[0]) safe(openSource)(job.streaming_answer, ids[0]);
          }),
        );
        row.append(
          content,
          el("small", "stream-label", "正在流式呈现已核验内容…"),
        );
      } else if (!finalStates.has(job.state)) {
        const head = el("div", "assistant-head");
        head.append(icon("source", 22), el("strong", "", "DocQA"));
        row.append(head, progressBox(job));
      } else {
        row.append(
          el(
            "p",
            "answer-note error",
            job.state === "cancelled"
              ? "任务已取消。"
              : errorText({ code: job.error_code || "WEB_WORKER_RESTARTED" }),
          ),
          button("重试", () => safe(retry)(m)),
        );
      }
    }
    $("messages").append(row);
  }
  if (!keepScroll && (atBottom || state.messages.length < 4))
    scroll.scrollTop = scroll.scrollHeight;
}
function progressBox(job) {
  const root = el("div", "progress-box");
  root.append(el("span", "spinner"));
  const content = el("div");
  content.append(
    el("div", "progress-title", stageNames[job.stage] || "正在处理"),
    el("div", "progress-meta", elapsed(job.created_at)),
  );
  root.append(
    content,
    button("取消", () =>
      safe(async () => {
        await api(`/jobs/${job.job_id}/cancel`, { method: "POST", body: {} });
        toast("已申请取消，当前步骤结束后停止发布。");
      })(),
    ),
  );
  return root;
}
function elapsed(created) {
  return `已用时 ${Math.max(0, Math.floor(Date.now() / 1000 - created))} 秒 · 回答检查通过后展示`;
}
function retrievalPreview(job) {
  const details = el("details", "retrieval-preview");
  details.open = true;
  details.append(el("summary", "", "已找到相关原文 · 正在组织并校验回答"));
  for (const item of job.retrieval_preview || []) {
    const row = el("div", "retrieval-preview-item");
    row.append(el("strong", "", item.source_name), el("p", "", item.text));
    details.append(row);
  }
  details.append(
    el("small", "subtle", "以下为检索原文预览，尚不是经过校验的最终回答。"),
  );
  return details;
}
function updateJobs(jobs) {
  for (const job of jobs) {
    const message = state.messages.find((m) => m.job?.job_id === job.job_id);
    if (!message) continue;
    const was = message.job.state;
    message.job = { ...message.job, ...job };
    const row = document.querySelector(`[data-job-id="${job.job_id}"]`);
    if (row && row.querySelector(".progress-title")) {
      row.querySelector(".progress-title").textContent =
        stageNames[job.stage] || statusNames[job.state] || job.state;
      row.querySelector(".progress-meta").textContent = elapsed(job.created_at);
      const previous = row.querySelector(".retrieval-preview");
      if (job.retrieval_preview?.length) {
        const preview = retrievalPreview(job);
        if (previous) previous.replaceWith(preview);
        else row.append(preview);
      }
    }
    if (finalStates.has(job.state) && !finalStates.has(was)) {
      safe(async () => {
        await loadMessages();
        await refreshHistory();
      })();
    }
  }
  updateScope();
  if (!activeJob()) stopWatching();
}
function startWatching() {
  if (state.stream || !state.conversation) return;
  const conversation = state.conversation.id,
    epoch = state.epoch,
    control = new AbortController();
  state.stream = control;
  watchConversation(
    conversation,
    (data) => {
      if (epoch !== state.epoch) return;
      if (data.kind) receiveAnswerStream(data);
      else updateJobs(data.jobs);
    },
    control.signal,
  ).catch(() => {
    if (!control.signal.aborted && epoch === state.epoch) {
      state.stream = null;
      state.poll = setTimeout(
        safe(async () => {
          await loadMessages();
          if (activeJob()) startWatching();
        }),
        1800,
      );
    }
  });
}
function receiveAnswerStream(data) {
  const id = data.job_id || data.job?.job_id;
  const message = state.messages.find((m) => m.job?.job_id === id);
  if (!message) return;
  if (data.kind === "answer_start") {
    message.job.streaming_answer = data.answer;
    drawMessages(true);
  } else if (data.kind === "answer_delta" && message.job.streaming_answer) {
    const answer = message.job.streaming_answer;
    if (!answer.claims.some((c) => c.claim_id === data.claim.claim_id))
      answer.claims.push(data.claim);
    const target = document.querySelector(
      `[data-job-id="${id}"] .stream-answer-content`,
    );
    target?.replaceChildren(
      answerContent(answer, (ids) => {
        if (ids[0]) safe(openSource)(answer, ids[0]);
      }),
    );
  } else if (data.kind === "answer_done") {
    message.job = data.job;
    drawMessages(true);
    focusAnswer(data.job.answer);
    updateScope();
    stopWatching();
    safe(refreshHistory)();
  }
}
async function submitQuestion(event, override, relation = false) {
  event?.preventDefault();
  const question = override?.question || $("question-input").value.trim();
  if (!question || activeJob() || state.submitting) return;
  const selected = effectiveDocuments();
  if (!selected.length) {
    toast("请至少选择一份已入库资料。");
    return;
  }
  const epoch = state.epoch;
  state.submitting = true;
  updateScope();
  try {
    notice("");
    if (!state.conversation) {
      const created = await api("/conversations", {
        method: "POST",
        body: {},
      });
      if (epoch !== state.epoch) return;
      state.conversation = created;
      text("conversation-title", "新对话");
    }
    const payload = {
      question,
      client_request_id: id(),
      scope_mode: state.scopeMode,
      categories: state.category ? [state.category] : null,
      doc_types: state.types,
      doc_ids:
        state.scopeMode === "selected" ? selected.map((d) => d.doc_id) : null,
      output_format: $("format-select").value,
      ...override,
    };
    delete payload.scope;
    const conversationId = state.conversation.id;
    const fingerprint = JSON.stringify({ ...payload, client_request_id: null });
    const previous = state.requests.get(conversationId);
    if (previous?.fingerprint === fingerprint)
      payload.client_request_id = previous.key;
    else
      state.requests.set(conversationId, {
        fingerprint,
        key: payload.client_request_id,
      });
    const job = await api(`/conversations/${state.conversation.id}/messages`, {
      method: "POST",
      body: payload,
    });
    state.requests.delete(conversationId);
    if (epoch !== state.epoch) return job;
    if (relation)
      state.pendingRelation = {
        conversation: state.conversation.id,
        job: job.job_id,
      };
    const updated = await api("/conversations/" + state.conversation.id);
    if (epoch !== state.epoch) return job;
    state.conversation = updated;
    text("conversation-title", state.conversation.title);
    $("question-input").value = "";
    state.drafts.delete(state.conversation.id);
    state.drafts.delete("new");
    $("question-input").style.height = "auto";
    await loadMessages();
    await refreshHistory();
    $("chat-scroll").scrollTop = $("chat-scroll").scrollHeight;
    return job;
  } finally {
    state.submitting = false;
    updateScope();
  }
}
async function retry(m) {
  if (activeJob()) {
    toast("请先等待或取消当前任务。");
    return;
  }
  await api(`/messages/${m.id}/retry`, { method: "POST", body: {} });
  await loadMessages();
}

function focusAnswer(answer) {
  const changed = state.activeAnswer?.answer_id !== answer.answer_id;
  state.activeAnswer = answer;
  text("sources-count", answer.citations.length);
  show("sources-empty", !answer.citations.length);
  clear("citations-list");
  for (const c of answer.citations) {
    const card = button(
      "",
      () => safe(openSource)(answer, c.citation_id),
      "citation-card",
    );
    const title = el("div", "citation-title");
    title.append(el("b", "", c.citation_id), el("span", "", c.source_name));
    card.append(
      title,
      el("span", "citation-location", locationText(c.locators)),
      el("p", "citation-preview", c.quote),
    );
    $("citations-list").append(card);
  }
  text("studio-context", `基于：${answer.question.slice(0, 65)}`);
  if (changed) {
    clear("source-detail");
    state.images = [];
    state.imageCursor = null;
    clear("images-list");
  }
  for (const b of $("studio-actions").querySelectorAll("button"))
    b.disabled = !answer.claims?.length;
}
async function openSource(answer, citationId) {
  const sequence = (state.sourceSequence = (state.sourceSequence || 0) + 1);
  focusAnswer(answer);
  chooseTab("sources");
  const aid = answer.answer_id;
  const fail = sourcePending(
    answer.citations.find((c) => c.citation_id === citationId)?.source_name ||
      "原文",
  );
  let source;
  try {
    source = await api(
      `/queries/${aid}/citations/${citationId}?include_preview=true`,
    );
  } catch (error) {
    fail(error);
    throw error;
  }
  if (
    state.activeAnswer?.answer_id !== aid ||
    sequence !== state.sourceSequence
  )
    return;
  const root = el("section", "source-detail");
  root.append(
    el("h3", "", source.source_name),
    el("span", "subtle", locationText(source.locators)),
    el("blockquote", "", source.quote),
  );
  const links = el("div", "source-links");
  links.append(button("阅读原文与 Markdown", () => openSourceReader(source)));
  const a = el("a", "", "下载原稿");
  a.href = source.resources.snapshot;
  a.download = "";
  links.append(
    a,
    button("查看相关图", () => {
      chooseTab("images");
      safe(loadImages)(false, citationId);
    }),
  );
  root.append(links);
  $("source-detail").replaceChildren(root);
  root.scrollIntoView({ block: "nearest" });
  openSourceReader(source);
}
function openTextPreview(title, content) {
  text("preview-title", title);
  text("preview-type", "原文 Markdown");
  clear("preview-actions");
  clear("preview-sources");
  $("preview-content").replaceChildren(el("pre", "source-markdown", content));
  $("preview-dialog").showModal();
}
async function loadImages(append = false, citationId) {
  if (!state.activeAnswer) return;
  const aid = state.activeAnswer.answer_id;
  const query = new URLSearchParams({
    cursor: String(append ? state.imageCursor || 0 : 0),
  });
  if (citationId) query.set("citation_id", citationId);
  if (!append)
    $("images-list").replaceChildren(
      el("p", "subtle", "正在定位同版相关图片…"),
    );
  const result = await api(`/queries/${aid}/related-images?${query}`);
  if (state.activeAnswer?.answer_id !== aid) return;
  state.images = append ? [...state.images, ...result.items] : result.items;
  state.imageCursor = result.next_cursor;
  clear("images-list");
  state.images.forEach((image, index) => {
    const card = el("article", "image-card"),
      b = button("", () => openImage(index), ""),
      img = el("img");
    img.src = image.thumbnail_url;
    img.alt =
      image.caption ||
      `${image.source_name} · ${image.image_kind === "page_render" ? "页面预览" : "图片"}`;
    img.loading = "lazy";
    img.addEventListener("error", () => {
      img.alt = "预览不可用，请核对原稿。";
    });
    b.append(img);
    const info = el("div", "image-info");
    info.append(
      el(
        "span",
        "badge" + (image.role === "reference" ? " warning" : ""),
        image.role === "evidence" ? "直接引用图" : "参考图 · 未参与回答证据",
      ),
      el("small", "", image.source_name),
      el("small", "", locationText([image.locator])),
      button("定位原文", () =>
        safe(openSource)(state.activeAnswer, image.citation_id),
      ),
    );
    card.append(b, info);
    $("images-list").append(card);
  });
  if (!result.total)
    $("images-list").append(
      el(
        "p",
        "subtle",
        "没有可可靠关联的图片。原生 Word 没有固定页映射时，不会猜测图片所在页。",
      ),
    );
  show("more-images", result.next_cursor !== null);
}
function openImage(index) {
  if (!state.images.length) return;
  state.imageIndex = (index + state.images.length) % state.images.length;
  state.imageScale = 1;
  const image = state.images[state.imageIndex];
  $("lightbox-image").src = image.preview_url;
  $("lightbox-image").alt = image.caption || image.source_name;
  text("lightbox-title", image.source_name);
  text(
    "lightbox-caption",
    (image.role === "reference"
      ? "参考图，未参与回答证据 · "
      : "直接引用图 · ") + locationText([image.locator]),
  );
  resetImage();
  if (!$("image-dialog").open) $("image-dialog").showModal();
}
function resetImage() {
  const n = $("lightbox-image");
  n.style.maxWidth = "100%";
  n.style.maxHeight = "100%";
  n.style.width = "auto";
  state.imageScale = 1;
}
function zoomImage(delta) {
  state.imageScale = Math.min(4, Math.max(0.3, state.imageScale + delta));
  const n = $("lightbox-image");
  n.style.maxWidth = "none";
  n.style.maxHeight = "none";
  n.style.width = `${Math.max(200, (n.naturalWidth || 900) * state.imageScale)}px`;
}

async function loadArtifacts() {
  if (!state.conversation) {
    clear("artifacts-list");
    return;
  }
  const cid = state.conversation.id,
    data = await api(`/conversations/${cid}/artifacts`);
  if (state.conversation?.id !== cid) return;
  clear("artifacts-list");
  for (const a of data.items) {
    const card = el("article", "artifact-card"),
      b = button("", () => safe(openArtifact)(a.id), "artifact-open"),
      body = el("div");
    body.append(
      el("strong", "", a.title),
      el(
        "small",
        "",
        `${artifactTypes[a.kind].label} · V${a.version} · ${new Date(a.created_at * 1000).toLocaleDateString()}`,
      ),
    );
    const mark = el("span", "artifact-symbol");
    mark.append(icon(a.kind, 23));
    b.append(mark, body);
    const foot = el("div", "artifact-foot");
    foot.append(
      deleteControl(`删除成果：${a.title}`, () => removeArtifact(a)),
      button("重新整理", () =>
        safe(async () => {
          const job = await api(`/artifacts/${a.id}/regenerate`, {
            method: "POST",
            body: {},
          });
          await waitArtifact(job.id, cid);
        })(),
      ),
    );
    card.append(b, foot);
    $("artifacts-list").append(card);
  }
  if (!data.items.length)
    $("artifacts-list").append(
      el("p", "subtle", "还没有成果。选择一条回答，将它整理成更清晰的形式。"),
    );
}
async function generateArtifact(kind, answer = state.activeAnswer) {
  if (!answer || !state.conversation) {
    toast("先选择一条有支持内容的回答。");
    return;
  }
  chooseTab("studio");
  try {
    const job = await api("/studio/jobs", {
      method: "POST",
      body: {
        conversation_id: state.conversation.id,
        answer_id: answer.answer_id,
        kind,
      },
    });
    toast("正在根据已核验内容整理…");
    await waitArtifact(job.id, state.conversation.id);
  } catch (error) {
    if (error.code === "ARTIFACT_RELATIONS_MISSING") {
      if (state.conversation?.generation_id !== answer.generation_id) {
        toast("该回答属于旧资料版本。请在当前版本重新提问，再生成关系图。");
        return;
      }
      if (
        !confirm(
          "当前回答没有经过检查的关系。要基于同一范围重新检索并生成关系图回答吗？",
        )
      )
        return;
      const message = state.messages.find(
        (m) => m.job?.answer?.answer_id === answer.answer_id,
      );
      await submitQuestion(
        null,
        {
          question: (
            answer.question +
            "\n请根据原文中明确支持的关系生成关系图，未知方向不要推断。"
          ).slice(0, 3900),
          output_format: "mermaid",
          scope_mode: "selected",
          doc_ids: message.scope.doc_ids,
          categories: message.scope.categories,
          doc_types: message.scope.doc_types || null,
        },
        true,
      );
    } else throw error;
  }
}
async function waitArtifact(identity, conversation) {
  for (let i = 0; i < 90; i++) {
    const job = await api("/studio/jobs/" + identity);
    if (job.state === "ready") {
      if (state.conversation?.id === conversation) {
        await loadArtifacts();
        await openArtifact(job.artifact_id);
      }
      return;
    }
    if (job.state === "failed") {
      const error = new Error(job.error_code);
      error.code = job.error_code;
      throw error;
    }
    await new Promise((r) => setTimeout(r, 700));
  }
  toast("成果仍在后台整理，稍后刷新成果列表查看。");
}
async function openArtifact(identity) {
  const a = await api("/artifacts/" + identity);
  if (a.conversation_id !== state.conversation?.id) return;
  state.preview = a;
  text("preview-title", a.title);
  text(
    "preview-type",
    `${artifactTypes[a.kind].label} · V${a.version} · ${a.content.quality === "answered" ? "来源可查" : "来源覆盖有缺口"}`,
  );
  const select = (ids) => previewSources(a, ids);
  $("preview-content").replaceChildren(renderArtifact(a, select));
  previewSources(a, []);
  clear("preview-actions");
  const formats =
    a.kind === "table"
      ? ["csv", "md", "json"]
      : a.kind === "slides"
        ? ["pptx", "md", "json"]
        : ["relation", "mindmap"].includes(a.kind)
          ? ["svg", "png", "json"]
          : ["md", "docx", "json"];
  const picker = el("select");
  picker.setAttribute("aria-label", "导出格式");
  for (const f of formats) {
    const o = el("option", "", f.toUpperCase());
    o.value = f;
    picker.append(o);
  }
  $("preview-actions").append(
    picker,
    button("导出 ↗", () => safe(exportArtifact)(a, picker.value)),
  );
  if (!$("preview-dialog").open) $("preview-dialog").showModal();
}
function previewSources(a, ids) {
  clear("preview-sources");
  $("preview-sources").append(
    el("h3", "", "来源与依据"),
    el(
      "p",
      "field-help",
      ids.length
        ? "已定位所选内容的引用。"
        : "点击单元格、节点或引用，查看对应原文。",
    ),
  );
  for (const c of a.content.citations.filter(
    (c) => !ids.length || ids.includes(c.id),
  )) {
    const block = el("div", "source-block");
    block.append(
      el("strong", "", `[${c.id}] ${c.name}`),
      el("p", "field-help", c.location),
      el("blockquote", "", c.quote),
      button("打开完整来源", () =>
        safe(async () => {
          $("preview-dialog").close();
          const epoch = state.epoch;
          const message = state.messages.find(
            (m) => m.job?.answer?.answer_id === a.answer_id,
          );
          const answer =
            message?.job?.answer || (await api(`/queries/${a.answer_id}`));
          if (epoch === state.epoch) await openSource(answer, c.id);
        })(),
      ),
    );
    $("preview-sources").append(block);
  }
}
async function exportArtifact(artifact, format) {
  const job = await api(`/artifacts/${artifact.id}/exports`, {
    method: "POST",
    body: { format },
  });
  toast("正在生成导出文件…");
  for (let i = 0; i < 80; i++) {
    const result = await api("/exports/" + job.id);
    if (result.state === "ready") {
      download(
        `/api/v1/exports/${job.id}/content`,
        `DocQA-${artifact.kind}.${format}`,
      );
      return;
    }
    if (result.state === "failed") {
      const error = new Error(result.error_code);
      error.code = result.error_code;
      throw error;
    }
    await new Promise((r) => setTimeout(r, 600));
  }
  toast("导出仍在处理，请稍后重试。");
}

async function uploadFiles(files) {
  if (!state.capabilities?.uploads_enabled) {
    toast("当前未启用上传，请按启动说明配置 Service。");
    return;
  }
  if (state.uploadBatchActive) {
    toast("上一批文件仍在上传，请等待传输完成。");
    return;
  }
  const candidates = Array.from(files);
  const queue = candidates.filter(
    (f) => /\.(pdf|docx|pptx)$/i.test(f.name) && !f.name.startsWith("~$"),
  );
  if (queue.length > 1000) {
    toast("单批最多 1000 份文档，请拆分文件夹；本次尚未上传。");
    return;
  }
  if (!queue.length) {
    toast("未发现 DOCX、PPTX 或 PDF 文档。");
    return;
  }
  state.uploadBatchActive = true;
  const total = queue.length;
  let completed = 0,
    failed = 0,
    lastRefresh = 0;
  text(
    "folder-upload-status",
    `发现 ${total} 份文档，忽略 ${candidates.length - total} 个非支持文件。准备上传…`,
  );
  async function worker() {
    while (queue.length) {
      const file = queue.shift();
      if (!/\.(pdf|docx|pptx)$/i.test(file.name)) {
        toast(`${file.name}：不支持的格式`);
        continue;
      }
      if (file.size > state.capabilities.max_upload_bytes || !file.size) {
        toast(`${file.name}：文件为空或超过大小限制`);
        failed++;
        text(
          "folder-upload-status",
          `已处理 ${completed + failed}/${total} · 失败 ${failed}`,
        );
        continue;
      }
      let record;
      try {
        record = await api("/uploads", {
          method: "POST",
          body: {
            name: file.name,
            size: file.size,
            category: state.category || "auto",
            relative_path: file.webkitRelativePath || null,
          },
        });
        state.localUploads.set(record.id, record);
        drawUploads();
        window.dispatchEvent(new Event("docqa:library-dirty"));
        const received = await uploadBytes(record.id, file, (bytes) => {
          state.localUploads.set(record.id, {
            ...record,
            state: "uploading",
            received: bytes,
          });
          drawUploads();
        });
        if (
          received.duplicate_of &&
          !file.webkitRelativePath &&
          !confirm(`${file.name} 的内容已在资料库中。仍另存一份吗？`)
        ) {
          await api("/uploads/" + record.id, { method: "DELETE" });
          state.localUploads.delete(record.id);
          drawUploads();
          completed++;
          continue;
        }
        await api(`/uploads/${record.id}/commit`, { method: "POST", body: {} });
        state.localUploads.delete(record.id);
        if (performance.now() - lastRefresh > 1000) {
          lastRefresh = performance.now();
          await loadDocuments();
        }
        completed++;
      } catch (error) {
        if (record)
          state.localUploads.set(record.id, {
            ...record,
            state: "upload_failed",
            error_code: error.code || "UPLOAD_INCOMPLETE",
          });
        drawUploads();
        toast(errorText(error));
        failed++;
      }
      text(
        "folder-upload-status",
        `传输 ${completed + failed}/${total} · 成功 ${completed} · 失败 ${failed}；已传输文件按队列解析入库。`,
      );
    }
  }
  try {
    await Promise.all([worker(), worker()]);
    await loadDocuments();
  } finally {
    $("file-input").value = "";
    $("folder-input").value = "";
    state.uploadBatchActive = false;
  }
}
async function reprocess(doc) {
  if (!confirm("重新处理会重新调用所需解析/模型服务，是否继续？")) return;
  await api(`/documents/${doc}/reprocess`, { method: "POST", body: {} });
  await loadDocuments();
  toast("已加入重新处理队列");
  window.dispatchEvent(new Event("docqa:library-dirty"));
}
async function previewDocument(doc) {
  const query =
    state.conversation && state.documents.some((d) => d.doc_id === doc.doc_id)
      ? "?conversation_id=" + state.conversation.id
      : "";
  const base = `/api/v1/documents/${doc.doc_id}`;
  openSourceReader({
    source_name: doc.source_name,
    resources: {
      markdown: base + "/content" + query,
      images_gallery: base + "/images" + query,
      preview: base + "/preview" + query,
      snapshot: base + "/original/content" + query,
      original:
        base +
        "/original/content" +
        query +
        (query ? "&" : "?") +
        "inline=true",
    },
  });
}
async function removeDocument(doc) {
  if (
    !confirm(
      `将 ${doc.source_name} 移出最新知识库？历史回答保留原版来源，原始文件不删除。`,
    )
  )
    return;
  await api(`/documents/${doc.doc_id}`, { method: "DELETE" });
  window.dispatchEvent(new Event("docqa:library-dirty"));
  toast(
    "已提交移出请求。新版本发布后生效；旧对话继续使用原版，可新建对话查看。",
  );
}
function resizePanel(handle, variable, side, min, max) {
  let start;
  const currentWidth = () =>
    $(side === 1 ? "left-panel" : "right-panel").getBoundingClientRect()
      .width ||
    parseInt(
      getComputedStyle(document.documentElement).getPropertyValue(variable),
    );
  const maximum = () =>
    innerWidth >= 1280 &&
    !$("workspace").classList.contains("left-hidden") &&
    !$("workspace").classList.contains("right-hidden")
      ? Math.min(max, innerWidth * (side === 1 ? 0.24 : 0.3))
      : max;
  handle.addEventListener("pointerdown", (e) => {
    start = {
      x: e.clientX,
      width: currentWidth(),
    };
    handle.setPointerCapture(e.pointerId);
  });
  handle.addEventListener("pointermove", (e) => {
    if (start)
      document.documentElement.style.setProperty(
        variable,
        Math.min(
          maximum(),
          Math.max(min, start.width + (e.clientX - start.x) * side),
        ) + "px",
      );
  });
  handle.addEventListener("pointerup", () => {
    start = null;
    savePrefs({
      [variable]: getComputedStyle(document.documentElement).getPropertyValue(
        variable,
      ),
    });
  });
  handle.addEventListener("pointercancel", () => {
    start = null;
  });
  handle.addEventListener("keydown", (e) => {
    if (!["ArrowLeft", "ArrowRight"].includes(e.key)) return;
    e.preventDefault();
    const n = currentWidth();
    document.documentElement.style.setProperty(
      variable,
      Math.min(
        maximum(),
        Math.max(min, n + (e.key === "ArrowRight" ? 12 : -12) * side),
      ) + "px",
    );
    savePrefs({
      [variable]: getComputedStyle(document.documentElement).getPropertyValue(
        variable,
      ),
    });
  });
}

function bind() {
  mountIcons();
  bindMaintenance();
  $("folder-upload").addEventListener("click", () => $("folder-input").click());
  $("folder-input").addEventListener("change", () =>
    safe(uploadFiles)($("folder-input").files),
  );
  const typeBoxes = [...document.querySelectorAll("[data-doc-type]")];
  const applyTypes = () => {
    const chosen = typeBoxes
      .filter((b) => b.checked)
      .map((b) => b.dataset.docType);
    $("types-all").checked = chosen.length === 3;
    state.types = chosen.length === 3 ? null : chosen;
    drawDocuments();
    updateScope();
  };
  $("types-all").addEventListener("change", () => {
    typeBoxes.forEach((b) => {
      b.checked = $("types-all").checked;
    });
    applyTypes();
  });
  typeBoxes.forEach((b) => b.addEventListener("change", applyTypes));
  $("refresh-documents").addEventListener("click", (e) => {
    e.preventDefault();
    e.stopPropagation();
    safe(refreshLibrary)();
  });
  $("reset-layout").addEventListener("click", () => {
    for (const variable of ["--left", "--right"])
      document.documentElement.style.removeProperty(variable);
    savePrefs({ "--left": null, "--right": null });
  });
  for (const tab of document.querySelectorAll("[data-tab]"))
    tab.addEventListener("click", () => {
      chooseTab(tab.dataset.tab);
      if (
        tab.dataset.tab === "images" &&
        state.activeAnswer &&
        !state.images.length
      )
        safe(loadImages)();
    });
  $("new-chat").addEventListener("click", safe(newChat));
  $("rename-chat").addEventListener(
    "click",
    safe(() => rename(state.conversation)),
  );
  $("question-form").addEventListener("submit", safe(submitQuestion));
  $("question-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      safe(submitQuestion)();
    }
  });
  $("question-input").addEventListener("input", () => {
    $("question-input").style.height = "auto";
    $("question-input").style.height =
      Math.min(168, $("question-input").scrollHeight) + "px";
  });
  for (const b of document.querySelectorAll("[data-prompt]"))
    b.addEventListener("click", () => {
      $("question-input").value = b.dataset.prompt;
      $("question-input").focus();
    });
  $("category-select").addEventListener("change", () => {
    state.category = $("category-select").value;
    drawDocuments();
    drawUploads();
  });
  $("document-search").addEventListener("input", () => {
    drawDocuments();
    drawUploads();
  });
  $("select-all").addEventListener("click", () => {
    state.scopeMode = "all";
    state.selected.clear();
    drawDocuments();
  });
  $("select-none").addEventListener("click", () => {
    state.scopeMode = "selected";
    state.selected.clear();
    drawDocuments();
  });
  $("upgrade-version").addEventListener("click", safe(useLatestLibrary));
  $("use-latest-library").addEventListener("click", safe(useLatestLibrary));
  $("refresh-history").addEventListener("click", (e) => {
    e.preventDefault();
    safe(refreshHistory)();
  });
  let searchTimer;
  $("history-search").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(safe(refreshHistory), 250);
  });
  for (const [control, archived] of [
    ["active-history", false],
    ["archived-history", true],
  ])
    $(control).addEventListener("click", () => {
      state.archived = archived;
      $("active-history").classList.toggle("active", !archived);
      $("archived-history").classList.toggle("active", archived);
      safe(refreshHistory)();
    });
  $("more-history").addEventListener(
    "click",
    safe(() => refreshHistory(true)),
  );
  $("more-messages").addEventListener(
    "click",
    safe(() => loadMessages(true)),
  );
  $("attach-button").addEventListener("click", () => $("file-input").click());
  $("file-input").addEventListener("change", () =>
    safe(uploadFiles)($("file-input").files),
  );
  const dz = $("dropzone");
  dz.addEventListener("click", () => $("file-input").click());
  dz.addEventListener("keydown", (e) => {
    if (["Enter", " "].includes(e.key)) {
      e.preventDefault();
      $("file-input").click();
    }
  });
  for (const type of ["dragenter", "dragover"])
    dz.addEventListener(type, (e) => {
      e.preventDefault();
      dz.classList.add("dragover");
    });
  for (const type of ["dragleave", "drop"])
    dz.addEventListener(type, (e) => {
      e.preventDefault();
      dz.classList.remove("dragover");
    });
  dz.addEventListener("drop", (e) => safe(uploadFiles)(e.dataTransfer.files));
  document.addEventListener("dragover", (e) => e.preventDefault());
  document.addEventListener("drop", (e) => e.preventDefault());
  $("refresh-artifacts").addEventListener("click", safe(loadArtifacts));
  $("refresh-images").addEventListener(
    "click",
    safe(() => loadImages()),
  );
  $("more-images").addEventListener(
    "click",
    safe(() => loadImages(true)),
  );
  for (const [k, v] of Object.entries(artifactTypes)) {
    const b = button("", () => safe(generateArtifact)(k), "studio-action");
    const label = el("span");
    label.append(el("strong", "", v.label), el("small", "", v.hint));
    b.append(icon(k, 23), label);
    b.disabled = true;
    $("studio-actions").append(b);
  }
  for (const [trigger, panel] of [
    ["toggle-left", "left"],
    ["toggle-right", "right"],
    ["close-right", "right"],
  ])
    $(trigger).addEventListener("click", () => togglePanel(panel));
  $("theme-button").addEventListener("click", () => {
    const dark = document.documentElement.dataset.theme !== "dark";
    document.documentElement.dataset.theme = dark ? "dark" : "light";
    savePrefs({ theme: dark ? "dark" : "light" });
  });
  for (const name of ["documents", "scope", "history"])
    $(name + "-section").addEventListener("toggle", () =>
      savePrefs({ [name + "Open"]: $(name + "-section").open }),
    );
  $("close-preview").addEventListener("click", () =>
    $("preview-dialog").close(),
  );
  $("close-image").addEventListener("click", () => $("image-dialog").close());
  $("image-prev").addEventListener("click", () =>
    openImage(state.imageIndex - 1),
  );
  $("image-next").addEventListener("click", () =>
    openImage(state.imageIndex + 1),
  );
  $("image-reset").addEventListener("click", resetImage);
  $("image-zoom-in").addEventListener("click", () => zoomImage(0.25));
  $("image-zoom-out").addEventListener("click", () => zoomImage(-0.25));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      $("workspace").classList.remove("left-open", "right-open");
      syncPanels();
    }
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "n") {
      e.preventDefault();
      safe(newChat)();
    }
  });
  resizePanel($("resize-left"), "--left", 1, 264, 400);
  resizePanel($("resize-right"), "--right", -1, 340, 560);
  window.addEventListener("resize", syncPanels);
  syncPanels();
}

async function initialize() {
  const p = prefs();
  if (p.theme) document.documentElement.dataset.theme = p.theme;
  for (const variable of ["--left", "--right"])
    if (/^\d{3}px$/.test(p[variable] || ""))
      document.documentElement.style.setProperty(
        variable,
        Math.min(
          variable === "--left" ? 400 : 560,
          Math.max(variable === "--left" ? 264 : 340, parseInt(p[variable])),
        ) + "px",
      );
  if (p.leftHidden) $("workspace").classList.add("left-hidden");
  if (p.rightHidden) $("workspace").classList.add("right-hidden");
  for (const name of ["documents", "scope", "history"])
    if (p[name + "Open"] !== undefined)
      $(name + "-section").open = !!p[name + "Open"];
  bind();
  window.dispatchEvent(new CustomEvent("docqa:bound"));
  try {
    const bootstrap = await api("/bootstrap");
    setToken(bootstrap.csrf_token);
    state.latestGeneration = bootstrap.knowledge_base?.generation_id;
    text(
      "connection-status",
      bootstrap.worker_ready ? "● 本地服务已连接" : "● 服务准备中",
    );
    notice(
      bootstrap.settings.synthetic
        ? "演示模式：用于验证功能与交互，不代表真实模型质量。"
        : "",
    );
    await Promise.all([
      api("/workspace").then((value) => {
        state.capabilities = value;
      }),
      loadDocuments(),
      refreshHistory(),
    ]);
    if (state.history.length && !$("question-input").value.trim())
      await openConversation(state.history[0].id);
    if (!state.capabilities.uploads_enabled) {
      $("dropzone").setAttribute("aria-disabled", "true");
      $("dropzone").title = "独立查询模式，需配置 Service 索引以启用上传。";
    }
  } catch (error) {
    notice(errorText(error), true);
    text("connection-status", "连接异常");
  }
  setInterval(() => {
    const job = activeJob();
    if (job) {
      const row = document.querySelector(
        `[data-job-id="${job.job_id}"] .progress-meta`,
      );
      if (row) row.textContent = elapsed(job.created_at);
    }
  }, 1000);
  let stopped = false,
    timer,
    polling = false,
    refreshSoon = false;
  const poll = async () => {
    if (stopped || polling) return;
    polling = true;
    if (document.visibilityState !== "hidden" && state.capabilities)
      await refreshLibrary(false);
    polling = false;
    if (!stopped) {
      const busy =
        state.localUploads.size ||
        state.uploads.some((u) =>
          ["committed", "queued", "parsing", "indexing"].includes(u.state),
        );
      timer = setTimeout(poll, refreshSoon ? 1000 : busy ? 3000 : 15000);
      refreshSoon = false;
    }
  };
  timer = setTimeout(poll, 3000);
  window.addEventListener("docqa:library-dirty", () => {
    if (!stopped) {
      if (polling) {
        refreshSoon = true;
        return;
      }
      clearTimeout(timer);
      timer = setTimeout(poll, 1000);
    }
  });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "hidden" && state.capabilities)
      safe(refreshLibrary)(false);
  });
  window.addEventListener("pagehide", () => {
    stopped = true;
    clearTimeout(timer);
  });
  window.addEventListener("pagehide", stopWatching);
}
async function refreshLibrary(manual = true) {
  if (manual) text("library-refresh-status", "正在刷新资料库…");
  try {
    await loadDocuments();
  } catch (error) {
    text("library-refresh-status", "资料库刷新失败：" + errorText(error));
  }
}
async function useLatestLibrary() {
  if (!state.conversation) return;
  const identity = state.conversation.id,
    epoch = state.epoch;
  const updated = await api(`/conversations/${identity}/upgrade`, {
    method: "POST",
    body: {},
  });
  if (epoch !== state.epoch || state.conversation?.id !== identity) return;
  state.conversation = updated;
  state.scopeMode = "all";
  state.selected.clear();
  await loadDocuments();
  await refreshHistory();
  toast("下一轮将使用新的资料版本，历史回答保留原版来源。");
}
initialize().catch((error) => {
  notice("交互初始化失败：" + errorText(error) + "。请刷新页面重试。", true);
  text("connection-status", "初始化失败");
});
