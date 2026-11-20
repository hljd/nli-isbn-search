"""Normalize and validate ISBN-10 and ISBN-13 values.

Adapted from the HOLLIS ISBN Checker (hollis-book-search/isbn_utils.py),
with an added ISBN-13 -> ISBN-10 conversion for equivalent-form searches.
"""

import math
import re


def normalize_isbn(value):
    """Return a validated ISBN without spaces or hyphens, or None."""
    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, float):
        # Excel often stores ISBN-13s as numbers (9780299284947.0).
        if not math.isfinite(value) or not value.is_integer():
            return None
        value = str(int(value))
    else:
        value = str(value).strip()
        if value.endswith(".0") and value[:-2].isdigit():
            value = value[:-2]

    value = re.sub(r"[\s-]", "", value).upper()

    if len(value) == 10:
        if not re.fullmatch(r"\d{9}[\dX]", value):
            return None
        total = sum((10 - i) * int(c) for i, c in enumerate(value[:9]))
        total += 10 if value[-1] == "X" else int(value[-1])
        return value if total % 11 == 0 else None

    if len(value) == 13 and value.isdigit():
        total = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(value))
        return value if total % 10 == 0 else None

    return None


def split_isbns(value):
    """Return ordered unique valid ISBNs and whether any nonempty part is invalid.

    A cell may hold several ISBNs separated by commas, semicolons, slashes,
    pipes or line breaks. Spaces and hyphens inside an ISBN are kept so that
    "978-0-299-28494-7" stays one ISBN. Periods are not separators because
    numeric exports may end in ".0".
    """
    parts = re.split(r"[,;／/|，；、\r\n]+", value) if isinstance(value, str) else [value]
    found, invalid = [], False
    for part in parts:
        if part is None or (isinstance(part, str) and not part.strip()):
            continue
        isbn = normalize_isbn(part)
        if isbn is None:
            invalid = True
        elif isbn not in found:
            found.append(isbn)
    return found, invalid


def isbn13(isbn):
    """Convert a validated ISBN-10 to ISBN-13 (ISBN-13s are returned unchanged)."""
    if len(isbn) == 13:
        return isbn
    base = "978" + isbn[:9]
    total = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(base))
    return base + str((-total) % 10)


def isbn10(isbn):
    """Convert a validated ISBN to ISBN-10, or None if no ISBN-10 form exists.

    Only 978-prefixed ISBN-13s have an ISBN-10 equivalent.
    """
    if len(isbn) == 10:
        return isbn
    if not isbn.startswith("978"):
        return None
    base = isbn[3:12]
    check = (11 - sum((10 - i) * int(c) for i, c in enumerate(base)) % 11) % 11
    return base + ("X" if check == 10 else str(check))


def equivalent(a, b):
    """Compare validated ISBNs, allowing ISBN-10/ISBN-13 equivalence."""
    return isbn13(a) == isbn13(b)
