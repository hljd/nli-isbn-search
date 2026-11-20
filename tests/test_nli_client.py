"""NliClient behavior: classification, retries, timeouts and cleanup.

No network is used. `fake_yaz` is a stand-in yaz-client executable speaking
the same text protocol, so the real subprocess/timeout code is exercised.
"""
import os
import stat
import sys
import time

import pytest

from conftest import FIXTURES, book
import nli_client
from nli_client import NO, REVIEW, YES, NliClient, SearchError, YazSession, parse_marc

POSITIVE, NEGATIVE = "9780299284947", "9780000000002"

FAKE_YAZ = r'''#!{python}
import os, sys, time
mode = os.environ.get("FAKE_YAZ_MODE", "ok")
marc = os.environ["FAKE_YAZ_MARC"]
with open(os.environ["FAKE_YAZ_LOG"], "a") as log:
    log.write("START\n")
dump, finds = None, 0
def out(*lines):
    for line in lines:
        sys.stdout.write(line + "\n")
    sys.stdout.flush()
for line in sys.stdin:
    cmd = line.strip()
    word = cmd.split(" ")[0]
    sys.stdout.write("Z> ")
    if word == "authentication":
        out("Authentication set to Open (z39/z39)")
    elif word == "open":
        if mode == "refuse":
            out("Connecting...error: Connection refused")
        else:
            out("Connecting...OK.", "Sent initrequest.", "Connection accepted by v3 target.", "Elapsed: 0.1")
    elif word in ("format", "close"):
        pass
    elif word == "set_marcdump":
        dump = cmd.split(" ", 1)[1]
    elif word == "find":
        finds += 1
        if mode == "hang_find":
            time.sleep(60)
        if mode == "crash_find":
            sys.exit(1)
        if mode == "drop" and finds > 1:
            out("Not connected yet")
            continue
        hits = 2 if cmd.endswith("{positive}") else 0
        out("Sent searchRequest.", "Received SearchResponse.", "Search was a success.",
            "Number of hits: %d, setno %d" % (hits, finds), "records returned: 0", "Elapsed: 0.1")
    elif word == "show":
        n = int(cmd.split("+")[1])
        with open(dump, "ab") as f, open(marc, "rb") as src:
            f.write(src.read())
        out("Sent presentRequest (1+%d)." % n, "Records: %d" % n, "[972NNL_INST]Record type: USmarc")
    elif word == "quit":
        out("See you later, alligator.")
        break
    else:
        out("Unknown command: %s." % word, "Type 'help' for list of commands")
'''


@pytest.fixture
def fake_yaz(tmp_path, monkeypatch):
    path = tmp_path / "yaz-client"
    path.write_text(FAKE_YAZ.replace("{python}", sys.executable).replace("{positive}", POSITIVE))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "starts.log"
    monkeypatch.setenv("FAKE_YAZ_MARC", str(FIXTURES / "nli_9780299284947.mrc"))
    monkeypatch.setenv("FAKE_YAZ_LOG", str(log))
    monkeypatch.setattr(nli_client, "REQUEST_GAP_SECONDS", 0)

    def starts():
        return log.read_text().count("START") if log.exists() else 0
    return str(path), starts


def client_for(path, sleeps=None, command_timeout=5):
    return NliClient(
        path, sleep=(sleeps.append if sleeps is not None else lambda s: None),
        session_factory=lambda exe: YazSession(exe, connect_timeout=5, command_timeout=command_timeout))


# --- end-to-end through a real subprocess --------------------------------

def test_positive_negative_and_single_connection(fake_yaz):
    path, starts = fake_yaz
    with client_for(path) as client:
        yes = client.search_isbn(POSITIVE)
        no = client.search_isbn(NEGATIVE)
        ten = client.search_isbn("0299284948")  # ISBN-10 of the positive
    assert yes.status == YES and yes.records[0].record_id == "990034518460205171"
    assert no.status == NO and not no.failed
    assert ten.status == YES
    assert starts() == 1  # one yaz-client process / connection for the batch


