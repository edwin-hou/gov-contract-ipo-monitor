import hashlib
from pathlib import Path

import pytest

from contract_ipo_monitor.sources.sec_issuer import effect_filer_registrations, filing_index_filers, primary_issuer
from contract_ipo_monitor.sources.sec_metadata import MAX_INDEX_BYTES


FIXTURES = Path(__file__).parent / "fixtures"
ACCESSION = "0001193125-26-408740"
XBRLI = "http://www.xbrl.org/2003/instance"
IX = "http://www.xbrl.org/2013/inlineXBRL"
DEI = "http://xbrl.sec.gov/dei/2026"


def filer(name="Example Inc.", cik="42", *, href=None):
    link = href or f"/cgi-bin/browse-edgar?CIK={cik}&amp;action=getcompany"
    return (f'<div class="companyInfo"><span class="companyName">{name} (Filer) '
            f'<acronym title="Central Index Key">CIK</acronym>: '
            f'<a href="{link}">{cik} (see all company filings)</a></span></div>')


def context(identity="base", cik="42", dimension=""):
    return (f'<xbrli:context id="{identity}"><xbrli:entity>'
            f'<xbrli:identifier scheme="http://www.sec.gov/CIK">{cik}</xbrli:identifier>'
            f'{dimension}</xbrli:entity><xbrli:period><xbrli:instant>2026-09-30'
            '</xbrli:instant></xbrli:period></xbrli:context>')


def fact(name, value, identity="base", prefix="dei"):
    return f'<ix:nonNumeric name="{prefix}:{name}" contextRef="{identity}">{value}</ix:nonNumeric>'


def facts(identity="base", cik="42", name="Example Inc.", form="8-K"):
    return (fact("EntityCentralIndexKey", cik, identity) + fact("EntityRegistrantName", name, identity)
            + fact("DocumentType", form, identity))


def inline(*, extra="", dimension="", namespaces=None, metadata=None, contexts=None):
    uris = {"xbrli": XBRLI, "ix": IX, "dei": DEI, "xbrldi": "http://xbrl.org/2006/xbrldi"}
    uris.update(namespaces or {})
    declarations = " ".join(f'xmlns:{prefix}="{uri}"' for prefix, uri in uris.items())
    return f'<html {declarations}><body>{contexts if contexts is not None else context(dimension=dimension)}{metadata if metadata is not None else facts()}{extra}</body></html>'


def effect(filers='<filer><cik>42</cik><entityName>Example Inc.</entityName><fileNumber>333-42</fileNumber></filer>'):
    return f'<edgarSubmission><submissionType>EFFECT</submissionType><effectiveData>{filers}</effectiveData></edgarSubmission>'


@pytest.mark.parametrize("name,size,digest", [
    ("sec_joint_corteva_eidp_8k_index_20260930.htm", 10618, "1b81c0e6e934b04966499b9415b2f895215c981d738e21a3cd60f4f56265c1f8"),
    ("sec_joint_corteva_eidp_8k_primary_20260930.htm", 35682, "05824d1a0803717cb4caa968f97b3b60db54e80b66f36f8c0ca452362f96696f"),
    ("sec_joint_cubebio_effect_primary_20260930.xml", 762, "6216a0f0a1db91be223294048f2ddf11161e162172906184bc3e89d8d3101226"),
    ("sec_joint_cubebio_effect_index_20260930.htm", 8576, "f6afac934c1c8f9b9a71ac6c694995134ade2fdef74dd3196f1496c5859b7bb4"),
])
def test_live_public_fixtures_preserve_recorded_source_bytes(name, size, digest):
    raw = (FIXTURES / name).read_bytes()
    assert len(raw) == size
    assert hashlib.sha256(raw).hexdigest() == digest


def test_live_joint_index_lists_both_filers_and_primary_metadata_resolves_base_corteva():
    # Public official documents retrieved on 2026-10-07 with the configured
    # truthful SEC identity. Filenames and disk receipts contain no credentials.
    index = (FIXTURES / "sec_joint_corteva_eidp_8k_index_20260930.htm").read_text(encoding="utf-8")
    primary = (FIXTURES / "sec_joint_corteva_eidp_8k_primary_20260930.htm").read_text(encoding="utf-8")
    assert filing_index_filers(index, ACCESSION) == [
        {"cik": "0000030554", "issuer_name": "EIDP, Inc.", "source_url":
         f"https://www.sec.gov/Archives/edgar/data/30554/{ACCESSION.replace('-', '')}/{ACCESSION}-index.htm"},
        {"cik": "0001755672", "issuer_name": "Corteva, Inc.", "source_url":
         f"https://www.sec.gov/Archives/edgar/data/1755672/{ACCESSION.replace('-', '')}/{ACCESSION}-index.htm"},
    ]
    assert primary_issuer(primary, "8-K") == {"cik": "0001755672", "issuer_name": "Corteva, Inc."}


