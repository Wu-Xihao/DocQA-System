// Knowledge-base dashboard: KPI tiles and single-series bar lists from /stats.
// Bars carry one hue (identity is the row label), values are printed as text.
import { $, el, clear, text, errorText } from "./dom.js";
import { api } from "./api.js";

const names = {
  text: "正文",
  table: "表格",
  figure: "图片",
  native: "原生正文",
  ocr: "OCR 转录",
  vl_generated: "图片释义",
  complete: "完整入库",
  native_only: "仅原生正文",
  validated_parts: "仅通过校验部分",
  answered: "已回答",
  partial: "部分回答",
  insufficient_evidence: "证据不足",
  conflicting_evidence: "来源冲突",
  failed: "失败",
  succeeded: "成功",
  cancelled: "已取消",
  queued: "排队中",
  running: "运行中",
  retrieval: "文档检索",
  chat: "正常聊天",
  ready: "已就绪",
  pending: "待处理",
  parsing: "解析中",
  indexed: "已入库",
};
const label = (key) => names[key] || key;
const seconds = (value) =>
  value === null || value === undefined
    ? "—"
    : value < 1
      ? Math.round(value * 1000) + " ms"
      : value.toFixed(1) + " s";
const millis = (value) =>
  value === null || value === undefined ? "—" : Math.round(value) + " ms";

function tile(title, value, note = "") {
  const node = el("div", "stat-tile");
  node.append(el("span", "stat-label", title), el("strong", "stat-value", value));
  if (note) node.append(el("span", "stat-note", note));
  return node;
}

function bars(title, data, { unit = "", total } = {}) {
  const section = el("section", "stat-card");
  section.append(el("h3", "", title));
  const entries = Object.entries(data || {}).filter(([, v]) => v > 0);
  if (!entries.length) {
    section.append(el("p", "subtle", "暂无数据"));
    return section;
  }
  const max = Math.max(...entries.map(([, v]) => v));
  const sum = total ?? entries.reduce((a, [, v]) => a + v, 0);
  const list = el("ul", "bar-list");
  for (const [key, value] of entries) {
    const row = el("li", "bar-row");
    const share = sum ? Math.round((value / sum) * 100) : 0;
    row.title = `${label(key)}：${value}${unit}（${share}%）`;
    const track = el("span", "bar-track");
    const fill = el("span", "bar-fill");
    fill.style.width = Math.max(2, (value / max) * 100) + "%";
    track.append(fill);
    row.append(
      el("span", "bar-name", label(key)),
      track,
      el("span", "bar-value", `${value}${unit}`),
    );
    list.append(row);
  }
  section.append(list);
  return section;
}

function columns(title, data, format = (v) => v) {
  const section = el("section", "stat-card wide");
  section.append(el("h3", "", title));
  const entries = Object.entries(data || {});
  if (!entries.length) {
    section.append(el("p", "subtle", "暂无数据"));
    return section;
  }
  const max = Math.max(1, ...entries.map(([, v]) => v));
  const chart = el("div", "column-chart");
  for (const [key, value] of entries) {
    const column = el("div", "column");
    column.title = `${key}：${format(value)}`;
    const fill = el("span", "column-fill");
    fill.style.height = Math.max(3, (value / max) * 100) + "%";
    column.append(fill, el("span", "column-label", key));
    chart.append(column);
  }
  section.append(chart);
  return section;
}

function render(data) {
  const root = $("stats-content");
  clear("stats-content");
  const kb = data.knowledge_base;
  const qa = data.questions;
  const search = data.search;
  const tiles = el("div", "stat-tiles");
  tiles.append(
    tile("文档", kb ? String(kb.documents) : "—", kb ? `版本 ${kb.generation_id.slice(0, 8)}` : ""),
    tile("检索分块", kb ? String(kb.chunks) : "—", kb ? `已索引图片 ${kb.figures_indexed}` : ""),
    tile(
      "检索路线",
      kb ? String(kb.recall_routes.length) : "—",
      kb ? kb.recall_routes.join(" + ") + (kb.synthetic ? " · 演示" : "") : "",
    ),
    tile("问答任务", qa ? String(qa.total) : "—", qa ? `端到端 P95 ${seconds(qa.latency_p95)}` : ""),
    tile(
      "完整回答率",
      qa && qa.answered_rate !== null ? Math.round(qa.answered_rate * 100) + "%" : "—",
      "文档检索模式",
    ),
    tile(
      "片段检索",
      search ? millis(search.p50_ms) : "—",
      search ? `P95 ${millis(search.p95_ms)} · ${search.count} 次` : "",
    ),
  );
  root.append(tiles);
  const grid = el("div", "stat-grid");
  if (kb)
    grid.append(
      bars("文档格式", kb.formats, { unit: " 份" }),
      bars("资料分类", kb.categories, { unit: " 份" }),
      bars("入库方式", kb.index_modes, { unit: " 份" }),
      bars("分块内容类型", kb.content_types, { unit: " 块", total: kb.chunks }),
      bars("分块内容来源", kb.origins, { unit: " 块", total: kb.chunks }),
    );
  if (data.service)
    grid.append(bars("后台解析状态", data.service.states, { unit: " 份" }));
  if (qa)
    grid.append(
      bars("回答状态", qa.answer_status, { unit: " 次" }),
      bars("对话模式", qa.modes, { unit: " 次" }),
      bars("主要错误码", qa.top_errors, { unit: " 次" }),
      columns("近 14 天提问量", qa.daily, (v) => v + " 次"),
    );
  if (search?.recent_ms?.length >= 3)
    grid.append(
      columns(
        "最近片段检索耗时",
        Object.fromEntries(search.recent_ms.map((v, i) => [String(i + 1), v])),
        (v) => v + " ms",
      ),
    );
  root.append(grid);
  const footer = el(
    "p",
    "subtle stat-footnote",
    `统计来自本机索引、Service 与任务记录，不含问题和资料正文；检索耗时为本次 Web 进程内记录。更新于 ${new Date(
      data.generated_at * 1000,
    ).toLocaleString()}`,
  );
  if (data.errors?.length) footer.textContent += ` · 部分数据不可用：${data.errors.join("，")}`;
  root.append(footer);
}

async function load() {
  text("stats-content", "正在统计…");
  try {
    render(await api("/stats", { timeoutMs: 30000 }));
  } catch (error) {
    text("stats-content", "统计失败：" + errorText(error));
  }
}

export function bindStats() {
  $("open-stats").addEventListener("click", () => {
    $("stats-dialog").showModal();
    load();
  });
  $("refresh-stats").addEventListener("click", load);
  $("close-stats").addEventListener("click", () => $("stats-dialog").close());
}
