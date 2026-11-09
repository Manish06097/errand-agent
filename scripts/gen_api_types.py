"""Generate web/lib/api-types.gen.ts from the API's OpenAPI document (architecture 17.4).

    python scripts/gen_api_types.py          write the committed file
    python scripts/gen_api_types.py --check  regenerate to a temp file and diff (make check)

The document is `errand.api.app.app.openapi()`, dumped with the dev routes registered
(ERRAND_DEV=1 is forced here), so the file is the same whoever runs it and the dev
drawer's types exist. The web code imports card and event types only from that file; a
changed Pydantic model with stale types fails `--check`.
"""

from __future__ import annotations

import difflib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TARGET = REPO / "web" / "lib" / "api-types.gen.ts"


def openapi_json() -> str:
    # Set before the app module is imported: routes are registered at creation.
    os.environ["ERRAND_DEV"] = "1"
    os.environ.setdefault("ERRAND_LANGFUSE", "off")
    sys.path.insert(0, str(REPO / "backend"))
    from errand.api.app import app

    return json.dumps(app.openapi(), indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def generate(out: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        spec = Path(tmp) / "openapi.json"
        spec.write_text(openapi_json(), encoding="utf-8")
        subprocess.run(
            [
                "pnpm",
                "-C",
                str(REPO / "web"),
                "exec",
                "openapi-typescript",
                str(spec),
                "-o",
                str(out),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )


def main(argv: list[str]) -> int:
    if "--check" not in argv:
        TARGET.parent.mkdir(parents=True, exist_ok=True)
        generate(TARGET)
        print(f"wrote {TARGET.relative_to(REPO)}")
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        fresh = Path(tmp) / "api-types.gen.ts"
        generate(fresh)
        current = TARGET.read_text(encoding="utf-8") if TARGET.is_file() else ""
        new = fresh.read_text(encoding="utf-8")
    if current == new:
        print("api types: up to date")
        return 0
    diff = difflib.unified_diff(
        current.splitlines(), new.splitlines(), "committed", "regenerated", lineterm="", n=2
    )
    print("\n".join(list(diff)[:60]), file=sys.stderr)
    print(
        "api types: web/lib/api-types.gen.ts is stale; run `uv run --frozen --project backend "
        "python scripts/gen_api_types.py` and commit it",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
