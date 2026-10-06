from datetime import UTC, date, datetime

import pytest

from contract_ipo_monitor.sources.sec_metadata import (MAX_INDEX_BYTES, accepted_at_from_filing_index,
                                                      filing_date_from_filing_index)


def index(accepted: str, filed="2026-10-05") -> str:
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
      <div class="formGrouping">
        <div class="infoHead">Filing Date</div><div class="info">{filed}</div>
        <div class="infoHead">Accepted</div><div class="info">{accepted}</div>
        <div class="infoHead">Documents</div><div class="info">12</div>
      </div></body></html>"""


@pytest.mark.parametrize("accepted,utc", [
    ("2026-01-06 16:15:07", datetime(2026, 1, 6, 21, 15, 7, tzinfo=UTC)),
    ("2026-07-06 16:15:07", datetime(2026, 7, 6, 20, 15, 7, tzinfo=UTC)),
    ("2026-10-02 20:15:23", datetime(2026, 10, 3, 0, 15, 23, tzinfo=UTC)),
    ("2026-03-08 03:00:00", datetime(2026, 3, 8, 7, tzinfo=UTC)),
    ("2026-11-01 02:00:00", datetime(2026, 11, 1, 7, tzinfo=UTC)),
])
def test_exact_accepted_metadata_uses_eastern_rules_and_retains_seconds(accepted, utc):
    assert accepted_at_from_filing_index(index(accepted)) == utc


def test_late_friday_acceptance_is_not_replaced_by_next_business_filing_date():
    html = index("2026-10-02 20:15:23", filed="2026-10-05")
    result = accepted_at_from_filing_index(html)
    assert result == datetime(2026, 10, 3, 0, 15, 23, tzinfo=UTC)
    assert filing_date_from_filing_index(html) == date(2026, 10, 5)


@pytest.mark.parametrize("html", [
    "", "<p>Accepted 2026-10-02 20:15:23</p>",
    '<div class="infoHead">Filing Date</div><div class="info">2026-10-05</div>',
    '<script><div class="infoHead">Accepted</div><div class="info">2026-10-02 20:15:23</div></script>',
    '<template><div class="infoHead">Accepted</div><div class="info">2026-10-02 20:15:23</div></template>',
    '<div class="otherinfoHead">Accepted</div><div class="info">2026-10-02 20:15:23</div>',
    '<pre>&lt;ACCEPTANCE-DATETIME&gt;20261002201523</pre>',
])
def test_absent_exact_metadata_never_invents_timestamp_from_prose_or_filing_date(html):
    assert accepted_at_from_filing_index(html) is None


def test_nested_text_entities_comments_and_multiple_class_tokens_preserve_actual_field():
    html = index("2026-10-02&#32;20:15:23").replace(
        '<div class="infoHead">Accepted</div>',
        '<div class="infoHead extra">  <span>Accepted</span> &nbsp; </div><!-- SEC field -->')
    assert accepted_at_from_filing_index(html) == datetime(2026, 10, 3, 0, 15, 23, tzinfo=UTC)


@pytest.mark.parametrize("value", [
    "", "2026-10-02", "2026-10-02T20:15:23", "2026-10-02 20:15:23Z",
    "2026-10-02 20:15:23-04:00", "2026-10-02 20:15:23 EST", "2026-10-02 20:15:23.000",
    "2026-02-30 20:15:23", "2026-10-02 24:00:00", "2026-10-02 20:15:60",
    "2026-1-02 20:15:23", "9999-12-31 23:59:59",
])
def test_malformed_or_changed_timestamp_format_is_not_silently_normalized(value):
    with pytest.raises(ValueError, match="Accepted timestamp"):
        accepted_at_from_filing_index(index(value))


@pytest.mark.parametrize("value", ["2026-03-08 02:30:00", "2026-11-01 01:30:00"])
def test_dst_nonexistent_and_ambiguous_labels_cannot_authorize_an_exact_instant(value):
    with pytest.raises(ValueError, match="Ambiguous or nonexistent"):
        accepted_at_from_filing_index(index(value))


@pytest.mark.parametrize("same_value", [False, True])
def test_duplicate_accepted_metadata_is_rejected_even_when_values_agree(same_value):
    other = "2026-10-02 20:15:23" if same_value else "2026-10-02 21:15:23"
    with pytest.raises(ValueError, match="Duplicate"):
        accepted_at_from_filing_index(index("2026-10-02 20:15:23") + index(other))


@pytest.mark.parametrize("html", [
    '<div class="infoHead">Accepted</div>',
    '<div class="infoHead">Accepted</div><div class="infoHead">Documents</div><div class="info">12</div>',
    '<div class="infoHead">Accepted</div>2026-10-02 20:15:23',
    '<div class="infoHead">Accepted</div><div class="info">2026-10-02 20:15:23',
    '<div class="infoHead">Accepted<div class="info">2026-10-02 20:15:23</div>',
    '<div class="infoHead" class="infoHead">Accepted</div><div class="info">2026-10-02 20:15:23</div>',
    '<div class="infoHead">Accepted</div><div class="info" class="info">2026-10-02 20:15:23</div>',
    '<div class="infoHead">Accepted (UTC)</div><div class="info">2026-10-02 20:15:23</div>',
])
def test_malformed_accepted_structure_cannot_pair_with_unrelated_metadata(html):
    with pytest.raises(ValueError, match="Malformed SEC Accepted metadata"):
        accepted_at_from_filing_index(html)


@pytest.mark.parametrize("html", [None, b"<html></html>", "x" * (MAX_INDEX_BYTES + 1)], ids=["missing", "bytes", "oversized"])
def test_parser_input_is_bounded_text(html):
    with pytest.raises(ValueError, match="bounded HTML text"):
        accepted_at_from_filing_index(html)
    with pytest.raises(ValueError, match="bounded HTML text"):
        filing_date_from_filing_index(html)


@pytest.mark.parametrize("html", [
    "", "<p>Filing Date: 2026-10-05</p>",
    '<div class="infoHead">Accepted</div><div class="info">2026-10-02 20:15:23</div>',
    '<pre>Date Filed: 2026-10-05</pre>',
    '<script><div class="infoHead">Filing Date</div><div class="info">2026-10-05</div></script>',
    '<template><div class="infoHead">Filing Date</div><div class="info">2026-10-05</div></template>',
])
def test_filing_date_requires_its_own_index_field_and_never_uses_acceptance_or_prose(html):
    assert filing_date_from_filing_index(html) is None


def test_filing_date_does_not_depend_on_presence_of_accepted_metadata():
    html = '<div class="infoHead">Filing Date</div><div class="info">2026-10-05</div>'
    assert filing_date_from_filing_index(html) == date(2026, 10, 5)
    assert accepted_at_from_filing_index(html) is None


@pytest.mark.parametrize("value", ["", "2026-02-30", "2026-2-05", "2026-10-05T00:00:00Z", "20261005"])
def test_malformed_filing_date_never_becomes_a_normalized_legal_date(value):
    with pytest.raises(ValueError, match="Malformed SEC Filing Date"):
        filing_date_from_filing_index(index("2026-10-02 20:15:23", filed=value))


@pytest.mark.parametrize("other", ["2026-10-05", "2026-10-06"])
def test_duplicate_filing_date_fields_rejected_even_if_the_dates_match(other):
    with pytest.raises(ValueError, match="Duplicate SEC Filing Date"):
        filing_date_from_filing_index(index("2026-10-02 20:15:23") + index("2026-10-02 20:15:23", filed=other))


@pytest.mark.parametrize("html", [
    '<div class="infoHead">Filing Date</div>',
    '<div class="infoHead">Filing Date</div><div class="infoHead">Accepted</div><div class="info">2026-10-02 20:15:23</div>',
    '<div class="infoHead">Filing Date<div class="info">2026-10-05</div>',
    '<div class="infoHead">Filing Date</div><div class="info">2026-10-05',
    '<div class="infoHead">Filing Date (UTC)</div><div class="info">2026-10-05</div>',
])
def test_malformed_filing_date_structure_does_not_borrow_another_field(html):
    with pytest.raises(ValueError, match="Malformed SEC Filing Date metadata"):
        filing_date_from_filing_index(html)
