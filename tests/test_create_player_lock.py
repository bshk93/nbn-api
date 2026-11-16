"""POST /api/players saves player-bios.json under _txn_lock, like every other
bios writer. Without it a transaction saving the bios between this endpoint's
load and save was silently overwritten. Loaders are stubbed; no file is read.

    venv/bin/python -m tests.test_create_player_lock
"""
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import routers.players as pl  # noqa: E402

held = {}
lock = threading.Lock()
pl._txn_lock = lock
disk = {"bios": {}}
pl.load_player_bios = lambda: dict(disk["bios"])
pl.save_player_bios = lambda d: (held.__setitem__("at_save", lock.locked()), disk.__setitem__("bios", d))
pl._build_team_map = lambda: {}
pl.log_write = lambda *a, **k: None
pl._check_secondary_pos = lambda *a, **k: None
pl._freeze = lambda *a, **k: None

pl.create_player(pl.PlayerCreate(slug="new-guy", name="GUY, NEW", pos=["SF"]), {"name": "t", "roles": ["admin"]})
ok = held.get("at_save") is True and "new-guy" in disk["bios"] and not lock.locked()
print(f"  [{'ok' if ok else 'FAIL'}] the save happens under _txn_lock, and the lock is released after ({held})")
sys.exit(0 if ok else 1)
