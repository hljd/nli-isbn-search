"""Spreadsheet round trip: input preserved, exactly two new columns, NLI Notes."""
import io

from openpyxl import Workbook, load_workbook
from openpyxl.cell.cell import MergedCell

from nli_client import NO, REVIEW, YES, NliRecord, SearchResult
from spreadsheet_processor import (
    process_sheet, read_input, render_workbook, run_searches, data_rows,
)

ORIGIN = NliRecord("990034518460205171", "The origins of Israel, 1882-1948: a documentary history",
                   {"9780299284947"}, ["NLI NLX_STACKS"])
OTHER = NliRecord("990000000000205171", "Another book", {"9780306406157"}, ["NLI NLX_STACKS"])
EBOOK = NliRecord("997012132147605171", "The origins of Israel (online)", {"9780299284930"}, [], True)

RESPONSES = {
    "9780299284947": SearchResult(YES, [ORIGIN]),
    "9780000000002": SearchResult(NO),
    "9780306406157": SearchResult(YES, [OTHER, ORIGIN]),
    "9780299284930": SearchResult(REVIEW, [EBOOK], "NLI record lists this ISBN, but no NLI holdings "
                                                   "(MARC 852) are recorded"),
    "9781861972712": SearchResult(REVIEW, reason="Connection timeout: the NLI catalog did not respond in time",
                                  failed=True),
}

ROWS = [  # (title, ISBN cell as entered, expected status)
    ("Positive", "978-0-299-28494-7", YES),
    ("Negative", "9780000000002", NO),
    ("Invalid", "9780299284940", REVIEW),
    ("Missing", None, REVIEW),
    ("Duplicate as ISBN-10", "0299284948", YES),
    ("Numeric cell", 9780299284947, YES),
    ("Two matches", "9780306406157", YES),
    ("E-book only", "9780299284930", REVIEW),
    ("Timed out", "9781861972712", REVIEW),
    ("Two ISBNs, one negative", "9780000000002; 9780299284947", YES),
    ("Negative plus junk", "9780000000002 / 12345", REVIEW),
]


def build_workbook():
    wb = Workbook()
    ws = wb.active
    ws.title = "Books"
    ws["A1"] = "Acquisitions list"           # preamble row above the header
    ws.merge_cells("A1:C1")
    ws.append(["Title", "ISBN", "Price"])
    for i, (title, isbn, _) in enumerate(ROWS):
        ws.append([title, isbn, 10 + i])
    ws.column_dimensions["A"].width = 33
    wb.create_sheet("Other")["A1"] = "untouched"
    return wb


def run(wb, search=None):
    calls = []

    def fake_search(isbn):
        calls.append(isbn)
        return RESPONSES[isbn]
    sheet = wb["Books"]
    rows = data_rows(sheet, header_row=2)
    results, stopped = process_sheet(sheet, rows, 2, search or fake_search)
    output, counts = render_workbook(wb, "Books", results, header_row=2)
    return load_workbook(io.BytesIO(output)), results, counts, calls, stopped


def test_round_trip_preserves_data_and_adds_two_columns():
    original = build_workbook()
    expected_values = [[c.value for c in row] for row in original["Books"].iter_rows()]
    out, results, counts, calls, stopped = run(build_workbook())
    ws = out["Books"]

    assert stopped is None
    assert ws.max_column == 3 + 2
    assert [ws.cell(2, c).value for c in range(1, 6)] == ["NLI Match", "NLI Notes", "Title", "ISBN", "Price"]
    # Original cells (including preamble and row order) are unchanged, shifted by 2.
    for r, row in enumerate(expected_values, 1):
        assert [ws.cell(r, c + 2).value for c in range(1, 4)] == row
    assert ws.cell(1, 1).value is None and ws.cell(1, 2).value is None
    assert "C1:E1" in {str(m) for m in ws.merged_cells.ranges}
    assert ws.column_dimensions["C"].width == 33
    assert out["Other"]["A1"].value == "untouched"
    # Every cell, original and new, header and data, is top-aligned (merged
    # ranges take their alignment from the top-left cell).
    assert all(c.alignment.vertical == "top" for row in ws.iter_rows() for c in row
               if not isinstance(c, MergedCell))

    statuses = [ws.cell(r, 1).value for r in range(3, 3 + len(ROWS))]
    assert statuses == [expected for _, _, expected in ROWS]
    assert counts == {YES: 5, NO: 1, REVIEW: 5}


