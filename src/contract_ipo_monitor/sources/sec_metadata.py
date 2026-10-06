"""Exact acceptance metadata from an official EDGAR filing-index document.

The caller owns official-host/accession identity and retrieval. This parser does
not fetch anything and never substitutes the legal Filing Date for Accepted.
SEC describes the separate acceptance clock in its webmaster timestamp FAQ:
https://www.sec.gov/files/about/webmaster-faq.htm
EDGAR's Eastern clock follows standard/daylight time currently in effect:
https://www.sec.gov/rules-regulations/2000/04/rulemaking-edgar-system
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

MAX_INDEX_BYTES = 8_000_000
MAX_INDEX_NODES = 50_000
_VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input",
                   "link", "meta", "param", "source", "track", "wbr"})
_NON_CONTENT = frozenset({"script", "style", "template", "noscript"})
_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


@dataclass
class _Element:
    tag: str
    attrs: list[tuple[str, str | None]] = field(default_factory=list)
    children: list[str | _Element] = field(default_factory=list)
    closed: bool = False

    def has_class(self, name: str) -> bool:
        return any(key == "class" and name in (value or "").split() for key, value in self.attrs)

    def duplicate_attrs(self) -> bool:
        keys = [key for key, _ in self.attrs]
        return len(keys) != len(set(keys))


class _IndexHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Element("#document", closed=True)
        self.stack = [self.root]
        self.nodes = 0

    def handle_starttag(self, tag, attrs):
        self.nodes += 1
        if self.nodes > MAX_INDEX_NODES:
            raise ValueError("SEC filing index exceeds the metadata node bound")
        node = _Element(tag, attrs, closed=tag in _VOID)
        self.stack[-1].children.append(node)
        if tag not in _VOID:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                self.stack[index].closed = True
                del self.stack[index:]
                return

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def _text(node: _Element) -> str:
    words, pending = [], [node]
    while pending:
        current = pending.pop()
        if isinstance(current, str):
            words.append(current)
        elif current.tag not in _NON_CONTENT:
            if current.tag == "br":
                words.append(" ")
            pending.extend(reversed(current.children))
    return " ".join("".join(words).split())


def _metadata_value(root: _Element, wanted: str) -> str | None:
    found, pending = [], [root]
    while pending:
        parent = pending.pop()
        if parent.tag in _NON_CONTENT:
            continue
        for index, node in enumerate(parent.children):
            if isinstance(node, str):
                continue
            pending.append(node)
            if not node.has_class("infoHead"):
                continue
            label = _text(node)
            if not label.casefold().startswith(wanted.casefold()):
                continue
            if (label.casefold() != wanted.casefold() or node.tag != "div" or not node.closed
                    or node.duplicate_attrs()):
                raise ValueError(f"Malformed SEC {wanted} metadata label")
            following = parent.children[index + 1:]
            value = next((child for child in following if not isinstance(child, str) or child.strip()), None)
            if (not isinstance(value, _Element) or value.tag != "div" or not value.has_class("info")
                    or value.has_class("infoHead") or not value.closed or value.duplicate_attrs()):
                raise ValueError(f"Malformed SEC {wanted} metadata value")
            found.append(_text(value))
    if not found:
        return None
    if len(found) != 1:
        raise ValueError(f"Duplicate SEC {wanted} metadata")
    return found[0]


def _field_value(html: str, name: str) -> str | None:
    if not isinstance(html, str) or len(html.encode("utf-8")) > MAX_INDEX_BYTES:
        raise ValueError("SEC filing index must be bounded HTML text")
    parser = _IndexHTML()
    parser.feed(html)
    parser.close()
    return _metadata_value(parser.root, name)


def filing_date_from_filing_index(html: str) -> date | None:
    """Return the independently labelled legal Filing Date; never an instant."""
    value = _field_value(html, "Filing Date")
    if value is None:
        return None
    if not _DATE.fullmatch(value):
        raise ValueError("Malformed SEC Filing Date")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError("Malformed SEC Filing Date") from None


def accepted_at_from_filing_index(html: str) -> datetime | None:
    """Return Accepted as UTC, None when absent, or ValueError when untrustworthy.

    Only the filing-index ``infoHead``/adjacent ``info`` field is recognized.
    Bare prose, scripts, complete-submission headers and Filing Date are not
    substitute authorities. The SEC wall-clock label contains no UTC offset;
    both possible Eastern folds must identify one valid instant before use.
    """
    value = _field_value(html, "Accepted")
    if value is None:
        return None
    if not _TIMESTAMP.fullmatch(value):
        raise ValueError("Malformed SEC Accepted timestamp")
    try:
        local = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        eastern = ZoneInfo("America/New_York")
        instants = set()
        for fold in (0, 1):
            candidate = local.replace(tzinfo=eastern, fold=fold).astimezone(UTC)
            if candidate.astimezone(eastern).replace(tzinfo=None) == local:
                instants.add(candidate)
    except (ValueError, OverflowError):
        raise ValueError("Malformed SEC Accepted timestamp") from None
    if len(instants) != 1:
        raise ValueError("Ambiguous or nonexistent SEC Accepted Eastern timestamp")
    return next(iter(instants))
