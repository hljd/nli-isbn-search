import pytest

from isbn_utils import equivalent, isbn10, isbn13, normalize_isbn, split_isbns


@pytest.mark.parametrize("raw, expected", [
    ("9780299284947", "9780299284947"),
    ("978-0-299-28494-7", "9780299284947"),
    (" 978 0 299 28494 7 ", "9780299284947"),
    (9780299284947.0, "9780299284947"),     # Excel numeric cell
    ("9780299284947.0", "9780299284947"),   # numeric text export
    ("0299284948", "0299284948"),
    ("0-299-28494-8", "0299284948"),
    ("029928493x", "029928493X"),           # lowercase check digit X
])
def test_normalize_valid(raw, expected):
    assert normalize_isbn(raw) == expected


@pytest.mark.parametrize("raw", [
    None, "", "   ", True, "9780299284940",  # bad ISBN-13 check digit
    "0299284940",                            # bad ISBN-10 check digit
    "978029928494", "97802992849477",        # wrong lengths
    "abcdefghij", "978-0-299-2849X-7", 9.78e12 + 0.5, float("nan"),
])
def test_normalize_invalid(raw):
    assert normalize_isbn(raw) is None


def test_conversions_round_trip():
    assert isbn13("0299284948") == "9780299284947"
    assert isbn10("9780299284947") == "0299284948"
    assert isbn10("9780299284930") == "029928493X"   # check digit X
    assert isbn10("0299284948") == "0299284948"
    assert isbn10("9791032305690") is None           # 979 ISBNs have no ISBN-10
    assert equivalent("0299284948", "9780299284947")
    assert not equivalent("0299284948", "9780299284930")


def test_split_multiple_and_invalid_parts():
    assert split_isbns("978-0-299-28494-7; 029928493X") == (["9780299284947", "029928493X"], False)
    assert split_isbns("9780299284947 / 12345") == (["9780299284947"], True)
    assert split_isbns("9780299284947, 9780299284947") == (["9780299284947"], False)
    assert split_isbns(None) == ([], False)
    assert split_isbns("not an isbn") == ([], True)