def test_duplicates_are_searched_once():
    *_, calls, _ = run(build_workbook())
    assert sorted(calls) == sorted(RESPONSES)  # each unique ISBN-13 exactly once


def test_notes_and_links():
    out, *_ = run(build_workbook())
    ws = out["Books"]
    notes = {ws.cell(r, 3).value: ws.cell(r, 2) for r in range(3, 3 + len(ROWS))}

    positive = notes["Positive"]
    assert positive.value == "The origins of Israel, 1882-1948: a documentary history"
    assert positive.hyperlink.target == "https://www.nli.org.il/en/books/NNL_ALEPH990034518460205171/NLI"

    assert notes["Negative"].value is None and notes["Negative"].hyperlink is None
    assert notes["Invalid"].value == "Invalid ISBN (format, length, or check digit)"
    assert notes["Missing"].value == "Missing ISBN"
    assert notes["Two matches"].value.splitlines() == [
        "Another book",
        "The origins of Israel, 1882-1948: a documentary history",
    ]
    assert "no NLI holdings" in notes["E-book only"].value
    assert "997012132147605171" in notes["E-book only"].value
    assert notes["Timed out"].value.startswith("Connection timeout")
    assert notes["Two ISBNs, one negative"].value == "The origins of Israel, 1882-1948: a documentary history"
    assert "invalid ISBN" in notes["Negative plus junk"].value


def test_colors():
    out, *_ = run(build_workbook())
    ws = out["Books"]
    colors = {ws.cell(r, 1).value: ws.cell(r, 5).fill.fgColor.rgb for r in range(3, 3 + len(ROWS))}
    assert colors[YES].endswith("DDF1D8")
    assert colors[NO].endswith("F7D9D9")
    assert colors[REVIEW].endswith("FFF0BD")


def test_failed_search_is_never_no():
    """Even if every search fails, nothing is classified No."""
    def failing(isbn):
        return SearchResult(REVIEW, reason="Connection timeout", failed=True)
    out, results, counts, _, stopped = run(build_workbook(), failing)
    assert NO not in counts
    assert stopped and "could not be reached" in stopped


def test_consecutive_failures_stop_the_batch():
    calls = []

    def failing(isbn):
        calls.append(isbn)
        return SearchResult(REVIEW, reason="timeout", failed=True)
    keys = [f"key{i}" for i in range(10)]
    results, stopped = run_searches(keys, failing, max_consecutive_failures=3)
    assert len(calls) == 3 and stopped
    assert all(r.status == REVIEW and r.failed for r in results.values())


def test_cache_skips_completed_but_retries_failed():
    cache, calls = {}, []

    def search(isbn):
        calls.append(isbn)
        return SearchResult(NO) if isbn == "a" else SearchResult(REVIEW, reason="x", failed=True)
    run_searches(["a", "b"], search, cache)
    run_searches(["a", "b"], search, cache)
    assert calls == ["a", "b", "b"] and set(cache) == {"a"}


def test_csv_import_keeps_text_and_leading_zeros():
    data = "Title,ISBN\r\nCorrect,0299284948\r\nHebrew,=9780000000002\r\n".encode("utf-8")
    sheet = read_input("books.csv", data).active
    assert sheet["B2"].value == "0299284948" and sheet["B3"].value == "=9780000000002"
    hebrew = "כותר,ISBN\n1,2\n".encode("cp1255")
    assert read_input("x.csv", hebrew, encoding="cp1255").active["A1"].value == "כותר"


def test_duplicate_titles_listed_once():
    from spreadsheet_processor import RowPlan, combine_row
    copy = NliRecord("990000000000000001", ORIGIN.title, {"9780299284947"}, ["NLI"])
    row = combine_row(RowPlan(["9780299284947"], False, "x"),
                      {"9780299284947": SearchResult(YES, [ORIGIN, copy])})
    assert row.notes == ORIGIN.title and row.url == ORIGIN.url
