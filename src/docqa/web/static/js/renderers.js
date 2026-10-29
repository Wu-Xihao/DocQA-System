import {
  el,
  button,
  richText,
  sourceWarning,
  errorText,
  statusNames,
  locationText,
} from "./dom.js";
import { icon } from "./icons.js";

export const artifactTypes = {
  table: { label: "结构化表格", symbol: "▦", hint: "整理有据内容" },
  relation: { label: "关系图", symbol: "⌘", hint: "连接明确关系" },
  mindmap: { label: "思维导图", symbol: "❋", hint: "梳理主题脉络" },
  report: { label: "研究文档", symbol: "▤", hint: "形成可读报告" },
  slides: { label: "演示文稿", symbol: "▰", hint: "把要点带上台" },
};

export function refs(ids, select) {
  const wrap = el("span", "source-refs");
  for (const id of ids || []) {
    const label = /^C\d+$/.test(id) ? `[${Number(id.slice(1))}]` : id;
    const b = button(label, () => select([id]), "citation-chip");
    b.dataset.citationId = id;
    b.title = `${id} · 查看引用原文`;
    b.setAttribute("aria-label", `${id} 查看引用原文`);
    wrap.append(b);
  }
  return wrap;
}

export function answerContent(answer, select) {
  const root = el("div", "answer-body");
  if (answer.synthetic)
    root.append(
      el("p", "answer-note", "演示回答：使用合成模型，仅用于检查系统流程。"),
    );
  if (sourceWarning(answer))
    root.append(
      el(
        "p",
        "answer-note",
        "部分来源仍有解析缺口；本回答仅使用已获准入库的内容，未解析或不确定部分不作为证据。",
      ),
    );
  if (answer.requested_format === "table" && answer.claims.length) {
    const content = {
      columns: ["来源支持的内容", "类型", "引用"],
      rows: answer.claims.map((c) => {
        const ids = answer.citations
          .filter((x) => x.claim_ids.includes(c.claim_id))
          .map((x) => x.citation_id);
        return [
          { value: c.text, sources: ids },
          { value: c.kind, sources: ids },
          { value: ids.join(" "), sources: ids },
        ];
      }),
    };
    root.append(renderTable(content, select));
  } else {
    let lastTopic = null;
    for (const [index, claim] of (answer.claims || []).entries()) {
      const topic = claim.topic_ids[0];
      if (
        Object.keys(answer.topic_texts || {}).length > 1 &&
        topic !== lastTopic
      ) {
        root.append(
          el(
            "h3",
            "answer-section-title",
            answer.topic_texts[topic] || "相关要点",
          ),
        );
        lastTopic = topic;
      }
      const row = el("div", index === 0 ? "claim answer-lead" : "claim");
      if (claim.kind === "inference")
        row.append(el("small", "subtle", "基于证据的归纳："));
      const prose = richText(claim.text);
      const citations = refs(
        answer.citations
          .filter((c) => c.claim_ids.includes(claim.claim_id))
          .map((c) => c.citation_id),
        select,
      );
      (prose.lastElementChild || prose).append(citations);
      row.append(prose);
      root.append(row);
    }
  }
  if (answer.citations?.length) {
    const documents = el("div", "answer-documents");
    documents.append(el("span", "subtle", "参考资料"));
    const seen = new Set();
    for (const citation of answer.citations) {
      const key = citation.doc_id + ":" + citation.revision_id;
      if (seen.has(key)) continue;
      seen.add(key);
      const file = button(
        citation.source_name,
        () => select([citation.citation_id]),
        "answer-document",
      );
      file.title = "阅读原文件与 Markdown";
      documents.append(file);
    }
    root.append(documents);
  }
  if (
    answer.requested_format === "mermaid" &&
    (answer.diagram_edges || []).length
  ) {
    const labels = [
      ...new Set(
        answer.diagram_edges.flatMap((e) => [e.source_label, e.target_label]),
      ),
    ];
    root.append(
      renderGraph(
        {
          nodes: labels.map((label, i) => ({ id: String(i), label })),
          edges: answer.diagram_edges.map((e) => ({
            source: String(labels.indexOf(e.source_label)),
            target: String(labels.indexOf(e.target_label)),
            direction: e.direction,
            sources: answer.citations
              .filter((c) => c.claim_ids.some((id) => e.claim_ids.includes(id)))
              .map((c) => c.citation_id),
          })),
        },
        select,
      ),
    );
  }
  for (const conflict of answer.conflicts || [])
    root.append(
      el("p", "answer-note error", "来源冲突：" + conflict.description),
    );
  if (answer.unanswered_topics?.length)
    root.append(
      el(
        "p",
        "answer-note",
        "仍缺少证据：" +
          answer.unanswered_topics
            .map((t) => answer.topic_texts?.[t] || t)
            .join("；"),
      ),
    );
  if (!answer.claims?.length)
    root.append(
      el(
        "p",
        "answer-note",
        answer.status === "failed"
          ? errorText({ code: answer.stop_reason })
          : "当前范围内没有足够的可用证据，系统没有补造结论。",
      ),
    );
  const extra = (answer.warnings || []).filter(
    (w) =>
      !w.startsWith("SOURCE_COVERAGE_PARTIAL:") &&
      !w.startsWith("SYNTHETIC_DEMO") &&
      !(answer.status === "failed" && w === answer.stop_reason),
  );
  if (extra.length) {
    const d = el("details", "answer-diagnostics");
    d.append(el("summary", "subtle", "处理说明"));
    for (const code of extra) d.append(el("p", "subtle", errorText({ code })));
    root.append(d);
  }
  if (answer.performance) {
    const p = answer.performance,
      details = el("details", "answer-diagnostics");
    details.append(
      el(
        "summary",
        "subtle",
        `耗时明细 · Agent ${Number(p.elapsed_seconds).toFixed(1)} 秒 · 模型任务 ${p.model_tasks} 次`,
      ),
    );
    const labels = {
      session_prepare: "会话准备",
      index_load: "索引加载与检查",
      retrieval: "检索与重排合计",
      evidence_load: "证据加载",
      evidence_order: "证据整理",
      evidence_pack: "证据装包",
      "chat.plan": "查询规划",
      "chat.assess": "覆盖评估",
      "chat.compose": "回答生成",
      "chat.assess_and_compose": "覆盖评估与回答编排",
      "chat.verify": "回答支持校验",
      evidence_validate: "原文完整性检查",
      publishing: "引用构建与发布检查",
    };
    for (const [key, seconds] of Object.entries(p.operations || {}))
      details.append(
        el(
          "p",
          "subtle",
          `${labels[key] || key}：${Number(seconds).toFixed(2)} 秒`,
        ),
      );
    for (const round of p.retrieval_rounds || []) {
      const recall = (round.recall || [])
        .map((routes) =>
          Object.entries(routes)
            .map(
              ([route, seconds]) =>
                `${{ dense: "向量化与向量检索", bm25: "关键词检索", sparse: "稀疏检索" }[route] || route} ${Number(seconds).toFixed(2)}s`,
            )
            .join(" / "),
        )
        .join("；");
      details.append(
        el(
          "p",
          "subtle",
          `第 ${round.round} 轮：${recall}；重排 ${Number(round.rerank_seconds).toFixed(2)}s，候选 ${round.rerank_candidates} 个${round.reranker_state === "cached" ? "（复用本轮会话缓存）" : ""}`,
        ),
      );
    }
    details.append(
      el(
        "p",
        "subtle",
        "Agent 内部计时不含排队、进程启动和落盘后复核；并行分路与检索合计有重叠，不能相加。",
      ),
    );
    root.append(details);
  }
  return root;
}

