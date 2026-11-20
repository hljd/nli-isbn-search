"""ISBN lookups in the National Library of Israel catalog over Z39.50.

How it works
------------
NLI's REST API is unreliable (HTTP 502), but its Z39.50 service works. We drive
the YAZ command-line client (``yaz-client``) as ONE long-lived subprocess per
batch, so a whole spreadsheet uses a single Z39.50 connection instead of one
connection per ISBN:

* Commands are written to yaz-client's stdin; a reader thread collects stdout.
* After every command we also send a made-up command (e.g. ``zzend7``).
  yaz-client processes commands strictly in order and answers the made-up one
  with ``Unknown command: zzend7.``, which tells us the real command is done.
  Every wait has a timeout, so a stalled network can never hang the app.
* ``set_marcdump`` makes yaz-client append each retrieved record, as binary
  MARC (ISO 2709), to a file. We parse that file's structured fields
  (001, 020, 245, 852) instead of scraping display text.
* Records are requested with ``show 1+N`` only after ``find`` reports N hits:
  NLI's server returns no records at all if more are requested than exist.

Failures (timeouts, dropped connections, unreadable responses) are retried a
bounded number of times on a fresh connection. If they persist, the ISBN is
reported as "Manual Review" -- never as "No".
"""
import logging
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field

from isbn_utils import equivalent, isbn10, isbn13, normalize_isbn

LOG = logging.getLogger(__name__)

# Connection settings published by NLI for its Z39.50 service (not secrets).
HOST = "nli.alma.exlibrisgroup.com"
PORT = 1921
DATABASE = "972NNL_INST"
USERNAME = "z39"
PASSWORD = "z39"
ISBN_ATTRIBUTE = "1=7"  # Bib-1 "ISBN" use attribute.

# Tuning defaults (not limits documented by NLI).
CONNECT_TIMEOUT = 20      # seconds to establish a connection
COMMAND_TIMEOUT = 30      # seconds for one search or record retrieval
REQUEST_GAP_SECONDS = 0.3  # polite pause between searches on one connection
RETRY_DELAYS = (3, 10)    # => up to 3 attempts per search
MAX_RECORDS = 20          # more hits than this for one ISBN => Manual Review

RECORD_URL = "https://www.nli.org.il/en/books/NNL_ALEPH{}/NLI"

YES, NO, REVIEW = "Yes", "No", "Manual Review"

# Where yaz-client is commonly installed, checked after $NLI_YAZ_CLIENT and PATH.
YAZ_CANDIDATES = (
    "/opt/local/bin/yaz-client",     # MacPorts
    "/opt/homebrew/bin/yaz-client",  # Homebrew (Apple silicon)
    "/usr/local/bin/yaz-client",     # Homebrew (Intel) / source builds
    "/usr/bin/yaz-client",           # Linux packages (apt install yaz)
)


class SearchError(Exception):
    """A search could not be completed. Messages are shown to staff."""

    def __init__(self, message, retryable=True):
        super().__init__(message)
        self.retryable = retryable


class YazNotFoundError(RuntimeError):
    pass


def find_yaz_client():
    """Return the path to yaz-client, or raise YazNotFoundError."""
    configured = os.environ.get("NLI_YAZ_CLIENT", "").strip()
    if configured:
        if os.path.isfile(configured) and os.access(configured, os.X_OK):
            return configured
        raise YazNotFoundError(f"NLI_YAZ_CLIENT points to {configured!r}, which is not an executable file.")
    found = shutil.which("yaz-client")
    if found:
        return found
    for candidate in YAZ_CANDIDATES:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    raise YazNotFoundError(
        "Could not find yaz-client. Install YAZ (MacPorts: 'sudo port install yaz'; "
        "Homebrew: 'brew install yaz'; Debian/Ubuntu: 'apt install yaz') or set "
        "NLI_YAZ_CLIENT to its full path."
    )


# --------------------------------------------------------------------------
# MARC (ISO 2709) parsing
# --------------------------------------------------------------------------

@dataclass
class MarcRecord:
    leader: str
    fields: list  # [(tag, str)] for control fields; [(tag, [(code, value)])] otherwise

    def control(self, tag):
        return next((value for t, value in self.fields if t == tag), None)

    def subfields(self, tag, code):
        return [value for t, subs in self.fields if t == tag and not isinstance(subs, str)
                for c, value in subs if c == code]

    def data_fields(self, tag):
        return [subs for t, subs in self.fields if t == tag and not isinstance(subs, str)]


