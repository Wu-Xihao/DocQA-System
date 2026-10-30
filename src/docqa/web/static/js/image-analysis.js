import { el } from "./dom.js";

// VL output is source data. Render as text, never as executable HTML or links.
export function imageAnalysis(image, { compact = false } = {}) {
  const root = el("section", "image-analysis");
  root.append(el("h4", "", "图片释义 · VL"));
  const vision = image.vision;
  if (!vision) {
    const state = image.semantic_state || image.state;
    root.append(
      el(
        "p",
        "subtle",
        state === "failed"
          ? "图片释义暂不可用，可在图片管理中重新解析。"
          : image.analysis_required === false
            ? "该图片作为归档或辅助图保留，尚未生成释义。"
            : "尚未生成图片释义，请先完成图片解析。",
      ),
    );
    return root;
  }
  const sections = [
    ["图中内容", vision.observations || []],
    ["可见元素", (vision.entities || []).map((entity) => entity.label)],
  ];
  const labels = new Map(
    (vision.entities || []).map((entity) => [entity.id, entity.label]),
  );
  const relations = (vision.relations || []).map((relation) => {
    const from = labels.get(relation.source) || relation.source;
    const to = labels.get(relation.target) || relation.target;
    const connection =
      relation.direction === "directed"
        ? " → "
        : relation.direction === "undirected"
          ? " — "
          : " / ";
    return (
      from +
      connection +
      to +
      (relation.label ? `：${relation.label}` : "") +
      (relation.direction === "unknown" ? "（方向未确定）" : "")
    );
  });
  sections.push(
    ["图中关系", relations],
    ["AI 解读", vision.interpretations || []],
    ["待核对", vision.uncertainties || []],
  );
  const available = sections.filter(([, values]) => values.length);
  if (!available.length) {
    root.append(el("p", "subtle", "该图片未识别出可释义的语义内容。"));
    return root;
  }
  const body = el("div", "image-analysis-body");
  for (const [heading, values] of available) {
    const section = el("section");
    section.append(el("h5", "", heading));
    const list = el("ul");
    for (const value of values) list.append(el("li", "", value));
    section.append(list);
    body.append(section);
  }
  if (compact) {
    root.append(el("p", "image-analysis-summary", available[0][1][0]));
    const details = el("details", "image-analysis-details");
    details.append(el("summary", "", "查看完整释义"), body);
    root.append(details);
  } else root.append(body);
  root.append(
    el("small", "subtle", "以上为模型对该图片的释义，可与原图对照。"),
  );
  return root;
}
