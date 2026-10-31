import { $, el, button, toast, errorText } from "./dom.js";
import { api } from "./api.js";
import { icon } from "./icons.js";

function intervalLabel(seconds) {
  if (seconds % 3600 === 0) return "每 " + seconds / 3600 + " 小时";
  if (seconds % 60 === 0) return "每 " + seconds / 60 + " 分钟";
  return "每 " + seconds + " 秒";
}

export function bindMaintenance() {
  const section = $("maintenance-section");
  let ready = false,
    loading = null,
    loadedAt = 0,
    signature = "",
    canAuthorize = false,
    feedbackTimer;
  const feedback = (message = "", failed = false) => {
    clearTimeout(feedbackTimer);
    const node = $("watch-feedback");
    node.textContent = message;
    node.hidden = !message;
    node.classList.toggle("error", failed);
    if (message && !failed && !message.startsWith("正在"))
      feedbackTimer = setTimeout(() => {
        node.textContent = "";
        node.hidden = true;
      }, 6000);
  };
  const perform = async (control, work) => {
    if (control.disabled) return;
    control.disabled = true;
    control.setAttribute("aria-busy", "true");
    try {
      await work();
    } catch (error) {
      feedback(errorText(error), true);
    } finally {
      control.disabled = false;
      control.removeAttribute("aria-busy");
    }
  };
  const formOpen = (open) => {
    $("watch-form").hidden = !open;
    $("watch-new").setAttribute("aria-expanded", String(open));
    $("watch-new").hidden = open;
    if (open) $("watch-path").focus();
  };
  const action = (label, glyph, work, compact = false) => {
    const control = button(
      compact ? "" : label,
      () => perform(control, work),
      compact ? "watch-icon-button" : "watch-menu-action",
    );
    control.prepend(icon(glyph, 17));
    control.title = label;
    control.setAttribute("aria-label", label);
    return control;
  };
  function sourceRow(source) {
    const name =
      source.path
        .replace(/[\\/]$/, "")
        .split(/[\\/]/)
        .at(-1) || source.path;
    const row = el("article", "watch-item");
    row.dataset.sourceId = source.source_id;
    const heading = el("div", "watch-item-heading");
    const title = el("strong", "watch-name", name);
    title.title = source.path;
    const badge = el(
      "span",
      "watch-state",
      source.enabled ? "已启用" : "已暂停",
    );
    badge.dataset.enabled = String(source.enabled);
    heading.append(icon("folder", 18), title, badge);
    const path = el("p", "watch-path", source.path);
    path.title = source.path;
    row.append(
      heading,
      path,
      el(
        "p",
        "watch-meta",
        intervalLabel(source.interval_seconds) +
          " · " +
          (source.recursive ? "含子目录" : "当前目录") +
          " · " +
          source.types.map((t) => t.toUpperCase()).join(" / "),
      ),
    );
    const footer = el("div", "watch-item-actions");
    const scan = action("立即扫描", "refresh", async () => {
      await api("/maintenance/sources/" + source.source_id + "/scan", {
        method: "POST",
        body: {},
      });
      feedback("扫描已排队，完成后资料库会自动更新。");
      window.dispatchEvent(new Event("docqa:library-dirty"));
    });
    const menu = el("details", "watch-menu"),
      summary = el("summary");
    summary.setAttribute("aria-label", "管理目录：" + name);
    summary.title = "管理目录";
    summary.append(icon("more", 19));
    const panel = el("div", "watch-menu-panel");
    panel.append(
      action(
        source.enabled ? "暂停同步" : "恢复同步",
        "folder-sync",
        async () => {
          await api("/maintenance/sources/" + source.source_id, {
            method: "PATCH",
            body: { enabled: !source.enabled },
          });
          menu.open = false;
          await load(true);
        },
      ),
      action("查看文件", "report", async () => {
        const value = await api(
          "/maintenance/sources/" + source.source_id + "/files",
        );
        menu.open = false;
        row.querySelector(".watch-files")?.remove();
        const details = el("details", "watch-files"),
          caption = el(
            "summary",
            "",
            "已发现 " +
              value.files.length +
              " 份文件" +
              (value.complete ? "" : "（扫描未完成）"),
          );
        details.open = true;
        details.append(caption);
        let shown = 0;
        const more = button("加载更多", () => appendFiles(), "text-button");
        function appendFiles() {
          const fragment = document.createDocumentFragment();
          for (const file of value.files.slice(shown, shown + 40)) {
            const item = el("p", "", file.path.split(/[\\/]/).at(-1));
            item.title = file.path;
            fragment.append(item);
          }
          shown += 40;
          details.insertBefore(fragment, more);
          more.hidden = shown >= value.files.length;
        }
        details.append(more);
        appendFiles();
        row.append(details);
      }),
      action("移除监控", "close", async () => {
        if (
          !confirm(
            "移除此监控，并将所属资料移出最新知识库？原始文件保留；临时停止请选择暂停同步。",
          )
        )
          return;
        await api("/maintenance/sources/" + source.source_id, {
          method: "DELETE",
        });
        menu.open = false;
        await load(true);
        window.dispatchEvent(new Event("docqa:library-dirty"));
      }),
    );
    menu.append(summary, panel);
    footer.append(scan, menu);
    row.append(footer);
    return row;
  }
  function load(force = false) {
    if (!ready) return Promise.resolve();
    if (loading) return loading;
    if (!force && Date.now() - loadedAt < 15000) return Promise.resolve();
    const list = $("watch-list");
    list.setAttribute("aria-busy", "true");
    feedback("正在读取目录…");
    loading = api("/maintenance/sources")
      .then((data) => {
        loadedAt = Date.now();
        canAuthorize = !!data.can_authorize;
        const next = JSON.stringify(data);
        if (next !== signature) {
          signature = next;
          const fragment = document.createDocumentFragment();
          for (const source of data.items) fragment.append(sourceRow(source));
          if (!data.items.length) {
            const empty = el("div", "watch-empty");
            empty.append(
              icon("folder-sync", 24),
              el("strong", "", "还没有监控目录"),
              el("p", "", "添加目录，自动同步后续变化。"),
            );
            fragment.append(empty);
          }
          list.replaceChildren(fragment);
          $("watch-count").textContent = data.items.length;
          $("watch-count").hidden = !data.items.length;
          const mine = new Set(data.web_authorized_roots || []);
          $("watch-allowed").replaceChildren(
            ...data.allowed_roots.map((path) => {
              const row = el("p", "allowed-root", path);
              if (mine.has(path)) {
                const revoke = button("取消授权", () =>
                  perform(revoke, async () => {
                    if (!confirm(`取消授权 ${path}？其中不能有仍在监控的目录。`))
                      return;
                    await api("/maintenance/roots", {
                      method: "DELETE",
                      body: { path },
                    });
                    await load(true);
                    toast("已取消授权。");
                  }),
                );
                row.append(" ", revoke);
              }
              return row;
            }),
          );
        }
        feedback();
      })
      .catch((error) => feedback(errorText(error), true))
      .finally(() => {
        list.removeAttribute("aria-busy");
        loading = null;
      });
    return loading;
  }
  const controls = [
    "watch-new",
    "watch-refresh",
    "watch-add",
    "index-status",
    "index-rebuild",
  ];
  controls.forEach((id) => {
    $(id).disabled = true;
  });
  window.addEventListener(
    "docqa:ready",
    (event) => {
      ready = !!event.detail.uploads_enabled;
      controls.forEach((id) => {
        $(id).disabled = !ready;
      });
      if (ready && section.open) load();
      if (!ready) feedback("当前未启用目录同步。");
    },
    { once: true },
  );
  section.addEventListener("toggle", () => {
    if (section.open) load();
  });
  $("watch-refresh").addEventListener("click", () =>
    perform($("watch-refresh"), () => load(true)),
  );
  $("watch-new").addEventListener("click", () => formOpen(true));
  $("watch-cancel").addEventListener("click", () => {
    formOpen(false);
    $("watch-new").focus();
  });
  $("watch-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const types = [
      ...document.querySelectorAll("[data-watch-type]:checked"),
    ].map((n) => n.dataset.watchType);
    if (!types.length) {
      feedback("请至少选择一种文档类型。", true);
      return;
    }
    const path = $("watch-path").value.trim();
    if (!path) {
      feedback("请输入文件夹路径。", true);
      $("watch-path").focus();
      return;
    }
    perform($("watch-add"), async () => {
      const body = {
        path,
        types,
        recursive: $("watch-recursive").checked,
        interval_seconds: Number($("watch-interval").value),
        category: "auto",
      };
      try {
        await api("/maintenance/sources", { method: "POST", body });
      } catch (error) {
        if (error.code !== "SOURCE_ROOT_NOT_ALLOWED" || !canAuthorize) throw error;
        if (
          !confirm(
            `“${path}” 尚未授权给后台。
授权后将写入 Service 配置的 allowed_source_roots，后台可读取该目录下的资料。是否授权并添加监控？`,
          )
        )
          return;
        feedback("正在授权目录…");
        await api("/maintenance/roots", { method: "POST", body: { path } });
        await api("/maintenance/sources", { method: "POST", body });
      }
      $("watch-path").value = "";
      formOpen(false);
      await load(true);
      $("watch-new").focus();
      toast("目录已添加，后台将按设置同步。");
      window.dispatchEvent(new Event("docqa:library-dirty"));
    });
  });
  $("index-status").addEventListener("click", () =>
    perform($("index-status"), async () => {
      $("index-status-text").textContent = "正在读取知识库状态…";
      try {
        const value = await api("/maintenance/index", { timeoutMs: 30000 });
        $("index-status-text").textContent =
          (value.documents ?? 0) +
          " 份资料，" +
          (value.chunks ?? 0) +
          " 个文本块";
      } catch (error) {
        $("index-status-text").textContent = errorText(error);
        throw error;
      }
    }),
  );
  $("index-rebuild").addEventListener("click", () =>
    perform($("index-rebuild"), async () => {
      await api("/maintenance/index/build", { method: "POST", body: {} });
      $("index-status-text").textContent = "更新已排队，完成后自动生效。";
      window.dispatchEvent(new Event("docqa:library-dirty"));
    }),
  );
  document.addEventListener("click", (event) => {
    for (const menu of section.querySelectorAll(".watch-menu[open]"))
      if (!menu.contains(event.target)) menu.open = false;
  });
  section.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    const menu = section.querySelector(".watch-menu[open]");
    if (menu) {
      event.stopPropagation();
      menu.open = false;
      menu.querySelector("summary").focus();
    }
  });
}

export async function showImageAnalysis(identity) {
  const value = await api(`/documents/${identity}/analysis`, {
    timeoutMs: 30000,
  });
  const figures = value.figures || [];
  const good = figures.filter(
    (f) =>
      f.semantic_state === "described" &&
      ["ok", "no_semantic_content"].includes(f.vision?.status) &&
      !f.vision?.uncertainties?.length &&
      !(f.vision?.relations || []).some((r) => r.direction === "unknown"),
  ).length;
  toast(
    `图片 ${figures.length} 张 · 释义成功且无不确定项 ${good} 张；待处理或需复核 ${figures.length - good} 张。`,
  );
}

export async function retryImageAnalysis(identity) {
  if (!confirm("将重新处理这份文档并重试图片解析，可能调用模型服务。继续？"))
    return;
  await api(`/documents/${identity}/analysis/retry`, {
    method: "POST",
    body: {},
  });
  toast("已加入图片解析重试队列");
  window.dispatchEvent(new Event("docqa:library-dirty"));
}
