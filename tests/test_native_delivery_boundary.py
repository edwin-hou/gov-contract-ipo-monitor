"""The owning Hermes mail runtime reads bytes; it does not prepare reports."""
from __future__ import annotations

import ast
import json
from pathlib import Path
import subprocess
import sys

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "src"


@pytest.mark.parametrize("module", ["notifications.py", "gmail_delivery.py"])
def test_native_delivery_modules_retain_python311_grammar(module):
    path = SOURCE / "contract_ipo_monitor" / module
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=(3, 11))


def test_sealed_outbox_delivery_does_not_import_collector_or_report_modules(tmp_path):
    # A fresh isolated interpreter prevents another test's imports from masking
    # an eager dependency. Exercise publication, leasing, exact MIME readback
    # matching and saved history with a provider double, without any network.
    script = r'''
import hashlib
import importlib.abc
import json
from datetime import UTC, datetime, timedelta
from email import policy
from email.message import EmailMessage
from pathlib import Path
import sys

sys.path.insert(0, sys.argv[1])
allowed = {"contract_ipo_monitor", "contract_ipo_monitor.notifications",
           "contract_ipo_monitor.gmail_delivery"}
class DeliveryOnly(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith("contract_ipo_monitor") and fullname not in allowed:
            raise AssertionError("Native delivery imported collector/report code: " + fullname)
sys.meta_path.insert(0, DeliveryOnly())
from contract_ipo_monitor.notifications import EmailOutbox
from contract_ipo_monitor.gmail_delivery import GmailOAuthTransport, message_content_sha256, MIME_TREE_VERSION

now = datetime(2026, 10, 7, tzinfo=UTC)
address = "preview@example.invalid"
message = EmailMessage(policy=policy.SMTP)
message["From"], message["To"] = address, address
message["Subject"] = "Sealed native-runtime fixture"
message["Message-ID"] = "<sealed-delivery@ipo-monitor.local>"
message.set_content("Already prepared report bytes. No alert, order or holding is assumed.")
message.add_alternative("<p>Already prepared HTML report.</p>", subtype="html")
message.add_attachment(b"%PDF-sealed-fixture", maintype="application", subtype="pdf", filename="research-report.pdf")
message.add_attachment(b'{"trade_ideas":[]}', maintype="application", subtype="json", filename="research-report.json")
raw = message.as_bytes()
outbox = EmailOutbox(Path(sys.argv[2]))
identifier = outbox.enqueue("native-fixture", raw, address, now=now, expires_at=now + timedelta(minutes=5))
class ProviderDouble:
    def deliver(self, original, recipient, identity):
        assert original == raw and recipient == address and identity == message["Message-ID"]
        digest = hashlib.sha256(original).hexdigest()
        return {"gmail_message_id":"fixture-one", "thread_id":"fixture-thread", "delivered_label":"INBOX",
                "provider_accepted_at":now.isoformat(), "verified_at":now.isoformat(), "recipient":address,
                "sender":address, "rfc822_id":identity, "raw_content_sha256":digest,
                "readback_raw_sha256":digest, "content_sha256":message_content_sha256(original),
                "content_sha256_version":MIME_TREE_VERSION}
assert outbox.deliver_once(ProviderDouble(), now=now)["status"] == "sent"
row = outbox.get(identifier)
assert row["raw_message"] == raw
assert outbox.historical_receipt_matches(row, json.loads(row["receipt_json"]))
project_modules = sorted(name for name in sys.modules if name.startswith("contract_ipo_monitor"))
assert set(project_modules) == allowed
assert "reportlab" not in sys.modules
print(json.dumps({"project_modules":project_modules, "sealed_delivery_verified":True}))
'''
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    result = subprocess.run([sys.executable, "-I", "-X", "utf8", "-c", script,
                             str(SOURCE), str(tmp_path / "native-mail.sqlite3")],
                            shell=False, capture_output=True, text=True, encoding="utf-8",
                            timeout=30, creationflags=flags)
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["sealed_delivery_verified"] is True
