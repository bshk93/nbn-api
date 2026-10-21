"""routers/names.py must name players the way nbn-today/names.js does.

    venv/bin/python -m tests.test_names
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from routers.names import player_name  # noqa: E402

CASES = {
    "MCCOLLUM, CJ": "CJ McCollum",
    "JAMES, LEBRON": "LeBron James",
    "GILGEOUS-ALEXANDER, SHAI": "Shai Gilgeous-Alexander",
    "BROWN JR, MIKEL": "Mikel Brown Jr.",
    "WILLIAMS III, ROBERT": "Robert Williams III",
    "ONEALE, ROYCE": "Royce O'Neale",
    "RUSSELL, DANGELO": "D'Angelo Russell",
    "DEROZAN, DEMAR": "DeMar DeRozan",
    "FOX, DE'AARON": "De'Aaron Fox",
    "Mccollum, Cj": "CJ McCollum",
    "Stephen Curry": "Stephen Curry",
    "": "",
}
fails = [(k, player_name(k), v) for k, v in CASES.items() if player_name(k) != v]
for k, got, want in fails:
    print(f"  [FAIL] {k!r}: got {got!r}, want {want!r}")
if fails:
    sys.exit(1)
print(f"all {len(CASES)} checks passed")
