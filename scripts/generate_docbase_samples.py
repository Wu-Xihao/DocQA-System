"""Generate non-business native-text sources and source-bound DocBase evaluation cases."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from docx import Document

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from docqa.doc2md.config import ParseOptions  # noqa: E402
from docqa.doc2md.pipeline import parse_document  # noqa: E402
from docqa.domain.documents import DocumentIR  # noqa: E402

SOURCES = (
    (
        "heart_protocol.docx",
        "protocol",
        "心跳协议",
        [
            "SD5993 心跳启动流程：调用 FE_HEART_Start 后发送心跳报文，等待对端确认。",
            "FE_HEART_Stop 用于停止心跳。启用连接前必须校验会话标识；认证失败时不得启动心跳。",
        ],
    ),
    (
        "recovery.docx",
        "operations",
        "故障恢复",
        [
            "心跳超时后执行连接恢复：先断开旧会话，再重新建立连接，最后重发注册请求。",
            "接口 RECOVER_LINK 的重试上限为三次，达到上限后记录告警并交由人工处理。",
        ],
    ),
    (
        "storage.docx",
        "storage",
        "索引存储",
        [
            "DocBase 将本地知识保存为不可变 generation。索引校验成功后才切换当前发布指针。",
            "构建失败时保留旧版本继续查询。向量、BM25 和稀疏检索必须共享同一份 Chunk 集合。",
        ],
    ),
    (
        "tls.docx",
        "security",
        "安全连接",
        [
            "TLS 连接需要校验证书链、证书有效期和服务域名，证书无效时应拒绝连接。",
            "密钥保存在本机环境变量，文档、日志及索引结果不得展示密钥值。",
        ],
    ),
)


def generate_samples(output: Path) -> dict:
    output = output.resolve()
    # Never overwrite an existing hand-edited fixture or parsed corpus.
    if output.exists() and any(output.iterdir()):
        raise FileExistsError("目标目录非空，请使用新的样例目录")
    output.mkdir(parents=True, exist_ok=True)
    source_dir = output / "input"
    source_dir.mkdir(exist_ok=True)
    artifact_root = output / "parsed"
    selected, cases = [], []
    targets = []
    for name, category, title, paragraphs in SOURCES:
        document = Document()
        document.add_heading(title, level=1)
        for paragraph in paragraphs:
            document.add_paragraph(paragraph)
        path = source_dir / name
        document.save(path)
        result = parse_document(
            path,
            artifact_root,
            options=ParseOptions(category=category, office_render="never", ocr_mode="disabled"),
        )
        if result.status != "complete":
            raise RuntimeError("原生测试样例不应产生解析缺口")
        selected.append(str(Path(result.artifact_dir).relative_to(output)).replace("\\", "/"))
        ir = DocumentIR.model_validate_json(
            (Path(result.artifact_dir) / "document.ir.json").read_bytes()
        )
        block = next(block for block in ir.blocks if block.text == paragraphs[0])
        targets.append(
            {
                "doc_id": ir.doc_id,
                "revision_id": ir.revision_id,
                "block_id": block.block_id,
                "component": "block_text",
                "text_start_codepoint": 0,
                "text_end_codepoint": len(block.text),
            }
        )
    questions = [
        "SD5993 的 FE_HEART_Start 如何启动心跳？",
        "心跳超时后如何恢复连接？",
        "DocBase 构建失败后旧版本还能查询吗？",
        "TLS 连接时如何校验证书？",
    ]
    for index, question in enumerate(questions):
        cases.append(
            {
                "question_id": f"Q{index + 1:02d}",
                "question": question,
                "answerable": True,
                "evidence_groups": [[targets[index]]],
            }
        )
    cases.append(
        {
            "question_id": "Q05",
            "question": "外星飞船的曲率引擎燃料配比是多少？",
            "answerable": False,
            "evidence_groups": [],
        }
    )
    sources = output / "sources.json"
    evaluation = output / "evaluation.json"
    sources.write_text(
        json.dumps(
            {"schema_version": "docqa.docbase-sources.v1", "artifacts": selected},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    evaluation.write_text(
        json.dumps(
            {"schema_version": "docqa.docbase-eval.v1", "cases": cases},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (output / "README.md").write_text(
        "# DocBase 人工验收样例\n\n四份原生中文 Word 文档、分类、已校验 doc2md 资产与五个问题。\n"
        "所有内容为人工构造测试资料，不代表真实模型效果或业务金样集。\n"
        "sources.json 使用相对路径，evaluation.json 绑定原文块与字符范围。\n"
        "更改或重新解析原文后需要重新标注对应 revision，不要直接沿用旧评估。\n",
        encoding="utf-8",
    )
    return {
        "input": str(source_dir),
        "parsed": str(artifact_root),
        "sources": str(sources),
        "evaluation": str(evaluation),
        "documents": len(SOURCES),
    }


def main():
    parser = argparse.ArgumentParser(description="生成 DocBase 原生文字、分类和评估测试资料")
    parser.add_argument("--output", type=Path, default=Path("runtime/docbase-manual"))
    arguments = parser.parse_args()
    try:
        result = generate_samples(arguments.output)
    except (OSError, ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
