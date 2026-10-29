import { $, el, button, text, clear, richText, errorText } from "./dom.js";
import { api, plain } from "./api.js";

let current = 0;
export function sourcePending(title) {
  current++;
  text("preview-title", title);
  text("preview-type", "来源阅读");
  clear("preview-actions");
  clear("preview-sources");
  const pending = el(
    "p",
    "reader-notice",
    "正在读取同版来源，首次访问会检查文件完整性…",
  );
  $("preview-content").replaceChildren(pending);
  if (!$("preview-dialog").open) $("preview-dialog").showModal();
  return (error) => {
    if (pending.isConnected)
      pending.textContent = errorText(error) + "。可关闭后重试。";
  };
}
const relative = (url) => {
  if (!url.startsWith("/api/v1/")) throw new Error("INVALID_RESOURCE");
  return url.slice(7);
};
function link(label, url, download = false) {
  relative(url);
  const a = el("a", "reader-file-link", label);
  a.href = url;
  a.target = "_blank";
  a.rel = "noopener";
  if (download) {
    a.download = "";
    a.removeAttribute("target");
  }
  return a;
}
function highlight(parent, value, quote) {
  const at = quote ? value.indexOf(quote) : -1;
  if (at < 0) parent.textContent = value;
  else
    parent.append(
      document.createTextNode(value.slice(0, at)),
      el("mark", "", quote),
      document.createTextNode(value.slice(at + quote.length)),
    );
}
function markdownView(value) {
  const root = el("div", "rich-text reader-markdown");
  let lines = value.split("\n");
  const firstBlock = lines.findIndex((line) => /^<a id="block-/.test(line));
  if (firstBlock > 0) {
    const info = el("details", "reader-metadata");
    info.append(
      el("summary", "subtle", "解析与版本信息"),
      el("pre", "reader-code", lines.slice(0, firstBlock).join("\n")),
    );
    root.append(info);
    lines = lines.slice(firstBlock);
  }
  for (let i = 0; i < lines.length; i++) {
    if (/^<a id="block-/.test(lines[i])) continue;
    if (/^\|/.test(lines[i]) && /^\|[\s:|\-]+$/.test(lines[i + 1] || "")) {
      const table = el("table", "reader-table");
      let first = true;
      while (i < lines.length && /^\|/.test(lines[i])) {
        if (!/^\|[\s:|\-]+$/.test(lines[i])) {
          const row = el("tr");
          for (const value of lines[i].slice(1, -1).split(/(?<!\\)\|/)) {
            const cell = el(first ? "th" : "td");
            cell.append(
              richText(
                value.trim().replaceAll("<br>", "\n").replaceAll("\\|", "|"),
              ),
            );
            row.append(cell);
          }
          table.append(row);
          first = false;
        }
        i++;
      }
      i--;
      const scroll = el("div", "table-scroll");
      scroll.append(table);
      root.append(scroll);
    } else if (/^!\[/.test(lines[i])) {
      root.append(
        el("p", "reader-notice", "图片请在相关图片区域或原文件中核对。"),
      );
    } else if (!/^> 来源：/.test(lines[i]))
      root.append(
        richText(
          lines[i]
            .replace(/^>\s?/, "")
            .replace(/\\([\\`*_{}\[\]#!|~+\-])/g, "$1"),
        ),
      );
  }
  return root;
}
export function openSourceReader(source) {
  const request = ++current;
  text("preview-title", source.source_name);
  text("preview-type", "来源阅读 · 同版原文");
  clear("preview-actions");
  $("preview-actions").append(
    link("原文件", source.resources.original),
    link("下载", source.resources.snapshot, true),
  );
  clear("preview-sources");
  const sidebar = $("preview-sources");
  sidebar.append(el("h3", "", "本次引用"));
  if (source.quote)
    sidebar.append(el("blockquote", "reader-quote", source.quote));
  sidebar.append(
    el(
      "p",
      "subtle",
      (source.locators || []).map((x) => x.display_label).join("；"),
    ),
  );
  sidebar.append(
    el(
      "p",
      "subtle",
      "高亮表示引用所在位置；图片、排版和遗漏内容请核对原文件。",
    ),
  );
  sidebar.append(
    link("打开原文件", source.resources.original),
    link("下载原文件", source.resources.snapshot, true),
  );
  if (!source.source_name.toLowerCase().endsWith(".pdf"))
    sidebar.append(
      el(
        "p",
        "subtle",
        "Word/PPT 将交给浏览器或本机 Office 打开；下方正文视图不会还原全部原始版式。",
      ),
    );
  const root = el("div", "source-reader"),
    tabs = el("nav", "reader-tabs"),
    content = el("article", "reader-content");
  root.append(tabs, content);
  $("preview-content").replaceChildren(root);
  if (!$("preview-dialog").open) $("preview-dialog").showModal();
  let original = source.original_content,
    markdown,
    selection = 0;
  const display = async (kind) => {
    const turn = ++selection;
    for (const b of tabs.children)
      b.setAttribute("aria-selected", String(b.dataset.kind === kind));
    content.replaceChildren(el("p", "subtle", "正在读取原文…"));
    try {
      if (kind === "images") {
        const base = source.resources.images_gallery;
        if (!base) throw new Error("IMAGE_PREVIEW_UNAVAILABLE");
        const grid = el("div", "reader-image-grid");
        const load = async (offset = 0) => {
          const data = await api(
            relative(base) +
              (base.includes("?") ? "&" : "?") +
              "offset=" +
              offset,
          );
          if (request !== current || turn !== selection || !root.isConnected)
            return;
          if (offset === 0)
            content.replaceChildren(
              el("p", "reader-notice", data.notice),
              el("h3", "", `全部图片 · ${data.total}`),
              grid,
            );
          for (const image of data.items) {
            const card = el("figure", "reader-image-card"),
              picture = el("img");
            picture.src = image.thumbnail;
            picture.loading = "lazy";
            picture.alt = image.caption;
            picture.addEventListener("error", () => {
              picture.alt = "此图片暂无法预览，请打开原文件核对";
            });
            const open = button("放大查看", () => {
              const dialog = el("dialog", "reader-image-dialog"),
                large = el("img");
              large.src = image.preview;
              large.alt = image.caption;
              dialog.append(
                button("关闭", () => dialog.close()),
                el("p", "", image.caption + " · " + image.location),
                large,
              );
              dialog.addEventListener("close", () => dialog.remove(), {
                once: true,
              });
              document.body.append(dialog);
              dialog.showModal();
            });
            card.append(
              picture,
              el("figcaption", "", image.caption),
              el(
                "small",
                "subtle",
                image.location +
                  (image.analysis_required ? "" : " · 归档/辅助图"),
              ),
              open,
            );
            grid.append(card);
          }
          if (!data.total)
            grid.append(
              el("p", "subtle", "该版本尚未提取图片，请查看原文件或重新解析。"),
            );
          if (data.next_offset !== null) {
            const more = button("加载更多图片", async () => {
              more.disabled = true;
              try {
                await load(data.next_offset);
                more.remove();
              } catch (e) {
                more.disabled = false;
                more.textContent = errorText(e) + "，重试";
              }
            });
            content.append(more);
          }
        };
        await load();
      } else if (kind === "original") {
        original ??= await api(relative(source.resources.preview));
        if (request !== current || turn !== selection || !root.isConnected)
          return;
        content.replaceChildren(el("p", "reader-notice", original.notice));
        const append = (data) => {
          for (const block of data.blocks) {
            const item = el("section", "reader-block");
            item.dataset.blockId = block.id;
            item.append(el("small", "reader-location", block.location));
            if (block.table_rows?.length) {
              const table = el("table", "reader-table");
              for (const values of block.table_rows) {
                const row = el("tr");
                for (const value of values) {
                  const cell = el("td");
                  highlight(cell, value, source.quote);
                  row.append(cell);
                }
                table.append(row);
              }
              const scroll = el("div", "table-scroll");
              scroll.append(table);
              item.append(scroll);
            } else {
              const body = el(block.type === "heading" ? "h3" : "p");
              highlight(body, block.text, source.quote);
              item.append(body);
            }
            content.append(item);
          }
          if (data.next_offset !== null) {
            const more = button(
              "继续阅读",
              async () => {
                more.disabled = true;
                try {
                  const url =
                    source.resources.preview +
                    (source.resources.preview.includes("?") ? "&" : "?") +
                    "offset=" +
                    data.next_offset;
                  const next = await api(relative(url));
                  if (
                    request !== current ||
                    turn !== selection ||
                    !root.isConnected
                  )
                    return;
                  more.remove();
                  append(next);
                } catch (e) {
                  more.disabled = false;
                  more.textContent = errorText(e) + "，点击重试";
                }
              },
              "text-button",
            );
            content.append(more);
          }
        };
        append(original);
        if (!original.blocks.length)
          content.append(
            el("p", "subtle", "暂无原生正文，请打开原文件核对扫描页或图片。"),
          );
      } else {
        markdown ??= await plain(source.resources.markdown);
        if (request !== current || turn !== selection || !root.isConnected)
          return;
        if (kind === "markdown")
          content.replaceChildren(markdownView(markdown));
        else {
          const pre = el("pre", "reader-code");
          markdown.split("\n").forEach((value, i) => {
            const line = el("span", "reader-line");
            line.dataset.line = i + 1;
            if (
              i + 1 >= source.markdown_start_line &&
              i + 1 <= source.markdown_end_line
            )
              line.classList.add("cited-line");
            line.append(
              el("small", "", i + 1),
              document.createTextNode(value || " "),
            );
            pre.append(line);
          });
          content.replaceChildren(pre);
        }
      }
      requestAnimationFrame(() => {
        if (request === current && turn === selection)
          content
            .querySelector("mark, .cited-line")
            ?.scrollIntoView({ block: "center" });
      });
    } catch (e) {
      if (request === current && turn === selection)
        content.replaceChildren(el("p", "answer-note error", errorText(e)));
    }
  };
  for (const [kind, label] of [
    ["original", "原文正文"],
    ["markdown", "Markdown"],
    ["raw", "MD 源码与定位"],
    ["images", "全部图片"],
  ]) {
    const b = button(label, () => display(kind));
    b.dataset.kind = kind;
    b.setAttribute("role", "tab");
    tabs.append(b);
  }
  tabs.setAttribute("role", "tablist");
  display(source.origin && source.origin !== "native" ? "raw" : "original");
}
