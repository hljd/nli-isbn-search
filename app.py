"""NLI ISBN Checker: is a book also held by the National Library of Israel?
"""

import hashlib
import logging
import os
import time
from pathlib import Path

import streamlit as st
from openpyxl.utils import get_column_letter

from nli_client import NO, REVIEW, YES, NliClient, YazNotFoundError, find_yaz_client
from spreadsheet_processor import (
    ImportErrorMessage, column_options, data_rows, has_formulas, plan_rows,
    process_sheet, read_input, render_workbook,
)

logging.basicConfig(
    level=getattr(logging, os.getenv("NLI_LOG_LEVEL", "WARNING").upper(), logging.WARNING),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


def format_duration(seconds):
    total = max(0, round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


st.set_page_config(page_title="NLI ISBN Search", layout="centered")
st.title("National Library of Israel ISBN Search")
st.write("Upload a book list, select its ISBN column, and download a copy showing which "
         "books are held by the National Library of Israel.")
st.caption("Green: yes, NLI has it · Red: no match found · Yellow: manual review needed")

try:
    yaz_path = find_yaz_client()
except YazNotFoundError as exc:
    st.error(f"This computer is missing a required tool. {exc}")
    st.stop()

# Completed searches are remembered for this browser session, so re-running
# after an interruption does not search the same ISBNs again.
cache = st.session_state.setdefault("isbn_cache", {})

# ---------------------------------------------------------------------------
uploaded = st.file_uploader("Book spreadsheet (.xlsx or .csv)", type=["xlsx", "csv"])
if uploaded is None:
    st.stop()
contents = uploaded.getvalue()
file_hash = hashlib.sha256(contents).hexdigest()

encoding, delimiter = "Auto", "Auto"
if Path(uploaded.name).suffix.lower() == ".csv":
    with st.expander("CSV options (change these if the preview looks wrong)"):
        encodings = {
            "Auto": "Auto", "UTF-8": "utf-8-sig", "UTF-16 (with BOM)": "utf-16",
            "Windows-1252 (Western European)": "cp1252", "Windows-1255 (Hebrew)": "cp1255",
            "Latin-1": "latin-1", "Mac Roman": "mac_roman",
        }
        separators = {"Auto": "Auto", "Comma": ",", "Semicolon": ";", "Tab": "\t", "Pipe": "|"}
        encoding = encodings[st.selectbox("Text encoding", list(encodings), key=f"enc:{file_hash}")]
        delimiter = separators[st.selectbox("Column separator", list(separators), key=f"sep:{file_hash}")]

import_info = {}
try:
    workbook = read_input(uploaded.name, contents, encoding, delimiter, import_info)
except ImportErrorMessage as exc:
    st.error(str(exc))
    st.stop()
except Exception:
    st.error("Could not read this file. Please upload an Excel (.xlsx) file or a CSV file.")
    st.stop()
for warning in import_info.get("warnings", []):
    st.warning(warning)

sheet_name = st.selectbox("Worksheet", workbook.sheetnames, key=f"sheet:{file_hash}")
sheet = workbook[sheet_name]
header_choices = list(range(1, min(5, max(sheet.max_row, 1)) + 1))
# Default to the first row that mentions "ISBN" (skips title rows above the headers).
header_default = next((i for i, r in enumerate(header_choices)
                       if any("isbn" in str(c.value or "").lower() for c in sheet[r])), 0)
header_row = st.selectbox("Header row (column names)", header_choices,
                          index=header_default, key=f"header:{file_hash}:{sheet_name}",
                          help="Only rows below this row are searched.")

with st.expander("Preview the first rows", expanded=True):
    shown_cols = min(sheet.max_column, 12)
    st.dataframe([
        {"Row": r, **{get_column_letter(c): str(sheet.cell(r, c).value or "") for c in range(1, shown_cols + 1)}}
        for r in range(1, min(sheet.max_row, 8) + 1)
    ], hide_index=True)
    if sheet.max_column > 12:
        st.caption("The preview shows columns A–L. All columns are available below.")

options = column_options(sheet, header_row)
if not options:
    st.error("This worksheet has no columns below the chosen header row.")
    st.stop()
labels = list(options)
default = next((i for i, label in enumerate(labels) if "isbn" in label.lower()), 0)
isbn_label = st.selectbox("ISBN column", labels, index=default,
                          key=f"isbn:{file_hash}:{sheet_name}:{header_row}")
isbn_col = options[isbn_label]

rows = data_rows(sheet, header_row)
plans, unique_isbns = plan_rows(sheet, rows, isbn_col)
st.write(f"{len(rows)} nonempty book {'row' if len(rows) == 1 else 'rows'} found.")
already = sum(1 for key in unique_isbns if key in cache)
if already:
    st.caption(f"{already} of these ISBNs were already searched in this session and will not be searched again.")
if has_formulas(sheet):
    st.warning("This worksheet contains Excel formulas. Two columns are inserted at the left, and "
               "formulas are not rewritten, so cell references in them may point to the wrong column "
               "in the downloaded copy. Consider uploading a copy with values only.")

signature = (file_hash, encoding, delimiter, sheet_name, header_row, isbn_col)
if st.session_state.get("result_signature") != signature:
    st.session_state.pop("completed", None)

# ---------------------------------------------------------------------------
if st.button("Search NLI", type="primary", disabled=not rows):
    st.session_state.pop("completed", None)
    bar = st.progress(0.0)
    count_text, timer_text, status_text = st.empty(), st.empty(), st.empty()
    started = time.perf_counter()
    durations, last = [], [time.perf_counter()]
    to_search = sum(1 for key in unique_isbns if key not in cache)  # new searches needed

    def on_progress(done, total, cached):
        now = time.perf_counter()
        if not cached:
            durations.append(now - last[0])
        last[0] = now
        bar.progress(done / total if total else 1.0)
        count_text.write(f"Completed {done} of {total} ISBN searches.")
        remaining_new = max(0, to_search - len(durations))
        estimate = ""
        if remaining_new and len(durations) >= 3:
            average = sum(durations) / len(durations)
            estimate = f" · About {format_duration(average * remaining_new)} remaining"
        timer_text.caption(f"Elapsed: {format_duration(now - started)}{estimate}")
        status_text.empty()

    def on_status(message):
        status_text.info(message)

    timer_text.caption("Connecting to the NLI catalog…")
    try:
        with NliClient(yaz_path, status_callback=on_status) as client:
            row_results, stopped = process_sheet(sheet, rows, isbn_col, client.search_isbn,
                                                 cache=cache, progress=on_progress)
    except Exception:
        logging.exception("Search run failed")
        st.error("The search stopped because of an unexpected error. ISBNs already searched are "
                 "remembered; click Search NLI to continue.")
        st.stop()
    elapsed = time.perf_counter() - started
    for placeholder in (count_text, timer_text, status_text):
        placeholder.empty()
    bar.empty()
    try:
        output, counts = render_workbook(workbook, sheet_name, row_results, header_row)
    except Exception:
        logging.exception("Export failed")
        st.error("The searches finished, but the Excel file could not be created.")
        st.stop()
    preview = [{"Excel row": row, "ISBN (original)": "" if plans[row].raw is None else str(plans[row].raw),
                "NLI Match": result.status, "Notes": result.notes}
               for row, result in row_results.items()]
    st.session_state["result_signature"] = signature
    st.session_state["completed"] = dict(output=output, counts=counts, preview=preview,
                                         stopped=stopped, elapsed=elapsed)

# ---------------------------------------------------------------------------
completed = st.session_state.get("completed")
if completed:
    if completed["stopped"]:
        st.warning(f"The search was stopped early because {completed['stopped']}. The rows that were "
                   "not searched are marked Manual Review. Check your internet connection and click "
                   "Search NLI again; completed searches will not be repeated.")
    st.info(f"Search finished in {format_duration(completed['elapsed'])}.")
    counts = completed["counts"]
    total = sum(counts.values())
    for column, (label, key) in zip(st.columns(3), [("Yes", YES), ("No", NO), ("Manual Review", REVIEW)]):
        column.metric(label, counts.get(key, 0))
        if total:
            column.caption(f"{counts.get(key, 0) / total:.0%}")
    st.dataframe(completed["preview"], hide_index=True)
    st.download_button("Download Excel file", data=completed["output"],
                       file_name=f"NLI_Checked_{Path(uploaded.name).stem}.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                       type="primary")
