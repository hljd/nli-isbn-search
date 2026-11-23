"""Spreadsheet import, batch ISBN searching, and Excel export.

The import helpers (text decoding, delimiter detection, column options, and
column insertion) are adapted from the HOLLIS ISBN Checker.
"""
import codecs
import csv
import io
from collections import Counter
from copy import copy
from dataclasses import dataclass
from itertools import islice
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Border, PatternFill, Side
from openpyxl.utils import column_index_from_string, get_column_letter
from openpyxl.worksheet.cell_range import MultiCellRange

from isbn_utils import isbn13, split_isbns
from nli_client import NO, REVIEW, YES, SearchResult

OUTPUT_HEADERS = ("NLI Match", "NLI Notes")
FILLS = {
    YES: PatternFill("solid", fgColor="DDF1D8"),     # green
    NO: PatternFill("solid", fgColor="F7D9D9"),      # red
    REVIEW: PatternFill("solid", fgColor="FFF0BD"),  # yellow
}
THIN = Side(style="thin", color="FF000000")
ALL_BORDERS = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
TEXT_EXTENSIONS = {".csv", ".tsv", ".txt"}
MAX_LISTED_RECORDS = 3     # records named in one NLI Notes cell
MAX_TITLE_LENGTH = 150
MAX_CONSECUTIVE_FAILURES = 5  # then stop: the catalog is probably unreachable
NO_MATCH_NOTE = "No matching ISBN found in NLI catalog"


class ImportErrorMessage(ValueError):
    """A safe, user-facing input error."""


# --------------------------------------------------------------------------
# Import (adapted from HOLLIS)
# --------------------------------------------------------------------------

def decode_text(contents, encoding="Auto"):
    """Decode strictly: never silently replace unrecognized characters."""
    warnings = []
    if encoding != "Auto":
        chosen = encoding
    elif contents.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        chosen = "utf-32"
    elif contents.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        chosen = "utf-16"
    else:
        sample = contents.rstrip(b"\x00")[:4096]
        if b"\x00" in sample:
            raise ImportErrorMessage("Select UTF-16 LE or UTF-16 BE under Text file options for this file.")
        try:
            contents.decode("utf-8-sig")
            chosen = "utf-8-sig"
        except UnicodeDecodeError:
            chosen = "cp1252"
            warnings.append("Used Windows-1252 because this file is not UTF-8. Check the preview; "
                            "change the encoding if text looks wrong.")
    try:
        text = contents.decode(chosen)
    except (UnicodeError, LookupError):
        raise ImportErrorMessage("Could not decode this file. Choose another encoding under Text file options.") from None
    text = text.lstrip("﻿").rstrip("\x00")
    if any(ord(c) < 32 and c not in "\t\r\n" for c in text):
        raise ImportErrorMessage("The decoded file contains unsupported control characters. "
                                 "Check the encoding or re-export it as CSV.")
    return text, chosen, warnings


def detect_delimiter(text):
    """Pick the delimiter that gives the most rows of a consistent width."""
    scores = []
    for delimiter in ("\t", ",", ";", "|"):
        try:
            rows = list(islice(csv.reader(io.StringIO(text, newline=""), delimiter=delimiter), 100))
        except csv.Error:
            continue
        widths = Counter(len(row) for row in rows if len(row) > 1)
        if widths:
            width, count = widths.most_common(1)[0]
            scores.append((count, width, delimiter))
    return max(scores)[2] if scores else ","


def read_input(filename, contents, encoding="Auto", delimiter="Auto", import_info=None):
    """Load an .xlsx file, or a CSV/TSV/TXT file into a one-sheet workbook."""
    extension = Path(filename).suffix.lower()
    if extension == ".xlsx":
        return load_workbook(io.BytesIO(contents))
    if extension not in TEXT_EXTENSIONS:
        raise ImportErrorMessage("Upload an .xlsx or .csv file.")
    text, chosen_encoding, warnings = decode_text(contents, encoding)
    chosen_delimiter = detect_delimiter(text) if delimiter == "Auto" else delimiter
    try:
        rows = list(csv.reader(io.StringIO(text, newline=""), delimiter=chosen_delimiter, strict=True))
    except csv.Error:
        raise ImportErrorMessage("Could not parse the text file. Check the separator and quoting.") from None
    if not any(any(value.strip() for value in row) for row in rows):
        raise ImportErrorMessage("This file contains no spreadsheet data.")
    if len(rows) > 1048576 or any(len(row) > 16382 for row in rows):
        raise ImportErrorMessage("This file exceeds Excel's worksheet size limits.")
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Books"
    for row_index, row in enumerate(rows, 1):
        for col_index, value in enumerate(row, 1):
            if len(value) > 32767:
                raise ImportErrorMessage("A text cell exceeds Excel's 32,767-character limit.")
            cell = sheet.cell(row_index, col_index, value)
            cell.data_type = "s"  # keep text as text: leading zeros, literal '='
    if import_info is not None:
        import_info.update(encoding=chosen_encoding, delimiter=chosen_delimiter, warnings=warnings)
    return workbook