def parse_marc(data):
    """Parse concatenated ISO 2709 records. Raises ValueError if malformed."""
    records, pos = [], 0
    while pos < len(data):
        if data[pos:pos + 1] in (b"\n", b"\r", b" "):
            pos += 1
            continue
        try:
            length = int(data[pos:pos + 5])
            base = int(data[pos + 12:pos + 17])
        except ValueError:
            raise ValueError("Invalid MARC leader.") from None
        record = data[pos:pos + length]
        if length < 25 or len(record) != length or not record.endswith(b"\x1d"):
            raise ValueError("Truncated MARC record.")
        leader = record[:24].decode("ascii", "replace")
        directory = record[24:base - 1]
        if len(directory) % 12:
            raise ValueError("Invalid MARC directory.")
        fields = []
        for i in range(0, len(directory), 12):
            entry = directory[i:i + 12].decode("ascii", "replace")
            tag, size, start = entry[:3], int(entry[3:7]), int(entry[7:12])
            raw = record[base + start:base + start + size].rstrip(b"\x1e")
            if tag < "010":
                fields.append((tag, raw.decode("utf-8", "replace").strip()))
            else:
                subs = []
                for chunk in raw[2:].split(b"\x1f")[1:]:
                    if chunk:
                        subs.append((chr(chunk[0]), chunk[1:].decode("utf-8", "replace").strip()))
                fields.append((tag, subs))
        records.append(MarcRecord(leader, fields))
        pos += length
    return records


# --------------------------------------------------------------------------
# Record interpretation and classification
# --------------------------------------------------------------------------

ISBN_TOKEN = re.compile(r"(?<![0-9Xx])(?:\d[\s-]*){9,12}[\dXx](?![0-9Xx])")


@dataclass
class NliRecord:
    record_id: str
    title: str
    isbns: set            # valid ISBNs from 020 $a (the record's own ISBNs)
    holdings: list        # e.g. ["NLI NLX_STACKS"] from 852 $b/$c
    online: bool = False  # 007/337 say "computer"/online resource

    @property
    def url(self):
        return RECORD_URL.format(self.record_id)


def _isbns_in(values):
    found = set()
    for value in values:
        for token in ISBN_TOKEN.findall(value):
            isbn = normalize_isbn(token)
            if isbn:
                found.add(isbn)
    return found


def summarize(marc):
    record_id = (marc.control("001") or "").strip()
    if not record_id.isdigit():
        raise ValueError("A returned record has no NLI record ID (MARC 001).")
    title = " ".join(marc.subfields("245", "a") + marc.subfields("245", "b"))
    title = re.sub(r"\s+", " ", title).strip().rstrip(" /:;,=.").strip()
    title = re.sub(r"\s+([:;])", r"\1", title)  # cataloging " : " -> ": "
    holdings = []
    for subs in marc.data_fields("852"):
        parts = [value for code, value in subs if code in "bc" and value]
        if any(code == "b" and value for code, value in subs):
            holdings.append(" ".join(parts))
    online = ((marc.control("007") or "").startswith("c")
              or "computer" in [v.lower() for v in marc.subfields("337", "a")])
    return NliRecord(record_id, title or "(no title)", _isbns_in(marc.subfields("020", "a")),
                     holdings, online)


@dataclass
class SearchResult:
    status: str                                  # YES, NO or REVIEW
    records: list = field(default_factory=list)  # NliRecords supporting the result
    reason: str = ""
    failed: bool = False                         # True if the search itself failed


def classify(isbn, records):
    """Decide Yes / Manual Review for a search that returned records.

    * Yes: at least one record lists this ISBN (or its ISBN-10/13 equivalent)
      in its own ISBN field (020 $a) AND has an NLI holdings field (852 $b).
    * Manual Review: records came back, but none passes both checks. (Example:
      an e-book record that mentions our ISBN only as a related print edition
      in 776 $z is NOT counted as a match.)
    """
    matching = [r for r in records if any(equivalent(isbn, x) for x in r.isbns)]
    held = [r for r in matching if r.holdings]
    if held:
        return SearchResult(YES, held)
    if matching:
        reason = "NLI record lists this ISBN, but no NLI holdings (MARC 852) are recorded"
        if all(r.online for r in matching):
            reason += "; it appears to be an online resource"
        return SearchResult(REVIEW, matching, reason)
    return SearchResult(REVIEW, records,
                        f"NLI returned {len(records)} record(s), but none lists this ISBN "
                        "in its own ISBN field (MARC 020); possibly a related edition")