export function renderTable(content, select) {
  const wrap = el("div", "data-table-wrap"),
    table = el("table", "data-table"),
    thead = el("thead"),
    header = el("tr"),
    body = el("tbody");
  let rows = [...content.rows],
    ascending = true;
  const draw = () => {
    body.replaceChildren();
    for (const row of rows) {
      const tr = el("tr");
      for (const cell of row) {
        const td = el("td");
        td.append(richText(cell.value), refs(cell.sources, select));
        tr.append(td);
      }
      body.append(tr);
    }
  };
  content.columns.forEach((label, i) => {
    const th = el("th");
    th.append(
      button(
        label + " ↕",
        () => {
          rows.sort(
            (a, b) =>
              String(a[i]?.value ?? "").localeCompare(
                String(b[i]?.value ?? ""),
                "zh-CN",
                { numeric: true },
              ) * (ascending ? 1 : -1),
          );
          ascending = !ascending;
          draw();
        },
        "table-sort",
      ),
    );
    header.append(th);
  });
  thead.append(header);
  table.append(thead, body);
  wrap.append(table);
  draw();
  return wrap;
}

const ns = "http://www.w3.org/2000/svg";
function svgNode(tag, attrs = {}, text) {
  const n = document.createElementNS(ns, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, String(v));
  if (text !== undefined) n.textContent = text;
  return n;
}
export function renderGraph(content, select) {
  const root = el("div", "graph-widget"),
    tools = el("div", "graph-tools"),
    canvas = el("div", "graph-canvas");
  const nodes = content.nodes || [],
    edges = content.edges || [],
    width = content.width || 1000,
    height =
      content.height || Math.max(420, Math.ceil(nodes.length / 3) * 150 + 50);
  let scale = 1,
    ox = 0,
    oy = 0;
  const svg = svgNode("svg", {
    viewBox: `0 0 ${width} ${height}`,
    role: "img",
    "aria-label": content.positions
      ? "思维导图：中心主题与左右分支"
      : "来源支持的关系图",
  });
  const defs = svgNode("defs"),
    marker = svgNode("marker", {
      id: "arrow-" + crypto.randomUUID(),
      viewBox: "0 0 10 10",
      refX: 9,
      refY: 5,
      markerWidth: 6,
      markerHeight: 6,
      orient: "auto-start-reverse",
    });
  marker.append(
    svgNode("path", { d: "M 0 0 L 10 5 L 0 10 z", fill: "#8c7dc7" }),
  );
  defs.append(marker);
  svg.append(defs);
  const pos = new Map(
    nodes.map((n, i) => [
      n.id,
      content.positions
        ? { x: content.positions[n.id][0], y: content.positions[n.id][1] }
        : { x: 170 + (i % 3) * 330, y: 75 + Math.floor(i / 3) * 150 },
    ]),
  );
  for (const edge of edges) {
    const a = pos.get(edge.source),
      b = pos.get(edge.target);
    if (!a || !b) continue;
    const dx = b.x - a.x,
      dy = b.y - a.y;
    const boundary = Math.min(
      dx ? 144 / Math.abs(dx) : Infinity,
      dy ? 46 / Math.abs(dy) : Infinity,
    );
    if (!Number.isFinite(boundary)) continue;
    const line = svgNode("path", {
      d: content.positions
        ? `M${a.x},${a.y} C${(a.x + b.x) / 2},${a.y} ${(a.x + b.x) / 2},${b.y} ${b.x},${b.y}`
        : `M${a.x + dx * boundary},${a.y + dy * boundary} L${b.x - dx * boundary},${b.y - dy * boundary}`,
      stroke: edge.color || "#b5a7dc",
      "stroke-width": 3,
      fill: "none",
      tabindex: 0,
      role: "button",
      "aria-label": `${edge.direction === "unknown" ? "方向未知" : "关系"}，点击查看来源`,
    });
    if (edge.direction === "directed")
      line.setAttribute("marker-end", `url(#${marker.id})`);
    if (edge.direction === "unknown")
      line.setAttribute("stroke-dasharray", "6 5");
    line.addEventListener("click", () => select(edge.sources || []));
    line.addEventListener("keydown", (e) => {
      if (e.key === "Enter") select(edge.sources || []);
    });
    svg.append(line);
  }
  for (const node of nodes) {
    const p = pos.get(node.id),
      group = svgNode("g", {
        class: "graph-node",
        tabindex: 0,
        role: "button",
        "aria-label": node.label,
      });
    group.append(
      svgNode("rect", {
        x: p.x - (content.positions ? 156 : 140),
        y: p.y - (content.heights?.[node.id] || 84) / 2,
        width: content.positions ? 312 : 280,
        height: content.heights?.[node.id] || 84,
        rx: 15,
        fill: node.level === 0 ? "var(--accent)" : "var(--surface)",
        stroke:
          node.level === 0 ? "var(--accent)" : node.color || "var(--border)",
      }),
      svgNode("title", {}, node.detail || node.label),
    );
    const label = svgNode("text", {
      x: p.x,
      y: content.lines
        ? p.y -
          ((content.lines[node.id].length - 1) * (content.line_height || 22)) /
            2
        : p.y - 12,
      "text-anchor": "middle",
      fill: node.level === 0 ? "var(--surface)" : "var(--text)",
      "font-size": content.font_size || 14,
      "font-family": "Microsoft YaHei,sans-serif",
    });
    const chars = Array.from(node.label);
    const rows =
      content.lines?.[node.id] ||
      Array.from(
        { length: Math.min(3, Math.ceil(chars.length / 18)) },
        (_, i) =>
          chars.slice(i * 18, (i + 1) * 18).join("") +
          (i === 2 && chars.length > 54 ? "…" : ""),
      );
    rows.forEach((row, i) =>
      label.append(
        svgNode(
          "tspan",
          { x: p.x, dy: i ? content.line_height || 22 : 0 },
          row,
        ),
      ),
    );
    group.append(label);
    const ids = node.sources || [
      ...new Set(
        edges
          .filter((e) => e.source === node.id || e.target === node.id)
          .flatMap((e) => e.sources || []),
      ),
    ];
    group.addEventListener("click", () => {
      select(ids);
      content.onNode?.(node);
    });
    group.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        select(ids);
        content.onNode?.(node);
      }
    });
    svg.append(group);
  }
  const update = () =>
    svg.setAttribute(
      "viewBox",
      `${ox} ${oy} ${width / scale} ${height / scale}`,
    );
  tools.append(
    button(
      "−",
      () => {
        scale = Math.max(0.35, scale - 0.2);
        update();
      },
      "icon-button",
    ),
    button("适应画布", () => {
      scale = 1;
      ox = oy = 0;
      update();
    }),
    button(
      "＋",
      () => {
        scale = Math.min(3, scale + 0.2);
        update();
      },
      "icon-button",
    ),
    el("small", "subtle", "可拖动画布 · 点击节点或关系查看来源"),
  );
  if (content.positions && root.requestFullscreen) {
    const fullscreen = button("全屏画布", () => {
      if (document.fullscreenElement === root) document.exitFullscreen();
      else
        root.requestFullscreen().catch(() => {
          tools.append(
            el("small", "subtle", "浏览器未允许全屏，可使用＋放大和拖动。"),
          );
        });
    });
    root.addEventListener("fullscreenchange", () => {
      fullscreen.textContent =
        document.fullscreenElement === root ? "退出全屏" : "全屏画布";
    });
    tools.append(fullscreen);
  }
  let drag;
  svg.addEventListener("pointerdown", (e) => {
    if (e.target.closest(".graph-node")) return;
    drag = { x: e.clientX, y: e.clientY, ox, oy };
    svg.setPointerCapture(e.pointerId);
  });
  svg.addEventListener("pointermove", (e) => {
    if (!drag) return;
    const ratio = width / svg.getBoundingClientRect().width / scale;
    ox = drag.ox - (e.clientX - drag.x) * ratio;
    oy = drag.oy - (e.clientY - drag.y) * ratio;
    update();
  });
  svg.addEventListener("pointerup", () => (drag = null));
  svg.addEventListener("pointercancel", () => (drag = null));
  canvas.append(svg);
  root.append(tools, canvas);
  return root;
}

