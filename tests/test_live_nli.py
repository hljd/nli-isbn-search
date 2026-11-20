"""Live lookups against NLI's Z39.50 server.

Skipped unless NLI_LIVE_TESTS=1:
    NLI_LIVE_TESTS=1 .venv/bin/python -m pytest tests/test_live_nli.py -v
"""
import os

import pytest

from nli_client import NO, REVIEW, YES, NliClient

pytestmark = pytest.mark.skipif(os.environ.get("NLI_LIVE_TESTS") != "1",
                                reason="set NLI_LIVE_TESTS=1 to query the real NLI catalog")


def test_verified_examples():
    with NliClient() as client:
        positive = client.search_isbn("9780299284947")
        negative = client.search_isbn("9780000000002")
        ebook = client.search_isbn("9780299284930")
    assert positive.status == YES
    assert positive.records[0].record_id == "990034518460205171"
    assert "origins of Israel" in positive.records[0].title
    assert negative.status == NO and not negative.failed
    assert ebook.status == REVIEW and not ebook.failed  # record exists, no 852 holdings
