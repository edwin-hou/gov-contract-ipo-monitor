"""Issuer authority from official filing metadata, independent of archive paths.

The caller verifies retrieval/accession identity and bounds primary-document bytes.
An index lists all joint filers; undimensioned DEI facts identify the base
registrant, not every joint registrant. The caller must check a returned primary
identity against its independently observed filing-index/catalogue filers.
"""
from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ET
from urllib.parse import parse_qs, urljoin, urlparse

from .sec_metadata import MAX_INDEX_BYTES, _Element, _IndexHTML, _NON_CONTENT, _text

_ACCESSION = re.compile(r"[0-9]{10}-[0-9]{2}-[0-9]{6}\Z")
_CIK = re.compile(r"[0-9]{1,10}\Z")
_REGISTRATION = re.compile(r"(?:333|33)-[0-9]+(?:-[0-9]+)?\Z")
_DEI = re.compile(r"http://xbrl\.sec\.gov/dei/[0-9]{4}(?:-[0-9]{2}-[0-9]{2})?\Z")
_IX = "http://www.xbrl.org/2013/inlineXBRL"
_XBRLI = "http://www.xbrl.org/2003/instance"
_XHTML = "http://www.w3.org/1999/xhtml"
_FIELDS = frozenset({"EntityCentralIndexKey", "EntityRegistrantName", "DocumentType"})
_XML_NON_CONTENT = _NON_CONTENT | {f"{{{_XHTML}}}{tag}" for tag in _NON_CONTENT}


def _cik(value: str) -> str | None:
    return f"{int(value):010d}" if _CIK.fullmatch(value) and int(value) > 0 else None


def _elements(root: _Element):
    pending = [root]
    while pending:
        current = pending.pop()
        if current.tag in _NON_CONTENT:
            continue
        yield current
        pending.extend(reversed([child for child in current.children if isinstance(child, _Element)]))


