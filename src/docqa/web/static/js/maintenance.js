import { $, el, button, toast, errorText } from "./dom.js";
import { api } from "./api.js";

const run = (fn) => async () => {
  try {
    await fn();
  } catch (e) {
    toast(errorText(e));
  }
};
export function bindMaintenance() {
  const section = $("maintenance-section");
  const load = async () => {
    const data = await api("/maintenance/sources");
    $("watch-allowed").textContent =
      "允许目录：" + data.allowed_roots.join("；");
    const list = $("watch-list");
    list.replaceChildren();
    for (const source of data.items) {
      const row = el("article", "watch-item");
      row.append(
        el("strong", "", source.path),
        el(
          "small",
          "subtle",
          `${source.enabled ? "监控中" : "已暂停"} · ${source.types.join(" / ")} · ${source.recursive ? "含子目录" : "当前目录"} · ${source.interval_seconds} 秒`,
        ),
      );
      const actions = el("div", "scope-actions");
      actions.append(
        button(
          "立即扫描",
          run(async () => {
            await api(`/maintenance/sources/${source.source_id}/scan`, {
              method: "POST",
              body: {},
            });
            toast("已加入扫描队列");
          }),
        ),
        button(
          source.enabled ? "暂停" : "恢复",
          run(async () => {
            await api(`/maintenance/sources/${source.source_id}`, {
              method: "PATCH",
              body: { enabled: !source.enabled },
            });
            await load();
          }),
        ),
        button(
          "查看文件",
          run(async () => {
            const value = await api(
              `/maintenance/sources/${source.source_id}/files`,
            );
            const details = el("details");
            details.open = true;
            details.append(
              el(
                "summary",
                "",
                `发现 ${value.files.length} 份文件${value.complete ? "" : " · 扫描有缺口"}`,
              ),
            );
            for (const file of value.files)
              details.append(el("p", "field-help", file.path));
            row.querySelector("details")?.remove();
            row.append(details);
          }),
        ),
        button(
          "删除监控",
          run(async () => {
            if (
              !confirm(
                "删除此监控，并将所属资料移出最新知识库？原始文件保留；仅临时停止请使用暂停。",
              )
            )
              return;
            await api(`/maintenance/sources/${source.source_id}`, {
              method: "DELETE",
            });
            await load();
          }),
        ),
      );
      row.append(actions);
      list.append(row);
    }
    if (!data.items.length)
      list.append(
        el(
          "p",
          "subtle",
          "尚未登记监控目录。浏览器上传文件夹为一次性导入，请在这里登记本机目录以持续更新。",
        ),
      );
  };
  section.addEventListener("toggle", () => {
    if (section.open) run(load)();
  });
  $("watch-refresh").addEventListener("click", run(load));
  $("watch-add").addEventListener(
    "click",
    run(async () => {
      const types = [
        ...document.querySelectorAll("[data-watch-type]:checked"),
      ].map((n) => n.dataset.watchType);
      if (!types.length) {
        toast("至少选择一种文档类型");
        return;
      }
      await api("/maintenance/sources", {
        method: "POST",
        body: {
          path: $("watch-path").value.trim(),
          types,
          recursive: $("watch-recursive").checked,
          interval_seconds: Number($("watch-interval").value),
          category: "auto",
        },
      });
      toast("监控目录已登记，自动分类并增量处理");
      await load();
    }),
  );
  $("index-rebuild").addEventListener(
    "click",
    run(async () => {
      await api("/maintenance/index/build", { method: "POST", body: {} });
      toast("已加入索引更新队列");
    }),
  );
  $("index-status").addEventListener(
    "click",
    run(async () => {
      const value = await api("/maintenance/index", { timeoutMs: 30000 });
      $("index-status-text").textContent =
        `已入库 ${value.documents ?? 0} 份 · ${value.chunks ?? 0} 个文本块 · ${value.vector_backend === "sqlite_vec" ? "SQLite-vec 向量库" : "旧版向量存储"} · ${(value.recall_routes || []).length} 路召回`;
    }),
  );
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
