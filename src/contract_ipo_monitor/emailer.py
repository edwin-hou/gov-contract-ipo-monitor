from __future__ import annotations

from datetime import UTC, datetime
from html import escape

from .models import AlertPayload, Candidate, EntityMatch


def _money(value: float | None) -> str:
    return "Not disclosed" if value is None else f"${value:,.0f}"


def _pct(numerator: float | None, denominator: float | None) -> str:
    if numerator is None or denominator in (None, 0):
        return "Unavailable"
    return f"{100 * numerator / denominator:.1f}%"


class EmailRenderer:
    def render(self, candidate: Candidate, entity: EntityMatch, fingerprint: str, *, validated_at: datetime | None = None) -> AlertPayload:
        validated_at = validated_at or datetime.now(UTC)
        c = candidate.contract
        l = candidate.listing
        m = candidate.market
        valuation = (m.market_cap if m else None) or l.proposed_valuation or l.transaction_value or l.max_offering_size
        route = l.route.value
        award_value = c.obligated_amount if c.obligated_amount is not None else c.current_value
        subject = f"[CONFIRMED CONTRACT + IPO] {l.issuer_name} | {c.agency} | {_money(award_value)} | {route}"

        risk_lines = list(candidate.risks) or [
            "Government awards may be modified, terminated, delayed, or funded below the stated ceiling.",
            "Contract ceilings and option years are not current revenue or guaranteed obligations.",
            "Small and pre-public companies may face dilution, financing, liquidity, execution, and listing-completion risk.",
            "Low-priced securities can have wide spreads, low volume, manipulation risk, and substantial loss potential.",
        ]
        source_times = [
            f"Contract published: {c.published_at.isoformat() if c.published_at else 'Not supplied'}",
            f"Contract first observed: {c.retrieved_at.isoformat()}",
            f"Listing filed/announced: {l.filed_at.isoformat()}",
            f"Validation completed: {validated_at.isoformat()}",
        ]
        text = f"""Why this alert fired
Official Class {c.evidence_class.value} evidence confirms award {c.award_id} to {c.recipient_name}; {entity.explanation} An active {route} signal supports a near-term public listing.

Company and listing status
Legal issuer: {l.issuer_name}
CIK: {l.cik or 'Not available'}
Ticker / venue: {(l.ticker or 'Private')} / {(m.venue if m else l.expected_exchange) or 'Not available'}
Listing route: {route}
Listing signal: {l.signal_id} ({l.status})
Underwriter: {l.named_underwriter or 'Not disclosed'}
Expected timing: {l.expected_window_end.isoformat() if l.expected_window_end else 'Not disclosed'}

Contract details
Agency: {c.agency}
Subagency / office: {c.subagency or 'Not supplied'} / {c.office or 'Not supplied'}
Award identifier: {c.award_id}; modification {c.modification_number}
Award date: {c.award_date.isoformat()}
Recipient: {c.recipient_name}
Scope: {c.description}
Obligated amount: {_money(c.obligated_amount)}
Current value: {_money(c.current_value)}
Potential ceiling: {_money(c.ceiling_amount)}
Prime contractor: {'Yes' if c.prime else 'No'}
Contract period: {c.start_date.isoformat() if c.start_date else 'Not supplied'} to {c.end_date.isoformat() if c.end_date else 'Not supplied'}

Small-company screen
Current/proposed price: {_money(m.price if m else l.proposed_price)}
Market cap / valuation proxy: {_money(valuation)}
Shares outstanding: {f'{m.shares_outstanding:,.0f}' if m and m.shares_outstanding else 'Not available'}
Quote time/source: {m.quote_at.isoformat() + ' / ' + m.source if m else 'Private issuer; primary filing proxy used'}

Materiality context
Obligation as % of market cap/valuation: {_pct(c.obligated_amount, valuation)}
Ceiling as % of market cap/valuation: {_pct(c.ceiling_amount, valuation)}
Obligation as % of latest annual revenue: {_pct(c.obligated_amount, candidate.annual_revenue)}
Ceiling as % of latest annual revenue: {_pct(c.ceiling_amount, candidate.annual_revenue)}

Risk report
- """ + "\n- ".join(risk_lines) + f"""

Evidence and timestamps
Contract source: {c.source_url}
Listing source: {l.source_url}
Entity match method: {entity.method}
""" + "\n".join(source_times) + """
SMTP acceptance: recorded after the mail server accepts the message; inbox delivery is not guaranteed.

Research alert only; not investment advice. Verify every primary source and assess suitability independently before trading.
"""
        html = "<html><body>" + "".join(
            f"<p>{escape(line)}</p>" if line and not line.endswith(":") else f"<h3>{escape(line[:-1])}</h3>"
            for line in text.splitlines()
        ) + "</body></html>"
        return AlertPayload(
            fingerprint=fingerprint,
            subject=subject,
            text_body=text,
            html_body=html,
            company_name=l.issuer_name,
            award_id=c.award_id,
            signal_id=l.signal_id,
        )
