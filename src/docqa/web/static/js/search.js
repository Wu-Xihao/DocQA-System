// Snippet search: shows ranked retrieval candidates with their source locations.
// These are candidates for reading, not verified answers or citations.
import { $, el, button, clear, text, toast, errorText } from "./dom.js";
import { api } from "./api.js";
import { openSourceReader } from "./source-reader.js";

let request = 0;

function checked(attribute) {
  return [...document.querySelectorAll(`[${attribute}]:checked`)].map(
    (box) => box.getAttribute(attribute),
  );
}

function filtersFromForm() {
  const must = $("search-must")
    .value.split(/\s+/)
    .map((term) => term.trim())
    .filter(Boolean)
    .slice(0, 10);
  const number = (id) => {
    const value = parseInt($(id).value, 10);
    return Number.isFinite(value) && value > 0 ? value : null;
  };
  const value = {
    doc_types: checked("data-search-type"),
    content_types: checked("data-search-content"),
    origins: checked("data-search-origin"),
    must_contain: must,
    source_name_contains: $("search-name").value.trim(),
    section_contains: $("search-section").value.trim(),
    page_from: number("search-page-from"),
    page_to: number("search-page-to"),
  };
  for (const [key, item] of Object.entries(value))
    if (item === null || item === "" || (Array.isArray(item) && !item.length))
      delete value[key];
  return value;
}

function highlighted(textValue, terms) {
  const block = el("p", "search-snippet");
  const words = terms.filter((t) => t.length > 0);
  if (!words.length) {
    block.textContent = textValue;
    return block;
  }
  const escaped = words.map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  const pattern = new RegExp(`(${escaped.join("|")})`, "gi");
  for (const part of textValue.split(pattern)) {
    if (!part) continue;
    if (words.some((w) => w.toLowerCase() === part.toLowerCase()))
      block.append(el("mark", "", part));
    else block.append(document.createTextNode(part));
  }
  return block;
}

function card(hit, terms) {
  const item = el("li", "search-hit");
  const head = el("div", "search-hit-head");
  head.append(
    el("span", "search-rank", "#" + hit.rank),
    el("strong", "", hit.source_name),
    el("span", "chip", hit.category),
  );
  if (hit.origins.some((o) => o !== "native"))
    head.append(
      el(
        "span",
        "chip warn",
        hit.origins.includes("ocr") ? "含 OCR 转录" : "含图片释义",
      ),
    );
  item.append(head);
  if (hit.section?.length)
    item.append(el("p", "search-section subtle", hit.section.join(" › ")));
  item.append(highlighted(hit.text, terms));
  item.append(el("p", "search-location subtle", "位置：" + hit.locations.join("；")));
  const score =
    hit.scores.rerank !== null && hit.scores.rerank !== undefined
      ? `重排分 ${Number(hit.scores.rerank).toFixed(3)}`
      : `RRF ${hit.scores.rrf}`;
  const routes = Object.entries(hit.scores.routes || {})
    .map(([route, rank]) => `${route}#${rank}`)
    .join(" · ");
  const actions = el("div", "search-actions");
  actions.append(
    button("阅读原文与 Markdown", () =>
      openSourceReader({
        source_name: hit.source_name,
        quote_title: "检索命中片段（未经校验）",
        quote: hit.text,
        locators: hit.locations.map((label) => ({ display_label: label })),
        resources: {
          markdown: hit.links.markdown,
          original: hit.links.original,
          snapshot: hit.links.original.replace(/[?&]inline=true$/, ""),
          preview: hit.links.markdown.replace("/content", "/preview"),
          images_gallery: hit.links.markdown.replace("/content", "/images"),
        },
      }),
    ),
    el("span", "subtle search-score", `${score}${routes ? " · " + routes : ""}`),
  );
  item.append(actions);
  return item;
}

async function run(getScope) {
  const question = $("search-question").value.trim();
  if (!question) return;
  const turn = ++request;
  const scope = $("search-use-scope").checked ? getScope() : {};
  const filters = filtersFromForm();
  if (scope.doc_types && !filters.doc_types)
    filters.doc_types = scope.doc_types;
  const body = {
    question,
    top_k: parseInt($("search-top-k").value, 10) || 8,
    categories: scope.categories || null,
    doc_ids: scope.doc_ids?.length ? scope.doc_ids : null,
    conversation_id: scope.conversation_id || null,
    filters: Object.keys(filters).length ? filters : null,
  };
  $("search-submit").disabled = true;
  text("search-summary", "正在检索…");
  clear("search-results");
  try {
    const data = await api("/search", {
      method: "POST",
      body,
      timeoutMs: 60000,
    });
    if (turn !== request) return;
    const conditions = data.applied_filter?.conditions
      ? Object.keys(data.applied_filter.conditions).length
      : 0;
    text(
      "search-summary",
      `${data.hits.length} 条片段 · 候选 ${data.candidate_count} · 用时 ${Math.round(
        data.elapsed_ms,
      )} ms${conditions ? ` · ${conditions} 项组合条件` : ""}${
        data.synthetic ? " · 演示模型（非真实语义）" : ""
      } · ${data.notice}`,
    );
    if (!data.hits.length) {
      $("search-results").append(
        el(
          "li",
          "search-empty subtle",
          data.reason === "no_eligible_documents"
            ? "当前条件下没有可检索的资料，请放宽条件。"
            : "当前指定资料范围内未检出相关片段。",
        ),
      );
      return;
    }
    const terms = [question, ...(filters.must_contain || [])]
      .join(" ")
      .split(/\s+/)
      .filter((t) => t.length > 1)
      .slice(0, 12);
    $("search-results").append(...data.hits.map((hit) => card(hit, terms)));
  } catch (error) {
    if (turn !== request) return;
    text("search-summary", "检索失败：" + errorText(error));
    toast("检索失败：" + errorText(error));
  } finally {
    if (turn === request) $("search-submit").disabled = false;
  }
}

export function bindSearch(getScope) {
  $("open-search").addEventListener("click", () => {
    // Narrow screens start with conditions folded so results stay visible.
    if (window.innerWidth < 640) $("search-dialog").querySelector("details").open = false;
    $("search-dialog").showModal();
    $("search-question").focus();
  });
  $("close-search").addEventListener("click", () => $("search-dialog").close());
  $("search-form").addEventListener("submit", (event) => {
    event.preventDefault();
    run(getScope);
  });
}
