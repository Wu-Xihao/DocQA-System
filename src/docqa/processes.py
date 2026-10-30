"""Fixed internal module commands shared by source and frozen launchers."""

import sys

WORKER_MODULES = frozenset(
    {"docqa.web.worker", "docqa.web.ingestion_worker", "docqa.web.visual"}
)


def module_command(module: str, *arguments: str) -> list[str]:
    if module not in WORKER_MODULES:
        raise ValueError("Unsupported internal worker")
    prefix = [sys.executable, "--internal-worker"] if getattr(sys, "frozen", False) else [
        sys.executable, "-m"
    ]
    return [*prefix, module, *arguments]