def filing_index_filers(index_html: str, accession: str) -> list[dict[str, str]]:
    """Read every exact companyInfo/companyName (Filer) identity, in source order.

    A document-folder CIK and the first company block do not establish a primary
    issuer. URLs are constructed from each verified identity and this accession.
    Malformed filer metadata fails visibly rather than returning a partial list.
    """
    if not isinstance(accession, str) or not _ACCESSION.fullmatch(accession):
        raise ValueError("Invalid SEC filing accession")
    if not isinstance(index_html, str) or len(index_html.encode("utf-8")) > MAX_INDEX_BYTES:
        raise ValueError("SEC filing index must be bounded HTML text")
    parser = _IndexHTML()
    parser.feed(index_html)
    parser.close()
    identities: dict[str, dict[str, str]] = {}
    for block in _elements(parser.root):
        if not block.has_class("companyInfo"):
            continue
        spans = [node for node in _elements(block) if node.has_class("companyName")]
        filer_spans = [node for node in spans if "(Filer)" in _text(node)]
        if not filer_spans:
            continue
        if (block.tag != "div" or not block.closed or block.duplicate_attrs()
                or len(spans) != 1 or len(filer_spans) != 1):
            raise ValueError("Malformed SEC filing-index filer block")
        span = filer_spans[0]
        if span.tag != "span" or not span.closed or span.duplicate_attrs():
            raise ValueError("Malformed SEC filing-index filer name")
        label = re.fullmatch(r"(.+?)\s+\(Filer\)\s+CIK:\s*([0-9]{1,10})\s*\(see all company filings\)", _text(span))
        if label is None or not label[1].strip():
            raise ValueError("Malformed SEC filing-index filer identity")
        links = []
        for node in _elements(span):
            if node.tag != "a":
                continue
            if not node.closed or node.duplicate_attrs():
                raise ValueError("Malformed SEC filing-index filer link")
            href = dict(node.attrs).get("href")
            if not href:
                continue
            parsed = urlparse(urljoin("https://www.sec.gov", href))
            query = parse_qs(parsed.query, keep_blank_values=True)
            if (parsed.scheme != "https" or parsed.hostname not in {"sec.gov", "www.sec.gov"}
                    or parsed.username or parsed.password or parsed.port not in (None, 443)
                    or parsed.fragment or parsed.path != "/cgi-bin/browse-edgar"
                    or query.get("action") != ["getcompany"] or len(query.get("CIK", [])) != 1):
                raise ValueError("Invalid SEC filing-index filer CIK link")
            cik = _cik(query["CIK"][0])
            if cik is None or _cik(label[2]) != cik:
                raise ValueError("Conflicting SEC filing-index filer CIK")
            links.append(cik)
        if len(links) != 1:
            raise ValueError("SEC filing-index filer has no unique CIK link")
        cik = links[0]
        identity = {"cik": cik, "issuer_name": label[1].strip(), "source_url":
                    f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{accession}-index.htm"}
        if cik in identities and identities[cik] != identity:
            raise ValueError("Conflicting SEC filing-index filer names")
        identities[cik] = identity
    return list(identities.values())


def _value(element: ET.Element) -> str:
    return " ".join("".join(element.itertext()).split())


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _effect_issuer(root: ET.Element) -> dict[str, str] | None:
    # EFFECT pairs each entity with its own file number. Multiple filers cannot
    # be collapsed into the last entity or mixed into one registration scope.
    if root.tag != "edgarSubmission":
        return None
    types = root.findall("submissionType")
    if types and (len(types) != 1 or _value(types[0]) != "EFFECT"):
        return None
    data = root.findall("effectiveData")
    filers = list(root.iter("filer"))
    if len(data) != 1 or len(filers) != 1 or filers[0] not in list(data[0]):
        return None
    filer = filers[0]
    ciks, names = filer.findall("cik"), filer.findall("entityName")
    if len(ciks) != 1 or len(names) != 1 or list(ciks[0]) or list(names[0]):
        return None
    cik, name = _cik(_value(ciks[0])), _value(names[0])
    if cik is None or not name:
        return None
    return {"cik": cik, "issuer_name": name}


def effect_filer_registrations(text: str) -> list[dict[str, str]]:
    """Preserve each EFFECT filer/registration pair, including joint filers.

    Only direct source fields within one filer can be paired. Malformed or
    conflicting metadata invalidates the complete result, so a valid first
    filer cannot hide an unparsed later filer or authorize an issuer-wide notice.
    Primary-document byte bounds remain the caller's responsibility.
    """
    if not isinstance(text, str) or re.search(r"<!ENTITY\b", text, re.I):
        return []
    try:
        root = ET.fromstring(text)
    except (ET.ParseError, ValueError):
        return []
    if root.tag != "edgarSubmission":
        return []
    types, data = root.findall("submissionType"), root.findall("effectiveData")
    if len(types) != 1 or list(types[0]) or _value(types[0]) != "EFFECT" or len(data) != 1:
        return []
    filers = data[0].findall("filer")
    if not filers or len(list(root.iter("filer"))) != len(filers):
        return []
    result: dict[tuple[str, str], dict[str, str]] = {}
    names_by_cik: dict[str, str] = {}
    for filer in filers:
        fields = {name: filer.findall(name) for name in ("cik", "entityName", "fileNumber")}
        if any(len(elements) != 1 or list(elements[0]) for elements in fields.values()):
            return []
        cik = _cik(_value(fields["cik"][0]))
        name, registration = _value(fields["entityName"][0]), _value(fields["fileNumber"][0])
        if cik is None or not name or not _REGISTRATION.fullmatch(registration):
            return []
        if cik in names_by_cik and names_by_cik[cik] != name:
            return []
        names_by_cik[cik] = name
        result[(cik, registration)] = {"cik": cik, "issuer_name": name, "registration_id": registration}
    return list(result.values())


def primary_issuer(text: str, form_type: str) -> dict[str, str] | None:
    """Return a singular EFFECT filer or coherent undimensioned DEI base issuer.

    Missing, malformed, dimensioned-only, or contradictory metadata is not an
    attribution. Inline fact QName prefixes must resolve to a real SEC DEI
    namespace; the iXBRL elements and CIK context use their exact namespaces.
    """
    if not isinstance(text, str) or not isinstance(form_type, str):
        return None
    # Primary bytes are bounded by the collector. Declared entities are not
    # needed for issuer facts and must not expand into unbounded metadata.
    if re.search(r"<!ENTITY\b", text, re.I):
        return None
    scopes: list[dict[str, str]] = [{}]
    pending_ns: list[tuple[str, str]] = []
    skipped: list[bool] = [False]
    facts: list[tuple[str, str | None, str]] = []
    contexts: list[ET.Element] = []
    fact_fields: dict[int, str] = {}
    try:
        parsed = ET.iterparse(io.StringIO(text), events=("start-ns", "start", "end"))
        for event, item in parsed:
            if event == "start-ns":
                pending_ns.append(item)
                continue
            if event == "start":
                scope = dict(scopes[-1]) if pending_ns else scopes[-1]
                scope.update(pending_ns)
                pending_ns.clear()
                scopes.append(scope)
                skip = skipped[-1] or item.tag in _XML_NON_CONTENT
                skipped.append(skip)
                if skip:
                    continue
                if item.tag == f"{{{_XBRLI}}}context":
                    contexts.append(item)
                if item.tag == f"{{{_IX}}}nonNumeric":
                    parts = item.get("name", "").split(":")
                    if len(parts) == 2 and _DEI.fullmatch(scope.get(parts[0], "")) and parts[1] in _FIELDS:
                        fact_fields[id(item)] = parts[1]
            else:
                field = fact_fields.get(id(item))
                if field is not None:
                    if item.get("continuedAt") or item.get("{http://www.w3.org/2001/XMLSchema-instance}nil") in {"true", "1"}:
                        return None
                    facts.append((field, item.get("contextRef"), _value(item)))
                scopes.pop()
                skipped.pop()
        root = parsed.root
    except (ET.ParseError, ValueError):
        return None
    form = form_type.strip().upper()
    if form == "EFFECT":
        return _effect_issuer(root)
    if root.tag not in {"html", f"{{{_XHTML}}}html"}:
        return None
    context_map: dict[str, tuple[str | None, bool]] = {}
    for context in contexts:
        identifier = context.get("id")
        if not identifier or identifier in context_map:
            return None
        dimensioned = any(_local(node.tag) in {"segment", "scenario", "explicitMember", "typedMember"}
                          or "dimension" in node.attrib for node in context.iter())
        entities = context.findall(f"{{{_XBRLI}}}entity")
        identifiers = entities[0].findall(f"{{{_XBRLI}}}identifier") if len(entities) == 1 else []
        cik = (_cik(_value(identifiers[0])) if len(identifiers) == 1
               and identifiers[0].get("scheme") == "http://www.sec.gov/CIK" and not list(identifiers[0]) else None)
        context_map[identifier] = (cik, dimensioned)
    grouped: dict[str, dict[str, set[str]]] = {}
    for field, context_ref, value in facts:
        if context_ref not in context_map:
            return None
        _context_cik, dimensioned = context_map[context_ref]
        if dimensioned:
            continue
        grouped.setdefault(context_ref, {}).setdefault(field, set()).add(value)
    candidates = set()
    for context_ref, fields in grouped.items():
        if set(fields) != _FIELDS or any(len(values) != 1 for values in fields.values()):
            return None
        cik = _cik(next(iter(fields["EntityCentralIndexKey"])))
        name = next(iter(fields["EntityRegistrantName"]))
        if (cik is None or cik != context_map[context_ref][0] or not name
                or next(iter(fields["DocumentType"])) != form):
            return None
        candidates.add((cik, name))
    if len(candidates) != 1:
        return None
    cik, name = candidates.pop()
    return {"cik": cik, "issuer_name": name}