def test_timeout_is_manual_review_and_bounded(fake_yaz, monkeypatch):
    path, starts = fake_yaz
    monkeypatch.setenv("FAKE_YAZ_MODE", "hang_find")
    sleeps, began = [], time.monotonic()
    with client_for(path, sleeps, command_timeout=1) as client:
        result = client.search_isbn(NEGATIVE)
    assert result.status == REVIEW and result.failed
    assert "timeout" in result.reason.lower()
    assert starts() == 3 and sleeps == [3, 10]   # 3 attempts, bounded backoff
    assert time.monotonic() - began < 15          # never waits indefinitely
    assert client.session is None


def test_crash_is_manual_review(fake_yaz, monkeypatch):
    path, _ = fake_yaz
    monkeypatch.setenv("FAKE_YAZ_MODE", "crash_find")
    with client_for(path) as client:
        result = client.search_isbn(POSITIVE)
    assert result.status == REVIEW and result.failed and "stopped unexpectedly" in result.reason


def test_connection_refused_is_manual_review(fake_yaz, monkeypatch):
    path, starts = fake_yaz
    monkeypatch.setenv("FAKE_YAZ_MODE", "refuse")
    with client_for(path) as client:
        result = client.search_isbn(NEGATIVE)
    assert result.status == REVIEW and result.failed and "Could not connect" in result.reason
    assert starts() == 3


def test_dropped_connection_reconnects(fake_yaz, monkeypatch):
    path, starts = fake_yaz
    monkeypatch.setenv("FAKE_YAZ_MODE", "drop")  # every 2nd+ find on a connection fails
    with client_for(path) as client:
        assert client.search_isbn(POSITIVE).status == YES
        assert client.search_isbn(POSITIVE).status == YES  # after reconnecting
    assert starts() == 2


def test_missing_executable_is_reported(tmp_path):
    with NliClient(str(tmp_path / "nope"), sleep=lambda s: None) as client:
        result = client.search_isbn(POSITIVE)
    assert result.status == REVIEW and result.failed and "Could not start yaz-client" in result.reason


# --- logic with an in-memory session ---------------------------------------

class ScriptedSession:
    """Answers search(term) from a {term: hits|exception} map."""
    def __init__(self, script, data=b""):
        self.script, self.data, self.searched = script, data, []

    def open(self):
        pass

    def close(self):
        pass

    def search(self, term):
        self.searched.append(term)
        outcome = self.script.get(term, 0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def fetch(self, count):
        return parse_marc(self.data)[:count]


def scripted_client(session):
    return NliClient("/bin/true", session_factory=lambda exe: session, sleep=lambda s: None)


def test_no_requires_both_isbn_forms_to_have_zero_hits():
    session = ScriptedSession({})
    assert scripted_client(session).search_isbn("0299284948").status == NO
    assert session.searched == ["9780299284947", "0299284948"]


def test_isbn10_fallback_finds_record():
    session = ScriptedSession({"0299284948": 1}, book("990001", ["0299284948"]))
    result = scripted_client(session).search_isbn("9780299284947")
    assert result.status == YES


def test_too_many_hits_needs_review_without_failure():
    session = ScriptedSession({POSITIVE: nli_client.MAX_RECORDS + 1})
    result = scripted_client(session).search_isbn(POSITIVE)
    assert result.status == REVIEW and not result.failed and "too many" in result.reason


def test_non_retryable_error_is_not_retried():
    session = ScriptedSession({POSITIVE: SearchError("bad", retryable=False)})
    result = scripted_client(session).search_isbn(POSITIVE)
    assert result.status == REVIEW and result.failed and session.searched == [POSITIVE]


def test_failed_second_form_is_not_a_no():
    """ISBN-13 had zero hits but the ISBN-10 search failed: not a 'No'."""
    session = ScriptedSession({"0299284948": SearchError("Connection timeout")})
    result = scripted_client(session).search_isbn(POSITIVE)
    assert result.status == REVIEW and result.failed


def test_find_yaz_client_honors_env(tmp_path, monkeypatch):
    exe = tmp_path / "my-yaz"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setenv("NLI_YAZ_CLIENT", str(exe))
    assert nli_client.find_yaz_client() == str(exe)
    monkeypatch.setenv("NLI_YAZ_CLIENT", str(tmp_path / "missing"))
    with pytest.raises(nli_client.YazNotFoundError):
        nli_client.find_yaz_client()
