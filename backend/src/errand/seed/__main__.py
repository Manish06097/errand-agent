"""python -m errand.seed {memory_demo|memory_rich_v1} --data-dir PATH"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

from errand.config import REPO_ROOT, MemorySettings
from errand.seed.base import KEY_FILE, dumps_manifest
from errand.seed.memory_demo import build_demo
from errand.seed.memory_rich import build_rich

DEFAULT_MANIFEST = REPO_ROOT / "backend" / "evals" / "fixtures" / "memory_rich_v1.json"


async def run(name: str, data_dir: Path, manifest: Path | None) -> str:
    if name == "memory_demo":
        seeded = await build_demo(data_dir)
        await seeded.db.close()
        return f"memory_demo: 8 memories in {data_dir}"
    seeded, doc = await build_rich(data_dir)
    await seeded.db.close()
    target = manifest or DEFAULT_MANIFEST
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(dumps_manifest(doc), encoding="utf-8")
    return (
        f"memory_rich_v1: {len(doc['rows'])} memories, {doc['messages']} messages "
        f"in {data_dir}; manifest {target}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    names = MemorySettings().seed_names
    parser = argparse.ArgumentParser(prog="errand.seed", description="Seed agent.sqlite.")
    parser.add_argument("name", choices=names)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, default=None, help="memory_rich_v1 only")
    args = parser.parse_args(argv)
    for name in ("agent.sqlite", KEY_FILE):
        if (args.data_dir / name).exists():
            print(
                f"errand.seed: {args.data_dir}/{name} already exists; use a new directory",
                file=sys.stderr,
            )
            return 2
    print(asyncio.run(run(args.name, args.data_dir, args.manifest)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
