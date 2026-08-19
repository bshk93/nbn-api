"""Numeric helpers matching R's, where "the obvious thing" differs from R's.

Two so far, `round()` and `mean()`, both found by the byte-identical gate
rather than by reading R's source first. Between them they moved 11 cells
across the 30 team files, each a per-game average landing on a rounding
boundary.
"""

from __future__ import annotations

import math
from decimal import ROUND_FLOOR, Decimal


def r_round(x: float, digits: int = 0) -> float:
    """`round(x, digits)` as R does it.

    The two languages round *different things*. Python rounds the exact value
    of the double: `0.05` is really 0.05000000000000000277…, which is above the
    midpoint, so `round(0.05, 1)` is `0.1`. R instead asks which of the two
    representable *doubles* either side — 0.0 and 0.1 — is nearer, and since
    the double nearest 0.1 is itself high by exactly the same margin, that is a
    genuine tie, resolved to even: `0`.

    Neither is wrong, and R's is arguably the more defensible: it rounds to the
    nearest number the output can actually hold. This matters because it is
    what six seasons of published per-game averages were computed with.

    Verified against R directly for the boundary cases 0.05, 0.15, 0.25, 0.35,
    0.45, 0.55, 2.925, 10.185, 0.5, 1.5, 2.5 — see tests/test_stats_writer.py.
    """
    if not math.isfinite(x):
        return x
    scale = Decimal(10) ** digits
    lo_i = int((Decimal(x) * scale).to_integral_value(rounding=ROUND_FLOOR))
    lo = lo_i / float(scale)
    hi = (lo_i + 1) / float(scale)

    below = Decimal(x) - Decimal(lo)
    above = Decimal(hi) - Decimal(x)
    if below < above:
        return lo
    if above < below:
        return hi
    return lo if lo_i % 2 == 0 else hi          # exact tie: go to even


def r_mean(values: list[float]) -> float:
    """`mean(x)` as R computes it: a mean plus one compensating pass.

    R's C implementation doesn't stop at `sum(x)/n`. It takes that as a first
    estimate, sums the residuals, and corrects:

        s = sum(x)/n ;  t = sum(x - s) ;  mean = s + t/n

    which recovers the error the first division threw away. For Bobby Portis'
    game scores that is the difference between 10.184999999999999 and 10.185 —
    invisible until it hits a rounding boundary, where it becomes 10.18 vs
    10.19 in a published per-game average.

    `math.fsum` for the accumulation, since R accumulates in long double and
    an exactly-rounded sum is the closest thing Python has.
    """
    n = len(values)
    s = math.fsum(values) / n
    return s + math.fsum([x - s for x in values]) / n