# --------------------------------------------------------------------------
# yaz-client session
# --------------------------------------------------------------------------

class YazSession:
    """One yaz-client process holding one Z39.50 connection."""

    def __init__(self, executable, connect_timeout=CONNECT_TIMEOUT, command_timeout=COMMAND_TIMEOUT):
        self.executable = executable
        self.connect_timeout = connect_timeout
        self.command_timeout = command_timeout
        self.proc = None
        self.workdir = None
        self.lines = queue.Queue()
        self.sequence = 0

    # -- process management ------------------------------------------------
    def open(self):
        self.workdir = tempfile.mkdtemp(prefix="nli-yaz-")
        self.dump_path = os.path.join(self.workdir, "records.mrc")
        try:
            self.proc = subprocess.Popen(
                [self.executable], cwd=self.workdir,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            )
        except OSError as exc:
            self.close()
            raise SearchError(f"Could not start yaz-client ({exc.strerror}).", retryable=False) from None
        threading.Thread(target=self._read_output, args=(self.proc.stdout, self.lines), daemon=True).start()
        try:
            self.command(f"authentication {USERNAME}/{PASSWORD}", 5)
            lines = self.command(f"open {HOST}:{PORT}/{DATABASE}", self.connect_timeout)
            if not any("Connection accepted" in line for line in lines):
                detail = next((line for line in lines if line and not line.startswith("Connecting")), "no response")
                raise SearchError(f"Could not connect to the NLI catalog ({detail}).")
            self.command("format usmarc", 5)
            # Relative path: yaz-client runs inside our private temp folder.
            self.command("set_marcdump records.mrc", 5)
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _read_output(stream, lines):
        try:
            for raw in iter(stream.readline, b""):
                lines.put(raw.decode("utf-8", "replace"))
        except (OSError, ValueError):
            pass
        lines.put(None)  # end of output: the process exited

    def close(self):
        proc, self.proc = self.proc, None
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.stdin.write(b"quit\n")
                    proc.stdin.flush()
                    proc.wait(timeout=3)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
            finally:
                if proc.poll() is None:
                    proc.kill()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        pass
                for stream in (proc.stdin, proc.stdout):
                    try:
                        stream.close()
                    except (OSError, ValueError):
                        pass
        if self.workdir:
            shutil.rmtree(self.workdir, ignore_errors=True)
            self.workdir = None

    # -- commands -------------------------------------------------------------
    def command(self, text, timeout=None):
        """Run one yaz-client command; return its output lines."""
        if self.proc is None or self.proc.poll() is not None:
            raise SearchError("The connection to the NLI catalog was lost.")
        self.sequence += 1
        marker = f"zzend{self.sequence}"
        try:
            self.proc.stdin.write(f"{text}\n{marker}\n".encode("utf-8"))
            self.proc.stdin.flush()
        except (OSError, ValueError):
            raise SearchError("The connection to the NLI catalog was lost.") from None
        deadline = time.monotonic() + (timeout or self.command_timeout)
        output = []
        timeout_error = SearchError("Connection timeout: the NLI catalog did not respond in time.")
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise timeout_error
            try:
                line = self.lines.get(timeout=remaining)
            except queue.Empty:
                raise timeout_error from None
            if line is None:  # the reader thread saw the process exit
                raise SearchError("yaz-client stopped unexpectedly.")
            line = re.sub(r"^(Z> )+", "", line.rstrip("\r\n"))
            if line == f"Unknown command: {marker}.":
                return output
            if not line.startswith("Unknown command: zzend") and not line.startswith("Type 'help'"):
                output.append(line)

    def search(self, isbn):
        """Return the hit count for an ISBN search."""
        lines = self.command(f"find @attr {ISBN_ATTRIBUTE} {isbn}")
        if any("Not connected" in line for line in lines):
            raise SearchError("The connection to the NLI catalog was lost.")
        hits = next((re.search(r"Number of hits: (\d+)", line) for line in lines
                     if "Number of hits:" in line), None)
        if not any("Search was a success" in line for line in lines) or hits is None:
            detail = next((line for line in lines if "iagnostic" in line or "failure" in line), "")
            raise SearchError("NLI search did not complete" + (f" ({detail})" if detail else "") + ".")
        return int(hits.group(1))

    def fetch(self, count):
        """Retrieve the first `count` records of the last search as MARC."""
        start = os.path.getsize(self.dump_path) if os.path.exists(self.dump_path) else 0
        lines = self.command(f"show 1+{count}")
        received = next((re.search(r"^Records: (\d+)", line) for line in lines
                         if line.startswith("Records:")), None)
        if received is None or int(received.group(1)) != count:
            raise SearchError("NLI did not return the expected records.")
        # yaz-client writes the dump as it prints; allow a moment for the OS.
        for _ in range(10):
            with open(self.dump_path, "rb") as handle:
                handle.seek(start)
                data = handle.read()
            try:
                records = parse_marc(data)
            except ValueError:
                records = None
            if records is not None and len(records) == count:
                return records
            time.sleep(0.1)
        raise SearchError("The NLI record data could not be read.")