export function renderMindmap(content, select) {
  const root = el("div", "mindmap"),
    toolbar = el("div", "graph-tools"),
    branches = el("div", "map-branches");
  root.append(el("div", "map-root", content.tree.label));
  for (const group of content.tree.children) {
    const d = el("details", "map-branch");
    d.open = true;
    d.append(el("summary", "", group.label));
    for (const fact of group.children) {
      const leaf = el("div", "map-leaf");
      leaf.append(
        button(fact.text, () => select(fact.sources), "map-fact"),
        refs(fact.sources, select),
      );
      d.append(leaf);
    }
    branches.append(d);
  }
  toolbar.append(
    button("展开全部", () =>
      branches.querySelectorAll("details").forEach((d) => (d.open = true)),
    ),
    button("折叠全部", () =>
      branches.querySelectorAll("details").forEach((d) => (d.open = false)),
    ),
    el("small", "subtle", "主题分组用于整理，不表示因果关系。"),
  );
  if (content.map_layout) {
    const detail = el(
      "section",
      "map-selected",
      "点击节点查看完整内容及引用；主题标题为编排分组，不表示新增事实。",
    );
    const graph = renderGraph(
      {
        ...content.map_layout,
        onNode: (node) => {
          detail.replaceChildren(
            el("strong", "", node.label),
            richText(node.detail || node.label),
            refs(node.sources || [], select),
          );
        },
      },
      select,
    );
    root.replaceChildren(graph, detail);
    const outline = el("details", "map-outline");
    outline.append(
      el("summary", "", "展开文字大纲 / 分支折叠"),
      toolbar,
      branches,
    );
    root.append(outline);
  } else root.append(toolbar, branches);
  return root;
}

