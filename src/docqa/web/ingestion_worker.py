"""One owned Service tick; never competes with an existing single-writer worker."""

import json
import sys
from pathlib import Path

from docqa.domain.errors import DocQAError
from docqa.service.config import ServiceOptions
from docqa.service.pipeline import Service


def main():
    try:
        options = ServiceOptions.model_validate(json.loads(Path(sys.argv[1]).read_bytes()))
        Service(sys.argv[2], options=options).run_once()
        return 0
    except DocQAError as error:
        return 75 if error.code == "SERVICE_ALREADY_RUNNING" else 4


if __name__ == "__main__":
    raise SystemExit(main())
