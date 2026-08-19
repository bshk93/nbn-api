"""Reproduce `readr::write_csv`'s output format, byte for byte.

The port's acceptance test is byte-identical output, so the writer is the
first thing that has to be right — every ported aggregation inherits it, and a
formatting bug would fail every slice for the same reason and tempt someone
into loosening the comparison instead.

The contract below was measured from the 86 files the R build actually
produces (2026-08-19), not read off readr's docs:

    line endings   LF only, no CRLF anywhere, trailing newline on every file
    encoding       UTF-8, no BOM
    missing        `NA` -- 14,903 of them in the corpus. **Not** an empty field
    empty string   stays empty: 403 in the corpus (the h2h matrix diagonal),
                   so "" and NA are different values and must not be conflated
    booleans       TRUE / FALSE, uppercase
    quoting        only when the field needs it (comma, quote, CR or LF);
                   50 of 86 files contain a quote, all from "LAST, FIRST"
                   names and "20-21, 21-22" season lists
    numbers        shortest representation that round-trips, and a whole
                   double loses its `.0` -- R writes 2790, where Python's
                   str(2790.0) would give "2790.0". No scientific notation
                   appears in the corpus; the guard below refuses to invent it

`test_stats_writer.py` checks the float rule against all 26,827 decimal values
in the real corpus, which is the part most likely to drift.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Iterable, Sequence

NA = "NA"
_NEEDS_QUOTE = ('"', ",", "\n", "\r")


def format_field(value: Any) -> str:
    """One value as readr would render it (unquoted; quoting is separate)."""
    if value is None:
        return NA
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float):
        return format_double(value)
    if isinstance(value, int):
        return str(value)
    return str(value)


def format_double(x: float) -> str:
    """A double as R prints it: shortest round-trip, no trailing `.0`.

    NaN is `NA`, matching how a missing numeric reaches the file. Infinities
    and exponent-form values do not occur anywhere in the corpus; rather than
    guess at R's rendering, they raise -- a wrong guess here would be a silent
    one-cell difference in a 250-row file.
    """
    if math.isnan(x):
        return NA
    if math.isinf(x):
        raise ValueError("infinite value has no verified R rendering")
    if x == int(x) and abs(x) < 1e15:
        # Negative zero keeps its sign: R writes `-0`, and it really occurs --
        # a point differential of exactly zero arrived at from below.
        sign = "-" if math.copysign(1.0, x) < 0 and x == 0 else ""
        return sign + str(int(x))
    s = repr(float(x))
    if "e" in s or "E" in s:
        raise ValueError(f"exponent form has no verified R rendering: {s}")
    return s


def quote_field(text: str) -> str:
    if any(c in text for c in _NEEDS_QUOTE):
        return '"' + text.replace('"', '""') + '"'
    return text


def render_row(values: Iterable[Any]) -> str:
    return ",".join(quote_field(format_field(v)) for v in values)


def render_csv(header: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    out = [render_row(header)]
    out.extend(render_row(r) for r in rows)
    return "\n".join(out) + "\n"


def write_csv(path: Path, header: Sequence[str], rows: Iterable[Sequence[Any]]) -> None:
    """Write atomically: a half-written CSV is a served CSV here."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(render_csv(header, rows), encoding="utf-8", newline="")
    tmp.replace(path)
