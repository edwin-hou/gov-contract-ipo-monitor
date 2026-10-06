"""Private deployment identity; no credentials or personal addresses in source."""
from __future__ import annotations
import json
from email.utils import parseaddr
from pathlib import Path

WORK = Path(__file__).resolve().parent

def settings():
    path = WORK / 'deployment_settings.json'
    if not path.exists():
        # Non-deliverable example identity for isolated tests/source templates.
        return {'base': str(WORK.parent), 'confirmed_recipient': 'reports@example.invalid'}
    if not path.is_file() or path.stat().st_size > 4096:
        raise ValueError('invalid_private_deployment_settings')
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict) or set(value) != {'base', 'confirmed_recipient'}:
        raise ValueError('invalid_private_deployment_settings')
    base, target = Path(value['base']), value['confirmed_recipient']
    if not base.is_absolute() or base.resolve() / 'work' != WORK.resolve():
        raise ValueError('deployment_workspace_mismatch')
    if not isinstance(target, str) or any(c.isspace() for c in target) or len(target) > 254:
        raise ValueError('invalid_confirmed_recipient')
    name, address = parseaddr(target)
    if name or address != target or target.count('@') != 1:
        raise ValueError('invalid_confirmed_recipient')
    return value
