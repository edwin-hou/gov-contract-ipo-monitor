from __future__ import annotations

import re

from pydantic import BaseModel


class RiskFinding(BaseModel):
    category: str
    severity: str
    finding: str


class RiskAnalyzer:
    PATTERNS: tuple[tuple[str, str, str], ...] = (
        ("going_concern", "high", r"substantial doubt.{0,120}going concern|ability to continue as a going concern"),
        ("dilution", "high", r"convertible (?:notes?|debt)|warrants?|may cause dilution|dilutive"),
        ("reverse_split", "high", r"reverse (?:stock )?split|1-for-\d+"),
        ("cash_runway", "high", r"insufficient cash|cash.{0,80}(?:months?|runway)|need additional capital"),
        ("debt", "medium", r"default under|debt covenant|senior secured|highly leveraged"),
        ("auditor", "high", r"auditor resignation|dismissed our independent|material weakness"),
        ("reporting", "high", r"delinquent filer|late filing|not timely file"),
        ("customer_concentration", "medium", r"customer concentration|single customer.{0,80}%|major customer"),
        ("spac_redemption", "medium", r"redemption rate|public shareholders.{0,80}redeem"),
        ("liquidity", "high", r"limited trading market|low trading volume|wide bid.ask spread|penny stock rules"),
    )

    def from_filing_text(self, text: str) -> list[RiskFinding]:
        findings: list[RiskFinding] = []
        for category, severity, pattern in self.PATTERNS:
            match = re.search(pattern, text, flags=re.I | re.S)
            if match:
                excerpt = re.sub(r"\s+", " ", text[max(0, match.start() - 60): match.end() + 120]).strip()
                findings.append(RiskFinding(category=category, severity=severity, finding=excerpt[:500]))
        return findings