export function renderReport(artifact, select) {
  const page = el("article", "report-page");
  page.append(
    el("h1", "", artifact.title),
    el(
      "p",
      "subtle",
      `资料版本 ${artifact.generation_id.slice(0, 12)} · 来源可追溯`,
    ),
  );
  if (artifact.content.quality !== "answered")
    page.append(el("p", "answer-note", "来源覆盖有缺口，仅整理已支持内容。"));
  page.append(el("h2", "", "研究要点"));
  for (const fact of artifact.content.facts) {
    const n = el("section", "claim");
    n.append(richText(fact.text), refs(fact.sources, select));
    page.append(n);
  }
  if (artifact.content.conflicts?.length) {
    page.append(el("h2", "", "来源冲突"));
    for (const c of artifact.content.conflicts)
      page.append(richText(c.description));
  }
  if (artifact.content.unanswered_topics?.length)
    page.append(
      el(
        "p",
        "answer-note",
        "尚待解决：" +
          artifact.content.unanswered_topics
            .map((t) => artifact.content.topic_labels[t] || t)
            .join("；"),
      ),
    );
  page.append(el("h2", "", "引用来源"));
  for (const c of artifact.content.citations) {
    const n = el("p");
    n.append(
      refs([c.id], select),
      document.createTextNode(c.name + " · " + c.location),
    );
    page.append(n);
  }
  return page;
}