# --------------------------------------------------------------------------
# High-level client
# --------------------------------------------------------------------------

class NliClient:
    """Search ISBNs over one reusable connection, reconnecting when needed.

    Use as a context manager (or call close()) so yaz-client always exits.
    """

    def __init__(self, executable=None, session_factory=YazSession, sleep=time.sleep,
                 status_callback=None):
        self.executable = executable or find_yaz_client()
        self.session_factory = session_factory
        self.sleep = sleep
        self.status_callback = status_callback
        self.session = None
        self.last_request = 0.0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        if self.session is not None:
            session, self.session = self.session, None
            session.close()

    def _status(self, message):
        if self.status_callback:
            self.status_callback(message)

    def _attempt(self, action):
        attempts = len(RETRY_DELAYS) + 1
        for attempt in range(attempts):
            try:
                if self.session is None:
                    session = self.session_factory(self.executable)
                    session.open()
                    self.session = session
                wait = self.last_request + REQUEST_GAP_SECONDS - time.monotonic()
                if wait > 0:
                    self.sleep(wait)
                try:
                    return action(self.session)
                finally:
                    self.last_request = time.monotonic()
            except SearchError as exc:
                self.close()  # never reuse a connection in an unknown state
                LOG.warning("NLI search attempt %s failed: %s", attempt + 1, exc)
                if not exc.retryable or attempt == attempts - 1:
                    raise
                delay = RETRY_DELAYS[attempt]
                self._status(f"{exc} Reconnecting in {delay} seconds (attempt {attempt + 2} of {attempts})…")
                self.sleep(delay)

    def _lookup(self, term):
        """Search one ISBN form; return its hit count and parsed records."""
        def action(session):
            hits = session.search(term)
            if hits == 0 or hits > MAX_RECORDS:
                return hits, []
            try:
                return hits, [summarize(m) for m in session.fetch(hits)]
            except ValueError as exc:
                raise SearchError(f"The NLI record data could not be interpreted: {exc}") from None
        return self._attempt(action)

    def search_isbn(self, isbn):
        """Search one validated ISBN. Never raises SearchError: failures
        become Manual Review results with failed=True."""
        forms = [isbn13(isbn)]
        alternate = isbn10(isbn)
        if alternate and alternate not in forms:
            forms.append(alternate)  # in case NLI indexed only the ISBN-10 form
        records = {}
        try:
            for form in forms:
                hits, found = self._lookup(form)
                if hits > MAX_RECORDS:
                    return SearchResult(REVIEW, reason=f"NLI returned {hits} records for this ISBN, "
                                                       "too many to verify automatically")
                for record in found:
                    records.setdefault(record.record_id, record)
                if records and classify(isbn, list(records.values())).status == YES:
                    break  # verified; no need to search the other ISBN form
        except SearchError as exc:
            return SearchResult(REVIEW, reason=str(exc).rstrip("."), failed=True)
        if not records:
            return SearchResult(NO)  # every search completed with zero hits
        return classify(isbn, list(records.values()))
