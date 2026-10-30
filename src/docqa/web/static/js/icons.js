// Local, consistent stroke icons: no font download or external icon service.
const paths = {
  chat: ["M4 4h16v12H9l-5 4Z", "M8 8h8", "M8 12h5"],
  image: ["M3 4h18v16H3Z", "m3 16 5-5 4 4 3-3 6 6", "M16 8h.01"],
  copy: ["M8 8h12v13H8Z", "M16 8V3H3v13h5"],
  folder: ["M3 5h7l2 3h9v12H3Z"],
  "folder-sync": [
    "M21 10V8h-9l-2-3H3v15h7",
    "M21 16a5 5 0 0 0-8-2",
    "M13 10v4h4",
    "M13 18a5 5 0 0 0 8 2",
    "M21 22v-4h-4",
  ],
  database: [
    "M20 6c0 2-4 3-8 3S4 8 4 6s4-3 8-3 8 1 8 3Z",
    "M4 6v12c0 2 4 3 8 3s8-1 8-3V6",
    "M4 12c0 2 4 3 8 3s8-1 8-3",
  ],
  info: ["M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Z", "M12 11v6", "M12 7h.01"],
  plus: ["M12 5v14", "M5 12h14"],
  more: ["M5 12h.01", "M12 12h.01", "M19 12h.01"],
  history: ["M3 11a9 9 0 1 1 2 7", "M3 5v6h6", "M12 7v5l3 2"],
  source: ["M8 4H5v16h3", "M16 4h3v16h-3", "M10 9l2-1v8", "M10 16h4"],
  report: [
    "M14 3H6a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8Z",
    "M14 3v5h5",
    "M9 12h6",
    "M9 16h6",
  ],
  table: ["M4 4h16v16H4Z", "M4 10h16", "M10 10v10"],
  relation: [
    "M4 4h5v5H4Z",
    "M15 4h5v5h-5Z",
    "M10 16h5v5h-5Z",
    "M9 6.5h6",
    "M6.5 9v4h6v3",
  ],
  mindmap: [
    "M9 9h6v6H9Z",
    "M15 12h5V5h-3",
    "M20 12v7h-3",
    "M9 12H4V5h3",
    "M4 12v7h3",
  ],
  slides: ["M3 4h18v13H3Z", "M8 22l4-5 4 5", "M7 8h10", "M7 12h6"],
  wordcloud: ["M6 18a4 4 0 0 1-1-7.9A7 7 0 0 1 18 8a5 5 0 0 1 0 10Z", "M8 11h5", "M7 14h3", "M13 14h4"],
  compare: ["M4 7h16l-4-4", "M20 17H4l4 4", "M20 7l-4 4", "M4 17l4-4"],
  "panel-left": ["M3 4h18v16H3Z", "M9 4v16", "M5.5 8h1", "M5.5 12h1"],
  "panel-right": ["M3 4h18v16H3Z", "M15 4v16", "M17.5 8h1", "M17.5 12h1"],
  theme: ["M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Z", "M12 3v18"],
  reset: ["M4 8h16", "M7 5 4 8l3 3", "M17 13l3 3-3 3", "M4 16h16"],
  upload: ["M12 16V3", "M7 8l5-5 5 5", "M4 15v6h16v-6"],
  search: ["M11 3a8 8 0 1 0 0 16 8 8 0 0 0 0-16Z", "M17 17l4 4"],
  chart: ["M4 20V11", "M10 20V5", "M16 20v-6", "M3 20h18", "M20 8l-3-3"],
  refresh: ["M20 10a8 8 0 1 0-2 8", "M20 4v6h-6"],
  edit: ["m15 4 5 5", "M4 20l5-1L21 7l-5-5L4 14Z"],
  attach: ["m8 13 7-7a3 3 0 0 1 4 4l-9 9a5 5 0 0 1-7-7l10-10"],
  send: ["M12 20V4", "m5 11 7-7 7 7"],
  close: ["m6 6 12 12", "M6 18 18 6"],
  chevron: ["m9 5 7 7-7 7"],
};

export function icon(name, size = 20) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  for (const [key, value] of Object.entries({
    viewBox: "0 0 24 24",
    width: size,
    height: size,
    fill: "none",
    stroke: "currentColor",
    "stroke-width": "1.7",
    "stroke-linecap": "round",
    "stroke-linejoin": "round",
    "aria-hidden": "true",
    focusable: "false",
  }))
    svg.setAttribute(key, value);
  svg.classList.add("ui-icon");
  for (const d of paths[name] || paths.report) {
    const path = document.createElementNS(svg.namespaceURI, "path");
    path.setAttribute("d", d);
    svg.append(path);
  }
  return svg;
}

export function mountIcons() {
  for (const host of document.querySelectorAll("[data-icon]"))
    host.replaceChildren(icon(host.dataset.icon));
  const controls = {
    "reset-layout": "reset",
    "theme-button": "theme",
    "rename-chat": "edit",
    "attach-button": "attach",
    "ask-button": "send",
    "close-right": "chevron",
    "close-preview": "close",
    "close-image": "close",
    "refresh-documents": "refresh",
    "refresh-history": "refresh",
    "refresh-artifacts": "refresh",
    "refresh-images": "refresh",
  };
  for (const [id, name] of Object.entries(controls))
    document.getElementById(id)?.replaceChildren(icon(name, 19));
  document.querySelector(".upload-symbol")?.replaceChildren(icon("upload", 24));
  for (const host of document.querySelectorAll(".search-field > span"))
    host.replaceChildren(icon("search", 17));
}
