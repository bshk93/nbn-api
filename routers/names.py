"""Player names for display: "MCCOLLUM, CJ" → "CJ McCollum".

A Python mirror of nbn-today/names.js, which every page uses, so a link card
names a player the way the page it links to does. `_display_name` in
players.py only title-cases ("Cj Mccollum"). Keep the word lists in step with
names.js; tests/test_names.py pins the same cases.
"""
from __future__ import annotations

import re

_WORDS = {w.upper().replace("'", "").replace("’", ""): w for w in (
    "DaRon", "DeMar", "DeRozan", "DiVincenzo", "LaMelo", "LaRavia", "LaVine",
    "LeBron", "LeVert", "MarJon", "TyTy", "VanVleet",
    "D'Angelo", "Day'Ron", "De'Aaron", "De'Anthony", "Ja'Kobe", "Ja'Kobi",
    "Kel'el", "N'Diaye", "N'Faly", "Nae'Qwan", "Nah'Shon", "O'Neale", "da",
)}
_INITIALS = {"AJ", "BJ", "CJ", "DJ", "EJ", "GG", "JB", "JD", "JJ", "JT", "KC", "KJ",
             "LJ", "OG", "PJ", "RJ", "TJ", "VJ"}


def _case_word(word: str) -> str:
    up = word.upper()
    known = _WORDS.get(up.replace("'", "").replace("’", ""))
    if known:
        return known
    if up in _INITIALS or re.fullmatch(r"II|III|IV|V", up):
        return up
    if up in ("JR", "JR."):
        return "Jr."
    if up in ("SR", "SR."):
        return "Sr."
    out = []
    for part in re.split(r"([-'’])", up.lower()):
        if len(part) > 2 and part.startswith("mc"):
            out.append("Mc" + part[2].upper() + part[3:])
        else:
            out.append(part[:1].upper() + part[1:])
    return "".join(out)


def player_name(raw: str) -> str:
    """'LAST, FIRST' (any case) → 'First Last'; no comma → re-cased as is."""
    s = (raw or "").strip()
    if not s:
        return ""
    if "," in s:
        last, _, first = s.partition(",")
        s = f"{first.strip()} {last.strip()}"
    return " ".join(_case_word(w) for w in s.split())
