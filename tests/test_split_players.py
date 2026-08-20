"""Regression test for `_split_players` (resolve_discord_fa_signings.py).

Written 2026-08-20 after a real flagged candidate showed the bug live: "The
Sacramento Kings accept Scoot Henderson, Reed Shepard, and Stephon Castle's
2026-2027 team option" produced ONE unresolvable candidate,
raw_player="Scoot Henderson, Reed Shepard" — the first two names glued
together — because the old split pattern only ever treated a comma as a
separator when paired with "and"/"&". A 3+-name Oxford-comma list has a bare
comma between every item except the last, which never matched.

    venv/bin/python -m tests.test_split_players
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from resolve_discord_fa_signings import _split_players  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


print("\n_split_players")

check("3-name Oxford-comma list splits into three",
      _split_players("Scoot Henderson, Reed Shepard, and Stephon Castle")
      == ["Scoot Henderson", "Reed Shepard", "Stephon Castle"])

check("4-name Oxford-comma list",
      _split_players("A, B, C, and D") == ["A", "B", "C", "D"])

check("3-name list with no Oxford comma before 'and'",
      _split_players("A, B and C") == ["A", "B", "C"])

check("bare-comma list with no 'and' at all",
      _split_players("A, B, C") == ["A", "B", "C"])

check("simple two-name 'and' join still works",
      _split_players("Reggie Perry and Immanuel Quickley")
      == ["Reggie Perry", "Immanuel Quickley"])

check("two-name '&' join still works",
      _split_players("AJ Griffin & Chris Duarte") == ["AJ Griffin", "Chris Duarte"])

check("parenthetical asides are still stripped before splitting",
      _split_players("Jordan Nwora (pick 48) and Markus Howard(pick 51)")
      == ["Jordan Nwora", "Markus Howard"])

check("a single name with no separator passes through unchanged",
      _split_players("Dante Exum") == ["Dante Exum"])

if FAILS:
    print(f"\n{len(FAILS)} check(s) failed: {FAILS}")
    sys.exit(1)
print("\nall checks passed")