def test_live_joint_effect_cannot_choose_last_filer_or_mix_registration_pairs():
    primary = (FIXTURES / "sec_joint_cubebio_effect_primary_20260930.xml").read_text(encoding="utf-8")
    assert primary_issuer(primary, "EFFECT") is None
    assert effect_filer_registrations(primary) == [
        {"cik": "0002058261", "issuer_name": "CubeBio Holdings Ltd", "registration_id": "333-298262"},
        {"cik": "0002058594", "issuer_name": "Cubebio Co., Ltd", "registration_id": "333-298262-01"},
    ]


def test_live_joint_effect_index_preserves_both_authoritative_filer_identities():
    index = (FIXTURES / "sec_joint_cubebio_effect_index_20260930.htm").read_text(encoding="utf-8")
    assert [(filer["cik"], filer["issuer_name"]) for filer in filing_index_filers(index, "9999999995-26-003117")] == [
        ("0002058261", "CubeBio Holdings Ltd"), ("0002058594", "Cubebio Co., Ltd"),
    ]


def test_exact_index_filer_identity_preserves_name_and_normalizes_cik():
    assert filing_index_filers(filer("Example (Holdings) &amp; Co.", "42"), ACCESSION)[0] == {
        "cik": "0000000042", "issuer_name": "Example (Holdings) & Co.",
        "source_url": f"https://www.sec.gov/Archives/edgar/data/42/{ACCESSION.replace('-', '')}/{ACCESSION}-index.htm",
    }


@pytest.mark.parametrize("tag", ["script", "style", "template", "noscript"])
def test_index_non_content_cannot_supply_filer_authority(tag):
    assert filing_index_filers(f'<{tag}>{filer("Spoof", "99")}</{tag}>' + filer(), ACCESSION)[0]["cik"] == "0000000042"
    assert filing_index_filers(f'<{tag}>{filer()}</{tag}>', ACCESSION) == []


def test_index_subject_and_prose_are_not_filers():
    assert filing_index_filers(filer().replace("(Filer)", "(Subject)") + '<p>Example (Filer) CIK: 42</p>', ACCESSION) == []


def test_index_same_identity_deduplicates_but_conflicting_name_fails():
    assert len(filing_index_filers(filer() + filer(), ACCESSION)) == 1
    with pytest.raises(ValueError, match="Conflicting"):
        filing_index_filers(filer() + filer("Different Inc."), ACCESSION)


@pytest.mark.parametrize("href", [
    "https://example.com/cgi-bin/browse-edgar?CIK=42&amp;action=getcompany",
    "https://www.sec.gov@example.com/cgi-bin/browse-edgar?CIK=42&amp;action=getcompany",
    "http://www.sec.gov/cgi-bin/browse-edgar?CIK=42&amp;action=getcompany",
    "/cgi-bin/browse-edgar?CIK=99&amp;action=getcompany",
    "/cgi-bin/browse-edgar?CIK=42&amp;CIK=99&amp;action=getcompany",
    "/cgi-bin/browse-edgar?CIK=42&amp;CIK=&amp;action=getcompany",
    "/cgi-bin/browse-edgar?CIK=42&amp;action=getcompany#other",
])
def test_index_cik_link_must_be_official_unique_and_agree_with_label(href):
    with pytest.raises(ValueError):
        filing_index_filers(filer(href=href), ACCESSION)


@pytest.mark.parametrize("html", [
    filer().replace('</span>', ''),
    filer().replace('class="companyName"', 'class="companyName" class="companyName"'),
    filer(cik="0"),
    filer().replace('(see all company filings)', '(other text)'),
])
def test_index_malformed_metadata_is_not_partial_success(html):
    with pytest.raises(ValueError):
        filing_index_filers(html, ACCESSION)


def test_index_inputs_are_bounded_and_accession_is_exact():
    with pytest.raises(ValueError, match="bounded"):
        filing_index_filers("x" * (MAX_INDEX_BYTES + 1), ACCESSION)
    with pytest.raises(ValueError, match="accession"):
        filing_index_filers(filer(), "../" + ACCESSION)


