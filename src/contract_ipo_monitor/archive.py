from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False, default=str) + "\n"


class EvidenceArchive:
    """Content-addressed immutable archive for raw/normalized evidence payloads."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def write(self, source: str, external_id: str, payload: Any, *, observed_at: datetime) -> Path:
        content = _canonical({"source": source, "external_id": external_id, "payload": payload})
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        safe_source = re.sub(r"[^a-zA-Z0-9_.-]", "_", source)
        directory = self.root / safe_source / f"{observed_at:%Y}" / f"{observed_at:%m}" / f"{observed_at:%d}"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{digest}.json"
        if path.exists():
            return path
        fd, temporary = tempfile.mkstemp(prefix=".evidence-", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        return path
