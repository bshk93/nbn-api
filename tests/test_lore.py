"""`routers/lore.py` — league lore for NBN Today writers.

Reads need a member token; writes need `curator` or `bod`. Pins:

  * no token can't read; any member can
  * the index lists documents digest-first, with their `## ` sections
  * search returns blocks containing every term, digest before notes
  * a table stays one block; each list item is its own block
  * names are validated (no path traversal), unknown names are 404
  * a plain member can't write; a curator can create and replace

Writes go to a temp directory; nothing here touches $DATA_DIR/lore.

    venv/bin/python -m tests.test_lore
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import routers.auth as auth  # noqa: E402
import routers.lore as lore  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


CURATOR_TOKEN = "c" * 64
PLAIN_TOKEN = "n" * 64
MEMBERS = {
    "Editor": {"token": CURATOR_TOKEN, "roles": ["curator"], "tenures": []},
    "Nobody": {"token": PLAIN_TOKEN, "roles": [], "tenures": []},
}
auth.load_members = lambda: MEMBERS

TMP = Path(tempfile.mkdtemp(prefix="nbn-lore-test-"))
lore.LORE_DIR = TMP / "lore"
lore.LORE_DIR.mkdir()
lore.log_write = lambda info, msg: None

(lore.LORE_DIR / "lore.md").write_text(
    "# NBN lore — by storyline\n\n"
    "## 2. The alt scandal\n\n"
    "- **Dangle** owned the Bucks and ran Roastchicken.\n"
    "  Killer was the Cavs GM under him.\n"
    "- **Perim** owned the Warriors.\n\n"
    "## 3. Championships\n\n"
    "| Season | Champion |\n|---|---|\n| 2021-22 | Hawks |\n| 2022-23 | Suns |\n"
)
(lore.LORE_DIR / "2020.md").write_text(
    "# 2020 notes\n\n## 2020-06\n\n- THE RESTART: Dangle cheating with the Bucks and Cavs, Dangle again.\n"
)

app = FastAPI()
app.include_router(lore.router)
c = TestClient(app)

CURATOR = {"Authorization": "Bearer " + CURATOR_TOKEN}
PLAIN = {"Authorization": "Bearer " + PLAIN_TOKEN}

print("reading")
check("no token can't read the index", c.get("/api/lore").status_code == 401)
r = c.get("/api/lore", headers=PLAIN)
check("a member can read the index", r.status_code == 200)
docs = r.json()["docs"]
check("digest listed first", [d["name"] for d in docs] == ["lore", "2020"])
check("index carries the title", docs[0]["title"] == "NBN lore — by storyline")
check("index carries the ## sections",
      docs[0]["sections"] == ["2. The alt scandal", "3. Championships"])

r = c.get("/api/lore/lore", headers=PLAIN)
check("a document reads whole", r.status_code == 200 and "Roastchicken" in r.json()["markdown"])
check("unknown document is a 404", c.get("/api/lore/nope", headers=PLAIN).status_code == 404)
check("a bad name is a 422, not a file read",
      c.get("/api/lore/..%2Fmembers", headers=PLAIN).status_code in (404, 422))
check("an uppercase name is rejected", c.get("/api/lore/Lore", headers=PLAIN).status_code == 422)

print("searching")
check("no token can't search", c.get("/api/lore/search?q=dangle").status_code == 401)
res = c.get("/api/lore/search?q=dangle", headers=PLAIN).json()
check("finds both documents", res["total"] == 2)
check("digest hit comes first", res["results"][0]["doc"] == "lore")
check("a hit carries its section", res["results"][0]["section"] == "2. The alt scandal")
check("a list item keeps its continuation line", "Killer was the Cavs GM" in res["results"][0]["text"])
check("the next list item is its own block", "Perim" not in res["results"][0]["text"])
res = c.get("/api/lore/search?q=dangle killer", headers=PLAIN).json()
check("every term must match", res["total"] == 1 and res["results"][0]["doc"] == "lore")
res = c.get("/api/lore/search?q=hawks", headers=PLAIN).json()
check("a table is one block", res["total"] == 1 and "2022-23 | Suns" in res["results"][0]["text"])
res = c.get("/api/lore/search?q=dangle&doc=2020", headers=PLAIN).json()
check("doc= narrows the search", res["total"] == 1 and res["results"][0]["doc"] == "2020")
check("limit is honoured",
      len(c.get("/api/lore/search?q=dangle&limit=1", headers=PLAIN).json()["results"]) == 1)
check("one-letter queries are refused", c.get("/api/lore/search?q=d", headers=PLAIN).status_code == 422)

print("writing")
body = {"markdown": "# Sagas\n\n## DJ / Embiid\n\n- The owners voted DJ out."}
check("no token can't write", c.put("/api/lore/sagas", json=body).status_code == 401)
check("a plain member can't write", c.put("/api/lore/sagas", json=body, headers=PLAIN).status_code == 403)
r = c.put("/api/lore/sagas", json=body, headers=CURATOR)
check("a curator can create", r.status_code == 200 and r.json()["created"] is True)
check("the file is on disk with a trailing newline",
      (lore.LORE_DIR / "sagas.md").read_text().endswith("voted DJ out.\n"))
r = c.put("/api/lore/sagas", json={"markdown": body["markdown"] + "\n- Bronamo traded Embiid.\n"},
          headers=CURATOR)
check("a curator can replace", r.status_code == 200 and r.json()["created"] is False)
check("the new document is searchable",
      c.get("/api/lore/search?q=bronamo", headers=PLAIN).json()["results"][0]["doc"] == "sagas")
check("sagas rank between the digest and the notes",
      [d["name"] for d in c.get("/api/lore", headers=PLAIN).json()["docs"]] == ["lore", "sagas", "2020"])
check("empty markdown is refused",
      c.put("/api/lore/sagas", json={"markdown": "  "}, headers=CURATOR).status_code == 422)
check("a bad name can't be written",
      c.put("/api/lore/Bad_Name", json=body, headers=CURATOR).status_code == 422)

print()
if FAILS:
    print(f"FAILURES: {FAILS}")
    sys.exit(1)
print("ALL PASS")
