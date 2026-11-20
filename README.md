# NLI ISBN Search

Checks whether the books on your list are held by the **National Library of Israel (NLI)**. Upload a spreadsheet, and the app looks up each ISBN in the NLI catalog. You then download a copy of your spreadsheet with two new columns at the front:

| Column | What it shows |
|---|---|
| **NLI Match** | **Yes**, **No**, or **Manual Review** |
| **Notes** | **Yes:** the book's title in NLI (click it to open the NLI record). **No:** left blank. **Manual Review:** the reason to check by hand. |

Rows are colored **green** (Yes), **red** (No), or **yellow** (Manual Review). The rest of your spreadsheet is unchanged.

## One-time setup

You need this once per computer. It is already done on the Judaica Mac.

1. Install YAZ, the tool that talks to the NLI catalog: `sudo port install yaz` (MacPorts) or `brew install yaz` (Homebrew).
2. In Terminal:
   ```bash
   cd ~/harvard-library-judaica-division/nli-isbn-search
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```

## Starting the app

```bash
cd ~/harvard-library-judaica-division/nli-isbn-search
.venv/bin/streamlit run app.py
```

The app opens in your browser. To stop it, press `Ctrl+C` in Terminal.

## Using the app

1. **Upload** an Excel (`.xlsx`) or `.csv` file.
2. **Check the selections:** worksheet, header row (the row with column names), and ISBN column. The app usually picks these correctly. It then tells you how many book rows it found.
3. Click **Search NLI** and keep the page open. It takes about 1 second per ISBN.
4. Review the summary (counts and percentages of Yes, No, and Manual Review), then click **Download Excel file**.

If the search is interrupted, click **Search NLI** again. Books already checked are not searched again.

## What the results mean

- **Yes:** NLI has a catalog record with this ISBN, and the record shows that NLI holds a copy.
- **No:** NLI's catalog has no record with this ISBN.
- **Manual Review:** the app could not decide. Check the Notes. Common reasons:
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
| "missing a required tool … yaz-client" | Do step 1 of the one-time setup. |
| Many rows say "Connection timeout" or "Could not connect" | Check your internet connection. Guest Wi-Fi and some VPNs block NLI's catalog connection. Then click **Search NLI** again. |
| An error mentioning `anaconda3` | Start the app with `.venv/bin/streamlit run app.py`, exactly as shown above. |
| Garbled text in the preview (CSV files) | Open **CSV options** and try another encoding. For Hebrew, try Windows-1255. |

---

## For maintainers

- **Code:** `app.py` (the interface), `nli_client.py` (catalog connection and matching rules), `spreadsheet_processor.py` (reading and writing spreadsheets), and `isbn_utils.py` (ISBN checks).
- **NLI connection:** Z39.50 at `nli.alma.exlibrisgroup.com:1921/972NNL_INST`, with the public login `z39`/`z39`, driven through `yaz-client`. No API key is needed. If YAZ is not on your `PATH`, set `NLI_YAZ_CLIENT=/path/to/yaz-client`.
- **Rule for "Yes":** a returned record lists the ISBN in MARC 020 $a and has an 852 $b holdings field.
- **Tests:** `.venv/bin/pip install -r requirements-dev.txt`, then `.venv/bin/python -m pytest` (offline). Add `NLI_LIVE_TESTS=1` to also query NLI.
- **Hosting:** the app is tested on a Mac only. On Streamlit Community Cloud, YAZ installs via `packages.txt`, but it is **unverified** whether outbound port 1921 is allowed. A Harvard server or VM that allows port 1921 is the fallback.
- **Known limits:**
  - Formulas in an uploaded sheet are not adjusted for the two inserted columns.
  - More than 20 NLI records for one ISBN goes to Manual Review.
