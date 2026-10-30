const ready = new Set(["ready", "ready_with_gaps"]);
const failed = new Set(["failed", "review_required", "upload_failed"]);
const labels = {
  queued: "等待解析",
  retry_wait: "等待后台重试",
  reading: "读取与检查文件",
  extracting: "提取正文、表格与图片",
  images: "图片识别与释义",
  markdown: "生成 Markdown 并检查",
  parsed: "解析完成，等待入库",
  verify_sources: "检查入库资料",
  chunk: "整理文本块",
  dense: "向量化文本块",
  sparse: "生成稀疏索引",
  write_and_validate: "写入并校验知识库",
  published: "知识库已发布",
};

export function uploadProgress(upload) {
  const p = upload.progress || {};
  const label =
    labels[p.stage] ||
    (upload.state === "indexing" ? "正在构建知识库" : "正在解析资料");
  if (upload.state === "queued")
    return {
      label: labels[p.stage] || "等待解析",
      current: 0,
      total: 1,
      counted: false,
    };
  if (!["parsing", "indexing", "committed"].includes(upload.state)) return null;
  const counted =
    Number.isInteger(p.current) &&
    Number.isInteger(p.total) &&
    p.total > 0 &&
    p.current >= 0 &&
    p.current <= p.total;
  return {
    label: counted ? `${label} · ${p.current}/${p.total}` : label,
    current: counted ? p.current : null,
    total: counted ? p.total : null,
    counted,
  };
}

export function uploadBatchStatus(batch) {
  const records = [...batch.items.values()];
  const done = records.filter((u) => ready.has(u.state)).length;
  const errors =
    (batch.failedWithoutRecord || 0) +
    records.filter((u) => failed.has(u.state)).length;
  const skipped =
    (batch.skipped || 0) +
    records.filter((u) => u.state === "cancelled").length;
  const settled = Math.min(batch.total, done + errors + skipped);
  const finished = !batch.transferring && settled === batch.total;
  const waiting = Math.max(0, batch.total - settled);
  return {
    hidden: finished && errors === 0,
    value: settled,
    total: batch.total,
    text: batch.transferring
      ? `传输 ${batch.processed}/${batch.total} · 已入库 ${done} · 失败 ${errors}`
      : `解析入库 ${done}/${batch.total}${waiting ? ` · 处理中 ${waiting}` : ""}${errors ? ` · 失败 ${errors}（请查看下方记录）` : ""}${skipped ? ` · 已取消 ${skipped}` : ""}`,
  };
}