export function renderSlides(artifact, select) {
  const pages = [
    {
      title: artifact.title,
      layout: "cover",
      body:
        "基于已核验来源整理\n" +
        (artifact.content.quality === "answered"
          ? "来源可追溯"
          : "来源覆盖有缺口"),
      sources: [],
    },
    ...(artifact.content.outline?.length
      ? [
          {
            title: "内容导航",
            body: artifact.content.outline
              .map((title, i) => `${String(i + 1).padStart(2, "0")}   ${title}`)
              .join("\n"),
            sources: [],
            layout: "agenda",
          },
        ]
      : []),
    ...artifact.content.slides,
    ...Array.from(
      { length: Math.ceil(artifact.content.citations.length / 4) },
      (_, i) => ({
        title: "引用与延伸阅读",
        body: artifact.content.citations
          .slice(i * 4, i * 4 + 4)
          .map((c) => `[${Number(c.id.slice(1))}] ${c.name} · ${c.location}`)
          .join("\n\n"),
        sources: artifact.content.citations
          .slice(i * 4, i * 4 + 4)
          .map((c) => c.id),
        layout: "sources",
      }),
    ),
    ...artifact.content.conflicts.map((c) => ({
      title: "来源冲突（须保留）",
      body: c.description,
      sources: [],
    })),
    ...(artifact.content.unanswered_topics.length
      ? [
          {
            title: "尚待解决的要点",
            body: artifact.content.unanswered_topics
              .map((t) => artifact.content.topic_labels[t] || t)
              .join("；"),
            sources: [],
          },
        ]
      : []),
  ];
  let index = 0;
  const root = el("div", "slides-layout"),
    thumbs = el("div", "slide-thumbnails"),
    main = el("div"),
    page = el("article", "slide-page"),
    nav = el("div", "slide-nav"),
    counter = el("span");
  const draw = () => {
    const selected = pages[index];
    page.dataset.layout = selected.layout || "focus";
    page.replaceChildren(
      el(
        "small",
        "slide-kicker",
        `DOCQA RESEARCH / ${String(index + 1).padStart(2, "0")}`,
      ),
      el("h2", "", selected.title),
    );
    const body = el("div", "slide-body");
    if (selected.cards?.length) {
      body.classList.add("slide-cards");
      for (const card of selected.cards) {
        const item = el("section", "slide-card");
        item.append(
          el("small", "", card.label),
          richText(card.body),
          refs(card.sources, select),
        );
        body.append(item);
      }
    } else body.textContent = selected.body;
    page.append(body);
    const footer = el("footer");
    footer.append(
      refs(pages[index].sources, select),
      document.createTextNode(`DocQA · ${index + 1}`),
    );
    page.append(footer);
    counter.textContent = `${index + 1} / ${pages.length}`;
    thumbs
      .querySelectorAll("button")
      .forEach((n, i) => n.classList.toggle("active", i === index));
  };
  pages.forEach((p, i) =>
    thumbs.append(
      button(
        `${i + 1}  ${p.title}`,
        () => {
          index = i;
          draw();
        },
        "slide-thumb",
      ),
    ),
  );
  nav.append(
    button("‹ 上一页", () => {
      index = Math.max(0, index - 1);
      draw();
    }),
    counter,
    button("下一页 ›", () => {
      index = Math.min(pages.length - 1, index + 1);
      draw();
    }),
  );
  main.append(page, nav);
  root.append(thumbs, main);
  draw();
  return root;
}

