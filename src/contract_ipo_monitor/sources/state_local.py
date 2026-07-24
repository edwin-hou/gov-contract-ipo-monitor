from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class AdapterStatus:
    name: str
    description: str
    enabled: bool
    authoritative: bool = True


class AdapterInventory:
    def __init__(self):
        self._adapters: dict[str, AdapterStatus] = {}

    def register(self, name: str, description: str, *, enabled: bool, authoritative: bool = True) -> None:
        self._adapters[name] = AdapterStatus(name, description, enabled, authoritative)

    @property
    def nationwide_complete(self) -> bool:
        return False

    def report(self) -> list[dict[str, object]]:
        return [asdict(self._adapters[name]) for name in sorted(self._adapters)]