def column_options(sheet, header_row=1):
    """Map "B — ISBN" style labels to column numbers (skipping empty columns)."""
    options = {}
    for col in range(1, sheet.max_column + 1):
        header = sheet.cell(header_row, col).value
        if header in (None, "") and not any(sheet.cell(row, col).value not in (None, "")
                                            for row in range(header_row + 1, sheet.max_row + 1)):
            continue
        label = (str(header).strip() if header is not None else "") or "(blank header)"
        options[f"{get_column_letter(col)} — {label}"] = col
    return options


def data_rows(sheet, header_row=1):
    """Nonempty rows below the header row."""
    return [row for row in range(header_row + 1, sheet.max_row + 1)
            if any(sheet.cell(row, col).value not in (None, "")
                   for col in range(1, sheet.max_column + 1))]


def has_formulas(sheet):
    return any(cell.data_type == "f" for row in sheet.iter_rows() for cell in row)


# --------------------------------------------------------------------------
# Searching
# --------------------------------------------------------------------------

@dataclass
class RowPlan:
    isbns: list      # valid ISBNs in the cell, as written (normalized)
    invalid: bool    # the cell also contains something that is not a valid ISBN
    raw: object      # the original cell value


def plan_rows(sheet, rows, isbn_col):
    """Read each row's ISBN cell. Returns (plans, unique ISBN keys to search).

    Unique keys are ISBN-13s, so an ISBN-10 and its ISBN-13 equivalent are
    searched only once, as are ISBNs repeated across rows.
    """
    plans, unique = {}, {}
    for row in rows:
        raw = sheet.cell(row, isbn_col).value
        isbns, invalid = split_isbns(raw)
        plans[row] = RowPlan(isbns, invalid, raw)
        for isbn in isbns:
            unique.setdefault(isbn13(isbn), isbn)
    return plans, list(unique)


def run_searches(keys, search, cache=None, progress=None,
                 max_consecutive_failures=MAX_CONSECUTIVE_FAILURES):
    """Search each unique ISBN key; return {key: SearchResult}.

    `search(isbn)` must return a SearchResult (NliClient.search_isbn).
    `cache` (a dict) holds completed results from earlier runs in this
    session; failed searches are never cached, so they are retried next time.
    If several searches in a row fail, the catalog is probably unreachable:
    the remaining ISBNs are marked Manual Review instead of waiting on more
    timeouts. `progress(done, total, cached)` is called after each key.
    Returns (results, stopped_message_or_None).
    """
    cache = {} if cache is None else cache
    results, failures, stopped = {}, 0, None
    for index, key in enumerate(keys):
        cached = True  # no new search was made for this key
        if stopped:
            results[key] = SearchResult(REVIEW, reason="Not searched: " + stopped, failed=True)
        elif key in cache:
            results[key] = cache[key]
        else:
            cached = False
            result = search(key)
            results[key] = result
            if result.failed:
                failures += 1
                if failures >= max_consecutive_failures:
                    stopped = (f"the NLI catalog could not be reached after {failures} "
                               "consecutive failed searches")
            else:
                failures = 0
                cache[key] = result
        if progress:
            progress(index + 1, len(keys), cached)
    return results, stopped


@dataclass
class RowResult:
    status: str
    notes: str
    url: str | None = None


def _short_title(record):
    title = record.title
    if len(title) > MAX_TITLE_LENGTH:
        title = title[:MAX_TITLE_LENGTH - 1].rstrip() + "…"
    return title


def _record_line(record):
    return f"NLI Record ID: {record.record_id}; Title: {_short_title(record)}"


def _records_text(records):
    lines = [_record_line(r) for r in records[:MAX_LISTED_RECORDS]]
    if len(records) > MAX_LISTED_RECORDS:
        lines.append(f"(+{len(records) - MAX_LISTED_RECORDS} more NLI records)")
    return lines


