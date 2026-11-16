"""routers/txn/ never uses a name that tests patch on routers.transactions.

Tests stub data by assigning to the transactions module
(`tx.load_player_bios = ...`). Code in routers/txn/ looks names up in its own
module, so it would keep calling the real loader under such a stub, and a test
that believed it was isolated would read, or write, the live data dir. The
package's rule (routers/txn/__init__.py) is that nothing which reaches a
patched name lives there; this keeps the rule true as tests add patches.

The patched names are read off the test files themselves, by the aliases
they import routers.transactions under.

    venv/bin/python -m tests.test_txn_package
"""
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FAILS = []


def check(name, cond, extra=""):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}{(' — ' + str(extra)) if extra else ''}")
    if not cond:
        FAILS.append(name)


def patched_names() -> set[str]:
    names = set()
    files = [*(ROOT / "tests").glob("*.py"), *(ROOT / "picks_conveyance" / "tests").glob("*.py"),
             *(ROOT / "routers").glob("*.py"), *ROOT.glob("*.py")]
    for p in files:
        s = p.read_text()
        aliases = set(re.findall(r"import routers\.transactions as (\w+)", s))
        aliases |= set(re.findall(r"from (?:routers|\.) import transactions as (\w+)", s))
        if re.search(r"from (?:routers|\.) import transactions\b(?! as)", s):
            aliases.add("transactions")
        for a in aliases:
            names |= set(re.findall(re.escape(a) + r"\.([A-Za-z_]\w*)\s*=[^=]", s))
            names |= set(re.findall(r"setattr\(" + re.escape(a) + r",\s*['\"](\w+)", s))
        names |= set(re.findall(r"patch(?:\.object)?\(['\"]routers\.transactions\.(\w+)", s))
        # saved = {n: getattr(T, n) for n in ("a", "b")} ... setattr(T, n, f)
        for a in aliases:
            for m in re.finditer(r"getattr\(" + re.escape(a) + r",\s*\w+\)\s+for\s+\w+\s+in\s+\(([^)]*)\)", s):
                names |= set(re.findall(r"['\"](\w+)['\"]", m.group(1)))
    return names


patched = patched_names()
check("found the patched names", len(patched) > 20, len(patched))
for path in sorted((ROOT / "routers" / "txn").glob("*.py")):
    tree = ast.parse(path.read_text())
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    defined = {getattr(n, "name", None) for n in tree.body} | {
        t.id for n in tree.body if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)}
    bad = sorted((used | defined) & patched)
    check(f"{path.name} uses nothing tests patch on transactions", not bad, bad)

if FAILS:
    print(f"{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print("all checks passed")
