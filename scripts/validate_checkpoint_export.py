"""Publish only a complete, verified checkpoint; retain real job failures."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from contract_ipo_monitor.checkpoint import validate_export


def verify(directory: Path, collection_outcome: str) -> dict | None:
    if not (directory / "checkpoint.json").exists():
        if collection_outcome == "success":
            raise ValueError("Successful collection did not publish a complete checkpoint")
        print("No complete checkpoint export; the preceding durable artifact is retained.")
        return None
    return validate_export(directory)


if __name__ == "__main__":
    manifest = verify(Path("work/checkpoint"), os.environ.get("COLLECTION_OUTCOME", "unknown"))
    if manifest:
        print("Verified checkpoint export: " + json.dumps(manifest, sort_keys=True))
        baseline = Path("work/checkpoint-baseline.json")
        if baseline.exists():
            from contract_ipo_monitor.checkpoint_history import verify_history
            snapshot = Path("work/checkpoint/monitor.db")
            with snapshot.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != manifest["database"]["sha256"]:
                    raise ValueError("History verification snapshot does not match the exported database")
            verification = verify_history(json.loads(baseline.read_text(encoding="utf-8")), snapshot)
            print("Verified retained report history: " + json.dumps(verification, sort_keys=True))
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a", encoding="utf-8") as stream:
            stream.write("valid=" + ("true" if manifest else "false") + "\n")