def combine_row(plan, results):
    """Turn one row's ISBN search results into its NLI Match and NLI Notes.

    * Yes if any ISBN in the cell is a verified NLI match (NLI Notes: title only).
    * No (NLI Notes blank) only if the cell holds no invalid text and EVERY search completed
      with zero hits.
    * Otherwise Manual Review, with the reasons.
    The hyperlink points to the first NLI record listed.
    """
    if not plan.isbns:
        missing = plan.raw is None or str(plan.raw).strip() == ""
        return RowResult(REVIEW, "Missing ISBN" if missing else "Invalid ISBN (format, length, or check digit)")
    by_isbn = [(isbn, results[isbn13(isbn)]) for isbn in plan.isbns]
    labelled = len(by_isbn) > 1

    yes = [(isbn, r) for isbn, r in by_isbn if r.status == YES]
    if yes:
        result = yes[0][1]
        # Yes: just the title(s) found, linked to the first NLI record.
        titles = list(dict.fromkeys(_short_title(r) for r in result.records))
        return RowResult(YES, "\n".join(titles[:MAX_LISTED_RECORDS]), result.records[0].url)

    if not plan.invalid and all(r.status == NO for _, r in by_isbn):
        return RowResult(NO, "")  # No: NLI Notes left blank

    lines, url = [], None
    for isbn, result in by_isbn:
        if result.status == NO:
            text = NO_MATCH_NOTE
        else:
            text = result.reason or "Manual review required"
        lines.append(f"ISBN {isbn}: {text}" if labelled else text)
        if result.status == REVIEW and result.records:
            lines.extend(_records_text(result.records))
            url = url or result.records[0].url
    if plan.invalid:
        lines.append("The ISBN cell also contains an invalid ISBN")
    return RowResult(REVIEW, "\n".join(lines), url)


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------

def insert_output_columns(sheet, count):
    """Insert `count` columns at A, shifting merged ranges and widths too.

    (openpyxl moves cell values but not these layout definitions.)
    """
    merged_ranges = [copy(area) for area in sheet.merged_cells.ranges]
    dimensions = {name: copy(dim) for name, dim in sheet.column_dimensions.items()}
    sheet.insert_cols(1, count)
    sheet.merged_cells = MultiCellRange()
    for area in merged_ranges:
        area.shift(col_shift=count)
        sheet.merged_cells.add(area)
    sheet.column_dimensions.clear()
    for name, dim in dimensions.items():
        shifted = get_column_letter(column_index_from_string(name) + count)
        dim.index = shifted
        if dim.min:
            dim.min += count
        if dim.max:
            dim.max += count
        sheet.column_dimensions[shifted] = dim


def set_hyperlink(cell, url):
    """Add a blue, underlined link while preserving other font attributes."""
    cell.hyperlink = url
    font = copy(cell.font)
    font.color, font.underline = "0563C1", "single"
    cell.font = font


def render_workbook(workbook, sheet_name, row_results, header_row=1):
    """Add NLI Match and NLI Notes as columns A and B; return (xlsx bytes, counts)."""
    sheet = workbook[sheet_name]
    original_max_col = sheet.max_column
    count = len(OUTPUT_HEADERS)
    insert_output_columns(sheet, count)
    last_col = original_max_col + count
    header_style = sheet.cell(header_row, count + 1)._style
    for col, title in enumerate(OUTPUT_HEADERS, 1):
        cell = sheet.cell(header_row, col, title)
        cell._style = copy(header_style)
    sheet.column_dimensions["A"].width = 15
    # Excel widths are in characters (~7 px each at the default font): 42 is
    # about 300 px. NLI Notes wrap, so longer titles/reasons take 2-3 lines.
    sheet.column_dimensions["B"].width = 42
    counts = Counter()
    for row, result in row_results.items():
        counts[result.status] += 1
        sheet.cell(row, 1, result.status)
        notes = sheet.cell(row, 2, result.notes or None)
        if result.notes:
            notes.data_type = "s"
        alignment = copy(notes.alignment)
        alignment.wrap_text, alignment.vertical = True, "top"
        notes.alignment = alignment
        if result.url:
            set_hyperlink(notes, result.url)
        for col in range(1, last_col + 1):
            cell = sheet.cell(row, col)
            cell.fill = copy(FILLS[result.status])
            cell.border = copy(ALL_BORDERS)
    for col in range(1, last_col + 1):
        sheet.cell(header_row, col).border = copy(ALL_BORDERS)
    for row in sheet.iter_rows(min_row=1, max_row=sheet.max_row, max_col=last_col):
        for cell in row:
            alignment = copy(cell.alignment)
            alignment.vertical = "top"
            cell.alignment = alignment
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue(), counts


def process_sheet(sheet, rows, isbn_col, search, cache=None, progress=None):
    """Plan, search and combine. Returns (row_results, stopped_message)."""
    plans, keys = plan_rows(sheet, rows, isbn_col)
    results, stopped = run_searches(keys, search, cache, progress)
    return {row: combine_row(plan, results) for row, plan in plans.items()}, stopped
