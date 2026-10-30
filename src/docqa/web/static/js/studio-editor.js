import { el, button, errorText } from "./dom.js";
import { api } from "./api.js";

export function editArtifact(artifact, saved) {
  const dialog = el("dialog", "editor-dialog"),
    form = el("form"),
    title = el("input"),
    sections = el("div", "editor-sections"),
    status = el("p", "subtle");
  title.value = artifact.title;
  title.maxLength = 100;
  title.setAttribute("aria-label", "成果标题");
  const editable = ["mindmap", "slides"].includes(artifact.kind);
  sections.hidden = !editable;
  const groups = (artifact.content.tree?.children || []).map((g, i) => ({
    key: String(i),
    title: g.label,
  }));
  const facts = (artifact.content.tree?.children || []).flatMap((g, i) =>
    g.children.map((c) => ({
      id: c.id,
      text: c.text,
      label: c.label || c.text.slice(0, 60),
      group: String(i),
    })),
  );
  let sequence = groups.length;
  const draw = () => {
    sections.replaceChildren(el("h3", "", "主题分组"));
    for (const group of groups) {
      const row = el("div", "editor-group"),
        input = el("input");
      input.value = group.title;
      input.maxLength = 80;
      input.setAttribute("aria-label", "分组标题");
      input.addEventListener("input", () => {
        group.title = input.value;
        sections
          .querySelectorAll(`option[value="${group.key}"]`)
          .forEach((option) => {
            option.textContent = group.title;
          });
      });
      row.append(
        input,
        button("移除分组", () => {
          if (groups.length < 2) return;
          groups.splice(groups.indexOf(group), 1);
          facts
            .filter((f) => f.group === group.key)
            .forEach((f) => {
              f.group = groups[0].key;
            });
          draw();
        }),
      );
      row.lastChild.disabled = groups.length < 2;
      sections.append(row);
    }
    sections.append(
      button("＋ 添加分组", () => {
        groups.push({ key: String(sequence++), title: "新分组" });
        draw();
      }),
      el("h3", "", "要点归属与顺序"),
    );
    facts.forEach((fact, index) => {
      const row = el("div", "editor-fact"),
        label = el("input"),
        select = el("select");
      label.value = fact.label;
      label.maxLength = 60;
      label.setAttribute("aria-label", "要点展示标题");
      label.addEventListener("input", () => {
        fact.label = label.value;
      });
      select.setAttribute("aria-label", "要点所属分组");
      for (const group of groups) {
        const option = el("option", "", group.title);
        option.value = group.key;
        select.append(option);
      }
      select.value = fact.group;
      select.addEventListener("change", () => {
        fact.group = select.value;
      });
      const move = (by) => {
        const target = index + by;
        if (target < 0 || target >= facts.length) return;
        [facts[index], facts[target]] = [facts[target], facts[index]];
        draw();
      };
      row.append(
        label,
        el("p", "subtle", fact.text),
        select,
        button("上移", () => move(-1)),
        button("下移", () => move(1)),
      );
      row.children[row.children.length - 2].disabled = index === 0;
      row.lastChild.disabled = index === facts.length - 1;
      sections.append(row);
    });
  };
  if (editable) draw();
  form.append(
    el("h2", "", "编辑成果编排"),
    el(
      "p",
      "subtle",
      editable
        ? "调整主题、要点标题、分组与顺序，保存为新版本。原有事实正文与引用保持完整；移除分组会把其要点移至第一个分组。编辑标题属于人工编排。"
        : "修改成果标题并保存为新版本，原有内容与引用保持完整。",
    ),
    title,
    sections,
    status,
  );
  const save = button("保存为新版本", async () => {
    const value = { title: title.value.trim() };
    if (editable) {
      if (
        groups.some((g) => !g.title.trim()) ||
        facts.some((f) => !f.label.trim())
      ) {
        status.textContent = "分组和要点标题不能为空。";
        return;
      }
      value.plan = {
        sections: groups
          .map((g) => ({
            title: g.title.trim(),
            claim_ids: facts.filter((f) => f.group === g.key).map((f) => f.id),
          }))
          .filter((g) => g.claim_ids.length),
        claim_labels: Object.fromEntries(
          facts.map((f) => [f.id, f.label.trim()]),
        ),
      };
    }
    save.disabled = true;
    try {
      const result = await api(`/artifacts/${artifact.id}/edits`, {
        method: "POST",
        body: value,
      });
      await saved(result);
      dialog.close();
    } catch (error) {
      status.textContent = errorText(error);
      save.disabled = false;
    }
  });
  const actions = el("div", "editor-actions");
  actions.append(
    button("取消", () => dialog.close()),
    save,
  );
  form.append(actions);
  form.addEventListener("submit", (e) => e.preventDefault());
  dialog.append(form);
  document.body.append(dialog);
  dialog.addEventListener("close", () => dialog.remove(), { once: true });
  dialog.showModal();
}

export async function visualCheck(image, host) {
  let record = await api(`/media/${image.id}/visual-check`, {
    method: "POST",
    body: {},
  });
  const result = el("div", "visual-result");
  host.append(result);
  for (let n = 0; n < 80; n++) {
    result.textContent = "正在使用 VL 看图复核…";
    if (record.state === "ready") {
      result.replaceChildren(el("strong", "", "看图复核结果"));
      for (const check of record.result.checks)
        result.append(
          el(
            "p",
            "",
            `${check.claim_id} · ${{ supported: "原图支持", contradicted: "与原图矛盾", uncertain: "无法判断" }[check.state]}：${check.reason}`,
          ),
          ...check.observations.map((t) => el("p", "subtle", t)),
        );
      result.append(
        el(
          "small",
          "subtle",
          "使用最长边2000px的受控预览图；原回答验证状态不变。",
        ),
      );
      return;
    }
    if (record.state === "failed") {
      result.textContent = errorText({ code: record.error_code });
      return;
    }
    await new Promise((r) => setTimeout(r, 500));
    record = await api(`/visual-checks/${record.id}`);
  }
  result.textContent = "复核仍在后台运行，稍后点击查看结果。";
}