def test_primary_identity_requires_same_undimensioned_context_and_matching_form():
    assert primary_issuer(inline(), "8-K") == {"cik": "0000000042", "issuer_name": "Example Inc."}
    assert primary_issuer(inline(), "S-1") is None
    assert primary_issuer(inline(metadata=facts(form="8-K/A")), "8-K") is None


def test_primary_dei_namespace_alias_is_resolved_instead_of_assuming_prefix():
    xml = inline().replace('xmlns:dei=', 'xmlns:entity=').replace('name="dei:', 'name="entity:')
    assert primary_issuer(xml, "8-K")["cik"] == "0000000042"


@pytest.mark.parametrize("prefix,uri", [
    ("dei", "https://example.com/dei/2026"),
    ("dei", DEI + "/spoof"),
    ("ix", "https://example.com/inlineXBRL"),
    ("xbrli", "https://example.com/instance"),
])
def test_primary_namespace_spoofs_are_not_metadata(prefix, uri):
    assert primary_issuer(inline(namespaces={prefix: uri}), "8-K") is None


@pytest.mark.parametrize("dimension", [
    '<xbrli:segment><xbrldi:explicitMember dimension="dei:LegalEntityAxis">company:OtherMember</xbrldi:explicitMember></xbrli:segment>',
    '<xbrli:scenario/>',
    '<xbrldi:typedMember dimension="dei:LegalEntityAxis"><entity>Other</entity></xbrldi:typedMember>',
])
def test_dimensioned_only_primary_facts_cannot_establish_base_issuer(dimension):
    assert primary_issuer(inline(dimension=dimension), "8-K") is None


def test_dimensioned_joint_registrant_does_not_replace_base():
    dimensional = '<xbrli:segment><xbrldi:explicitMember dimension="dei:LegalEntityAxis">company:OtherMember</xbrldi:explicitMember></xbrli:segment>'
    xml = inline(extra=context("other", dimension=dimensional) + fact("EntityRegistrantName", "Other Inc.", "other"))
    assert primary_issuer(xml, "8-K")["issuer_name"] == "Example Inc."


@pytest.mark.parametrize("metadata", [
    facts() + fact("EntityCentralIndexKey", "99"),
    facts() + fact("EntityRegistrantName", "Other Inc."),
    facts() + fact("DocumentType", "S-1"),
    fact("EntityCentralIndexKey", "42") + fact("EntityRegistrantName", "Example Inc."),
    facts(cik="0"),
    facts(name=" "),
])
def test_primary_missing_or_contradictory_facts_remain_unresolved(metadata):
    assert primary_issuer(inline(metadata=metadata), "8-K") is None


def test_primary_two_coherent_base_identities_are_ambiguous():
    xml = inline(contexts=context() + context("other", "99"), metadata=facts() + facts("other", "99", "Other Inc."))
    assert primary_issuer(xml, "8-K") is None


def test_primary_context_cik_must_match_fact_and_duplicate_contexts_are_rejected():
    assert primary_issuer(inline(contexts=context(cik="99")), "8-K") is None
    assert primary_issuer(inline(contexts=context() + context()), "8-K") is None
    assert primary_issuer(inline(contexts=""), "8-K") is None


def test_primary_identity_parts_cannot_be_joined_across_contexts():
    xml = inline(contexts=context() + context("other"), metadata=
                 fact("EntityCentralIndexKey", "42") + fact("EntityRegistrantName", "Example Inc.", "other") + fact("DocumentType", "8-K", "other"))
    assert primary_issuer(xml, "8-K") is None


@pytest.mark.parametrize("tag", ["script", "template"])
def test_primary_non_content_facts_do_not_override_real_facts(tag):
    assert primary_issuer(inline(extra=f'<{tag}>{facts(cik="99", name="Spoof")}</{tag}>'), "8-K")["cik"] == "0000000042"


def test_effect_singular_pair_resolves_without_positional_identity():
    assert primary_issuer(effect(), "EFFECT") == {"cik": "0000000042", "issuer_name": "Example Inc."}
    assert effect_filer_registrations(effect()) == [{"cik": "0000000042", "issuer_name": "Example Inc.", "registration_id": "333-42"}]


