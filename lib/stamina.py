"""Stamina as the model sees it: the reported value, unrounded, or "unknown".

AFC PORT of lab/experiment_5/stamina.py (verbatim except imports and this note). See AFC_RUNBOOK.md.

Upstream lib/state.py renders the controlled player's stamina as

    stam = me.get("stamina", 100)
    f"stam={stam:.0f}"

`.0f` rounds to a whole number (half to even), and the sample values are 0-1 fractions,
so 0.25 -> "0", 0.5 -> "0", 0.95 -> "1". A missing value becomes "100" (another scale),
and None or a string raises inside summarize_state(), which sends the whole tick to the
fallback without calling the model.

Lab representation:
  - a finite int/float within STAMINA_RANGE -> the value with ALL its digits, positional
    notation (shortest repr that round-trips: 0.95 -> "0.95", 1 -> "1", 1e-05 -> "0.00001")
  - anything else (missing, None, bool, string, NaN, inf, out of range) -> "unknown"

STAMINA_RANGE is an ASSUMPTION from the sample data (0.65-0.95) and the 0-1 reading of it;
the server's scale is not verified. If it turns out to be 0-100, every value renders
"unknown": wrong-but-loud instead of silently misread. Change the constant then.
"""

import math
from decimal import Decimal

STAMINA_RANGE = (0.0, 1.0)
UNKNOWN = "unknown"


def format_stamina(value) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return UNKNOWN
    if not math.isfinite(value) or not STAMINA_RANGE[0] <= value <= STAMINA_RANGE[1]:
        return UNKNOWN
    return format(Decimal(repr(value)), "f")
