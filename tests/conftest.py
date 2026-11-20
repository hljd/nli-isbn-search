import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def marc_record(fields, leader_status="n"):
    """Build a binary ISO 2709 record from [(tag, value)] pairs.

    Control fields (tag < 010) take a string; data fields take
    (indicators, [(code, value), ...]).
    """
    directory, body = b"", b""
    for tag, value in fields:
        if tag < "010":
            data = value.encode() + b"\x1e"
        else:
            indicators, subfields = value
            data = indicators.encode() + b"".join(
                b"\x1f" + code.encode() + text.encode() for code, text in subfields) + b"\x1e"
        directory += f"{tag}{len(data):04d}{len(body):05d}".encode()
        body += data
    base = 24 + len(directory) + 1
    length = base + len(body) + 1
    leader = f"{length:05d}{leader_status}am a22{base:05d} a 4500".encode()
    return leader + directory + b"\x1e" + body + b"\x1d"


def book(record_id, isbns, title="A book", holdings=True, extra=()):
    fields = [("001", record_id)]
    fields += [("020", ("  ", [("a", isbn)])) for isbn in isbns]
    fields.append(("245", ("10", [("a", title + " /"), ("c", "by Someone.")])))
    fields += list(extra)
    if holdings:
        fields.append(("852", ("8 ", [("b", "NLI"), ("c", "NLX_STACKS")])))
    return marc_record(fields)