@pytest.mark.parametrize("filers", [
    '<filer><cik>42</cik></filer><filer><entityName>Example Inc.</entityName></filer>',
    '<filer><cik>42</cik><cik>99</cik><entityName>Example Inc.</entityName></filer>',
    '<filer><cik>42</cik><entityName>Example Inc.</entityName><entityName>Other Inc.</entityName></filer>',
    '<filer><cik>42</cik></filer>',
    '<filer><cik>0</cik><entityName>Example Inc.</entityName></filer>',
    '<filer><cik>42</cik><entityName> </entityName></filer>',
])
def test_effect_requires_one_complete_singular_filer_pair(filers):
    assert primary_issuer(effect(filers), "EFFECT") is None


def test_effect_form_and_root_must_agree():
    assert primary_issuer(effect().replace('>EFFECT<', '>S-1<'), "EFFECT") is None
    assert primary_issuer(effect().replace('<edgarSubmission>', '<edgarSubmission xmlns="urn:spoof">'), "EFFECT") is None


@pytest.mark.parametrize("text", ["", "<html>", "plain prose EntityRegistrantName Example Inc.", '<!DOCTYPE html [<!ENTITY name "Example Inc.">]><html/>'])
def test_invalid_xml_and_entity_expansion_cannot_establish_primary_authority(text):
    assert primary_issuer(text, "8-K") is None
    assert effect_filer_registrations(text) == []


@pytest.mark.parametrize("filers", [
    '<filer><cik>42</cik><cik>42</cik><entityName>Example Inc.</entityName><fileNumber>333-42</fileNumber></filer>',
    '<filer><cik>42</cik><entityName>Example Inc.</entityName><entityName>Other Inc.</entityName><fileNumber>333-42</fileNumber></filer>',
    '<filer><cik>42</cik><entityName>Example Inc.</entityName><fileNumber>333-42</fileNumber><fileNumber>333-42-01</fileNumber></filer>',
    '<filer><cik>42</cik><entityName>Example Inc.</entityName></filer>',
    '<filer><cik>0</cik><entityName>Example Inc.</entityName><fileNumber>333-42</fileNumber></filer>',
    '<filer><cik>42</cik><entityName>Example Inc.</entityName><fileNumber>001-42</fileNumber></filer>',
    '<filer><cik>42</cik><entityName>Example Inc.</entityName><fileNumber>333-42 trailing</fileNumber></filer>',
    '<filer><cik>42</cik><entityName><span>Example Inc.</span></entityName><fileNumber>333-42</fileNumber></filer>',
    '<filer><cik>42</cik></filer><filer><entityName>Example Inc.</entityName><fileNumber>333-42</fileNumber></filer>',
])
def test_effect_registration_pairs_reject_malformed_or_duplicate_child_fields(filers):
    assert effect_filer_registrations(effect(filers)) == []


def test_effect_registration_pairs_cannot_hide_bad_later_filer_or_conflicting_identity():
    valid = '<filer><cik>42</cik><entityName>Example Inc.</entityName><fileNumber>333-42</fileNumber></filer>'
    bad = '<filer><cik>99</cik><entityName>Other Inc.</entityName></filer>'
    assert effect_filer_registrations(effect(valid + bad)) == []
    conflict = valid.replace('Example Inc.', 'Different Inc.').replace('333-42', '333-43')
    assert effect_filer_registrations(effect(valid + conflict)) == []
    assert effect_filer_registrations(effect(valid + valid)) == [{"cik": "0000000042", "issuer_name": "Example Inc.", "registration_id": "333-42"}]


@pytest.mark.parametrize("transform", [
    lambda xml: xml.replace('>EFFECT<', '>F-1<'),
    lambda xml: xml.replace('<edgarSubmission>', '<edgarSubmission xmlns="urn:spoof">'),
    lambda xml: xml.replace('<submissionType>EFFECT</submissionType>', ''),
    lambda xml: xml.replace('<effectiveData>', '<effectiveData><wrapper>').replace('</effectiveData>', '</wrapper></effectiveData>'),
])
def test_effect_registration_pairs_require_direct_effect_source_structure(transform):
    assert effect_filer_registrations(transform(effect())) == []


def test_effect_issuer_can_resolve_when_registration_scope_remains_missing():
    text = effect('<filer><cik>42</cik><entityName>Example Inc.</entityName></filer>')
    assert primary_issuer(text, "EFFECT") == {"cik": "0000000042", "issuer_name": "Example Inc."}
    assert effect_filer_registrations(text) == []
