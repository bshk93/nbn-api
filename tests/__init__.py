"""Every test module runs as `python -m tests.<name>`, so this runs first in each.

It points the NB¥ wallet at a throwaway directory before any test can touch it.
A test that reaches a wallet write by a side door — a donation credits NB¥, a
new member gets a starting grant — would otherwise write the live ledger and
the live `member-balances.json`. That happened once (2026-09-25, via
test_donations), and it is the kind of thing a per-test redirect only catches
after the fact. A test that wants its own files still sets them itself.
"""
import tempfile
from pathlib import Path

from routers import wallet as _wallet

_tmp = Path(tempfile.mkdtemp(prefix="nbn-wallet-test-"))
_wallet.LEDGER_FILE = _tmp / "nbyen-ledger.jsonl"
_wallet.BALANCES_FILE = _tmp / "member-balances.json"
