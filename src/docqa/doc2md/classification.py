"""Deterministic local categorization; explicit categories always take precedence."""

RULES = {
    "研究论文": ["abstract", "references", "doi.org", "论文", "journal", "methodology"],
    "技术规范": ["协议", "接口", "规范", "api reference", "specification", "protocol"],
    "项目汇报": ["汇报", "进展", "项目计划", "progress report", "milestone"],
    "使用指南": ["操作步骤", "使用说明", "安装", "user guide", "installation"],
}


def classify(content, name, requested):
    if requested != "auto":
        return requested
    text = (
        name
        + "\n"
        + "\n".join(block.text for block in content.blocks if block.origin == "native")[:12000]
    ).casefold()
    scores = {
        label: sum(text.count(term.casefold()) for term in terms) for label, terms in RULES.items()
    }
    label = max(scores, key=scores.get)
    chosen = label if scores[label] else "其他资料"
    content.metadata["classification"] = {
        "method": "local_keyword_rules.v1",
        "category": chosen,
        "matched_terms": [t for t in RULES.get(chosen, []) if t.casefold() in text],
    }
    return chosen
