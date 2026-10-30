import { el, button } from "./dom.js";

function svgNode(tag, attrs = {}) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  return node;
}

export function renderWordcloud(content, select) {
  const root = el("section", "wordcloud-view");
  const cloud = content.wordcloud_layout;
  const words = new Map(content.words.map((word) => [word.id, word]));
  const tools = el("div", "wordcloud-tools");
  const feedback = el(
    "p", "wordcloud-selection", "点击关键词，查看对应原文引用。",
  );
  feedback.setAttribute("aria-live", "polite");
  const canvas = el("div", "wordcloud-canvas");
  const svg = svgNode("svg", {
    viewBox: `0 0 ${cloud.width} ${cloud.height}`,
    role: "group",
    "aria-label": "关键词词云，字号表示相对出现频次；可使用 Tab 与 Enter 查看来源",
  });
  let zoom = 1;
  function resize(value) {
    zoom = Math.max(1, Math.min(3, value));
    svg.style.width = `${zoom * 100}%`;
    zoomLabel.textContent = `${Math.round(zoom * 100)}%`;
  }
  const zoomLabel = el("span", "subtle", "100%");
  tools.append(
    el("strong", "", `${content.words.length} 个关键词`),
    button("−", () => resize(zoom - .25), "tiny-button"),
    zoomLabel,
    button("＋", () => resize(zoom + .25), "tiny-button"),
    button("适应", () => {
      resize(1);
      canvas.scrollTo(0, 0);
    }),
  );
  tools.querySelectorAll("button")[0].setAttribute("aria-label", "缩小词云");
  tools.querySelectorAll("button")[1].setAttribute("aria-label", "放大词云");
  function choose(word) {
    for (const node of root.querySelectorAll("[data-word-id]")) {
      const active = node.dataset.wordId === word.id;
      node.classList.toggle("active", active);
      node.setAttribute("aria-pressed", String(active));
    }
    feedback.textContent = `${word.text}：出现 ${word.weight} 次，来自 ${word.claim_ids.length} 条已核验要点。`;
    select(word.sources);
  }
  for (const position of cloud.positions) {
    const word = words.get(position.id);
    if (!word) continue;
    const node = svgNode("text", {
      x: position.x,
      y: position.y,
      "text-anchor": "middle",
      "dominant-baseline": "central",
      "font-size": position.font_size,
      textLength: position.text_width,
      lengthAdjust: "spacingAndGlyphs",
      tabindex: "0",
      role: "button",
      "aria-label": `${word.text}，出现 ${word.weight} 次，查看来源`,
      "aria-pressed": "false",
      "data-word-id": word.id,
      "data-color": position.color,
      class: "wordcloud-word",
    });
    node.textContent = word.text;
    const title = svgNode("title");
    title.textContent = `${word.text} · ${word.weight} 次 · ${word.sources.join("、")}`;
    node.append(title);
    node.addEventListener("click", () => choose(word));
    node.addEventListener("keydown", (event) => {
      if (["Enter", " "].includes(event.key)) {
        event.preventDefault();
        choose(word);
      }
    });
    svg.append(node);
  }
  canvas.append(svg);
  const list = el("details", "wordcloud-list");
  list.append(el("summary", "", "查看全部关键词与词频"));
  const terms = el("div", "wordcloud-terms");
  for (const word of content.words) {
    const term = button("", () => choose(word), "wordcloud-term");
    term.dataset.wordId = word.id;
    term.setAttribute("aria-pressed", "false");
    term.append(el("span", "", word.text), el("small", "", `${word.weight} 次`));
    terms.append(term);
  }
  list.append(terms);
  root.append(
    tools,
    el("p", "field-help", "仅统计当前已核验回答；字号表示相对词频，不代表整个资料库的词频或重要性。"),
    canvas, feedback, list,
  );
  if (cloud.positions.length < content.words.length)
    root.append(el("p", "field-help", "部分关键词收录在下方列表中，可展开查看全部关键词。"));
  return root;
}
