"""MARC parsing and Yes / Manual Review decisions, including real NLI data."""
import pytest

from conftest import FIXTURES, book, marc_record
from nli_client import REVIEW, YES, classify, parse_marc, summarize


def records(data):
    return [summarize(m) for m in parse_marc(data)]


def test_real_nli_response_with_two_records():
    """The verified positive example returns 2 hits: the print book (with
    852 holdings) and an e-book that cites our ISBN only in 776 $z."""
    found = records((FIXTURES / "nli_9780299284947.mrc").read_bytes())
    assert [r.record_id for r in found] == ["990034518460205171", "997012132147605171"]
    printed, ebook = found
    assert printed.title == "The origins of Israel, 1882-1948: a documentary history"
    assert printed.holdings == ["NLI NLX_STACKS"]
    assert "9780299284947" not in ebook.isbns and ebook.online and not ebook.holdings

    result = classify("9780299284947", found)
    assert result.status == YES
    assert [r.record_id for r in result.records] == ["990034518460205171"]  # e-book excluded
    assert result.records[0].url == "https://www.nli.org.il/en/books/NNL_ALEPH990034518460205171/NLI"


def test_isbn10_matches_isbn13_in_record():
    result = classify("0299284948", records(book("990001", ["978-0-299-28494-7 (paperback)"])))
    assert result.status == YES


def test_multiple_held_matches_are_all_kept():
    data = book("990001", ["9780299284947"], "First") + book("990002", ["0299284948"], "Second")
    result = classify("9780299284947", records(data))
    assert result.status == YES and [r.record_id for r in result.records] == ["990001", "990002"]


def test_match_without_holdings_needs_review():
    result = classify("9780299284947", records(book("990001", ["9780299284947"], holdings=False)))
    assert result.status == REVIEW and "no NLI holdings" in result.reason


def test_record_without_our_isbn_needs_review():
    data = book("990001", ["9780299284930"], extra=[("776", ("08", [("z", "9780299284947")]))])
    result = classify("9780299284947", records(data))
    assert result.status == REVIEW and "MARC 020" in result.reason
    assert result.records[0].record_id == "990001"   # listed for the reviewer


def test_cancelled_isbn_in_020z_is_not_a_match():
    data = marc_record([("001", "990001"), ("020", ("  ", [("z", "9780299284947")])),
                        ("245", ("10", [("a", "Title")])), ("852", ("8 ", [("b", "NLI")]))])
    assert classify("9780299284947", records(data)).status == REVIEW


def test_missing_record_id_is_rejected():
    with pytest.raises(ValueError):
        records(marc_record([("020", ("  ", [("a", "9780299284947")]))]))


@pytest.mark.parametrize("data", [b"garbage!!", book("990001", ["9780299284947"])[:-10]])
def test_malformed_marc_raises(data):
    with pytest.raises(ValueError):
        parse_marc(data)
