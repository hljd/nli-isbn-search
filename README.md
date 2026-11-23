# National Library of Israel ISBN Search

Checks whether the books on your list are held by the **National Library of Israel (NLI)**. Upload a spreadsheet, and the app looks up each ISBN in the NLI catalog. You then download a copy of your spreadsheet with two new columns at the front:

| Column | What it shows |
|---|---|
| **NLI Match** | **Yes**, **No**, or **Manual Review** |
| **NLI Notes** | **Yes:** the book's title in NLI (click it to open the NLI record). **No:** left blank. **Manual Review:** the reason to check by hand. |

Rows are colored **green** (Yes), **red** (No), or **yellow** (Manual Review). The rest of your spreadsheet is unchanged.

## Opening the app

Go to **[nlisearch.streamlit.app](https://nlisearch.streamlit.app)** in any web browser. 

If the page says the app has gone to sleep, click the button to wake it up and wait a minute for it to start.

## Using the app

1. **Upload** an Excel (`.xlsx`) or `.csv` file.
2. **Check the selections:** worksheet, header row (the row with column names), and ISBN column. The app usually picks these correctly. It then tells you how many book rows it found.
3. Click **Search NLI** and keep the page open. It takes about 1 second per ISBN.
4. Review the summary (counts and percentages of Yes, No, and Manual Review), then click **Download Excel file**.

If the search is interrupted, click **Search NLI** again. Books already checked are not searched again.

## What the results mean

- **Yes:** NLI has a catalog record with this ISBN, and the record shows that NLI holds a copy.
- **No:** NLI's catalog has no record with this ISBN.
- **Manual Review:** the app could not decide. Check the NLI Notes. Common reasons:
  - the ISBN is missing or invalid
  - the connection to NLI failed (a failed search is never reported as "No")
  - NLI has a record but shows no copy on the shelf (often an e-book)
  - the record found does not list this exact ISBN (possibly a different edition)

Good to know:
- ISBN-10 and ISBN-13 are both accepted, with or without hyphens.
- A cell may contain several ISBNs, separated by `,` `;` `/` or line breaks. The book is **Yes** if any of them is found in NLI.
- ISBNs separated only by spaces, or with extra text such as `ISBN 978…` or `978… (pbk.)`, are treated as invalid. Remove the extra text before uploading.
- The same book listed twice is searched only once.
- The app searches by ISBN only. Books without an ISBN go to Manual Review.

## Troubleshooting

| Problem | What to do |
|---|---|
| The app is asleep or will not load | Click the button to wake it up and wait a minute. If it still does not load, reload the page. |
| Many rows say "Connection timeout" or "Could not connect" | The NLI catalog may be busy or down. Wait a few minutes, then click **Search NLI** again. |
| Garbled text in the preview (CSV files) | Open **CSV options** and try another encoding. For Hebrew, try Windows-1255. |

---

## For maintainers

- **Code:** `app.py` (the interface), `nli_client.py` (catalog connection and matching rules), `spreadsheet_processor.py` (reading and writing spreadsheets), and `isbn_utils.py` (ISBN checks).
- **NLI connection:** Z39.50 at `nli.alma.exlibrisgroup.com:1921/972NNL_INST`, with the public login `z39`/`z39`, driven through `yaz-client`. No API key is needed. If YAZ is not on your `PATH`, set `NLI_YAZ_CLIENT=/path/to/yaz-client`.
- **Rule for "Yes":** a returned record lists the ISBN in MARC 020 $a and has an 852 $b holdings field.
- **Tests:** `.venv/bin/pip install -r requirements-dev.txt`, then `.venv/bin/python -m pytest` (offline). Add `NLI_LIVE_TESTS=1` to also query NLI.
- **Hosting:** Streamlit Community Cloud, with YAZ installed via `packages.txt`.
  - **Official:** [nlisearch.streamlit.app](https://nlisearch.streamlit.app), deployed from [hljd/nli-isbn-search](https://github.com/hljd/nli-isbn-search). This is the link to give library staff.
  - **Testing:** [nlisearchtest.streamlit.app](https://nlisearchtest.streamlit.app). Try changes here before they reach the official app.
- **Running locally** (for development; needs YAZ: `brew install yaz` or `sudo port install yaz`):
  ```bash
  git clone https://github.com/hljd/nli-isbn-search.git
  cd nli-isbn-search
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt
  .venv/bin/streamlit run app.py
  ```
  Use `.venv/bin/streamlit` as shown; a different Python (such as Anaconda) may not have the required packages.
- **Known limits:**
  - Formulas in an uploaded sheet are not adjusted for the two inserted columns.
  - More than 20 NLI records for one ISBN goes to Manual Review.
- **References:**
  - [NLI catalog search](https://www.nli.org.il/en/search?projectName=NLI): for checking Manual Review rows by hand.
  - [NLI services for librarians](https://www.nli.org.il/en/at-your-service/librarians): NLI's Z39.50 connection details.