export function renderArtifact(artifact, select) {
  const c = artifact.content;
  const view =
    artifact.kind === "table"
      ? renderTable(c, select)
      : artifact.kind === "relation"
        ? renderGraph(c, select)
        : artifact.kind === "mindmap"
          ? renderMindmap(c, select)
          : artifact.kind === "slides"
            ? renderSlides(artifact, select)
            : renderReport(artifact, select);
  const root = el("div", "artifact-view");
  if (c.synthetic)
    root.append(el("p", "answer-note", "演示成果：未做真实模型语义校验。"));
  if (c.quality !== "answered")
    root.append(
      el("p", "answer-note", "仅整理已支持内容；请核对原回答的缺口与冲突。"),
    );
  if (c.organization_notice)
    root.append(el("p", "answer-note", c.organization_notice));
  if (c.organization === "ai_editorial")
    root.append(
      el(
        "p",
        "subtle",
        "AI 已整理主题与顺序；内容来自已校验回答，引用保持可追溯。",
      ),
    );
  root.append(view);
  return root;
}
export function messageHeader(answer) {
  const head = el("div", "assistant-head");
  head.append(
    icon("source", 22),
    el("strong", "", "DocQA"),
    el("span", "answer-status", statusNames[answer.status] || answer.status),
  );
  return head;
}
