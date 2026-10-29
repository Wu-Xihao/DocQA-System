"""Small, checksummed display metadata. Never used to admit answer evidence."""

from types import SimpleNamespace

from docqa.docbase.retrieval import generation_directory, validate_recall_selection
from docqa.docbase.storage import (
    SCHEMA,
    _checksum,
    _digest,
    _io_path,
    _read_json,
    _safe_path,
    generation_recall_routes,
)
from docqa.domain.errors import DocQAError


def display_catalog(index, options, generation_id=None):
    directory = _io_path(generation_directory(index, generation_id))
    try:
        manifest = _read_json(directory / "manifest.json", maximum=16 * 1024 * 1024)
        if manifest["schema_version"] != SCHEMA or manifest["generation_id"] != directory.name:
            raise ValueError("Display generation identity differs")
        values = {}
        for name in ("members.json", "profiles.json"):
            path = _safe_path(directory, name)
            metadata = manifest["files"][name]
            if path.stat().st_size != metadata["size"] or _checksum(path) != metadata["sha256"]:
                raise ValueError("Display metadata checksum differs")
            values[name] = _read_json(path, maximum=16 * 1024 * 1024)
        members, profiles = values["members.json"], values["profiles.json"]
        if (
            _digest(profiles) != manifest["profiles_sha256"]
            or len(members) != manifest["member_count"]
        ):
            raise ValueError("Display metadata identity differs")
        validate_recall_selection(SimpleNamespace(profiles=profiles), options)
        if profiles["mode"] != options.mode:
            raise DocQAError("PROFILE_MISMATCH", "Knowledge index mode differs")
        documents = [
            {key: item[key] for key in ("doc_id", "source_name", "category", "artifact_id")}
            | {"quality": (item.get("source_quality") or {}).get("index_mode", "complete")}
            for item in members
        ]
        if len({d["doc_id"] for d in documents}) != len(documents) or any(
            not isinstance(value, str) for doc in documents for value in doc.values()
        ):
            raise ValueError("Invalid display documents")
        return dict(
            generation_id=manifest["generation_id"],
            documents=documents,
            categories=sorted({d["category"] for d in documents}),
            chunks=manifest["chunk_count"],
            synthetic=manifest["synthetic"],
            recall_routes=list(generation_recall_routes(profiles)),
            partial_documents=manifest.get("partial_source_count", 0),
            evidence_verified=False,
        )
    except DocQAError:
        raise
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise DocQAError("GENERATION_CORRUPT", "Cannot read checked display metadata") from error
