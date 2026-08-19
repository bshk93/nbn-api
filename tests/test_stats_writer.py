"""The R-compatible CSV writer (stats_build/csvio.py), port spec Phase 2.

Every ported aggregation inherits this writer, so a formatting bug here fails
every slice for the same reason — and that is exactly the situation where
someone loosens the comparison instead of fixing the writer. The spec forbids
that, so the writer gets pinned before any aggregation is ported.

The second half checks against the **real 86 output files** the R build
produced, which is the only way to catch what readr actually does rather than
what its documentation says. It skips if that directory isn't present, so the
suite still runs on a box without the data.
"""

import csv
import math
import pathlib
import re
import sys

from stats_build.csvio import format_field, format_double, quote_field, render_csv

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


print("missing vs empty — different values, and readr keeps them different")
check("None is NA, not empty", format_field(None) == "NA")
check("NaN is NA too", format_double(float("nan")) == "NA")
check("empty string stays empty", format_field("") == "")
check("...which the h2h diagonal depends on", render_csv(["A"], [[""]]) == "A\n\n")

print("\nbooleans")
check("True is TRUE", format_field(True) == "TRUE")
check("False is FALSE", format_field(False) == "FALSE")
check("not 1/0 — bool is checked before int", format_field(1) == "1")

print("\nnumbers")
check("whole double drops the .0 (R writes 2790, not 2790.0)", format_double(2790.0) == "2790")
check("fractional double keeps its digits", format_double(5885.8) == "5885.8")
check("negative zero keeps its sign", format_double(-0.0) == "-0")
check("plain zero does not gain one", format_double(0.0) == "0")
check("int passes through", format_field(41) == "41")

print("\nquoting — only when needed")
check('"LAST, FIRST" is quoted', quote_field("YOUNG, TRAE") == '"YOUNG, TRAE"')
check("a plain name is not", quote_field("standings") == "standings")
check("an embedded quote is doubled", quote_field('a"b') == '"a""b"')
check("a newline forces quoting", quote_field("a\nb") == '"a\nb"')

print("\nfile shape")
out = render_csv(["A", "B"], [[1, "x"], [2, None]])
check("LF only, no CRLF", "\r" not in out)
check("trailing newline", out.endswith("\n"))
check("exact bytes", out == "A,B\n1,x\n2,NA\n")

print("\nagainst the real build output")
root = pathlib.Path("/var/lib/nothing-but-stats/derived")
files = sorted(root.rglob("*.csv")) if root.is_dir() else []
if not files:
    print("  [skip] no derived/ on this box")
else:
    NUM = re.compile(r"^-?\d+(\.\d+)?$")
    struct_bad, num_checked, num_bad = [], 0, []
    for f in files:
        original = f.read_text()
        rows = list(csv.reader(original.splitlines()))
        if "\n".join(",".join(quote_field(v) for v in r) for r in rows) + "\n" != original:
            struct_bad.append(f.name)
        for r in rows[1:]:
            for v in r:
                if NUM.match(v):
                    num_checked += 1
                    if format_double(float(v)) != v:
                        num_bad.append((f.name, v, format_double(float(v))))
    check(f"all {len(files)} files re-render byte-identically (quoting, escaping, line ends)",
          not struct_bad)
    # The one known divergence: readr's double formatter is not always shortest
    # round-trip (vroom 1.6.5, ~0.16% of arbitrary doubles get one extra
    # significant digit). It reaches exactly one value in the corpus, in the
    # OFF_RTG column. Pinned rather than tolerated -- if this count moves, the
    # writer's float rule needs the real algorithm, not a bigger allowance.
    check(f"{num_checked} numeric values render identically, apart from the known readr quirk",
          all(v[1].lstrip("-").replace(".", "").startswith("16841887793779169") for v in num_bad))
    check("the quirk is still confined to 2 cells", len(num_bad) == 2)
    print(f"       ({num_checked} numeric values checked across {len(files)} files)")

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print("all checks passed")
