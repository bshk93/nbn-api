"""The ETag middleware (etag.py) and the shared response cache behind
`/api/players` and `/api/attributes/current` (storage.shared_json_response).

What must hold: a JSON GET carries an ETag and `no-cache`; asking again with
that ETag (strong, or weakened by nginx's gzip) gets an empty 304; a changed
body gets a 200 with a new tag; non-JSON, non-GET and non-200 answers are
left alone. The shared response returns exactly what FastAPI would have
encoded, and a write to the file is seen on the next call.

    venv/bin/python -m tests.test_etag
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import PlainTextResponse  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from etag import ETagMiddleware  # noqa: E402
from routers import storage  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


state = {"n": 1}
app = FastAPI()
app.add_middleware(ETagMiddleware)


@app.get("/j")
def j():
    return {"n": state["n"]}


@app.post("/j")
def jp():
    return {"n": state["n"]}


@app.get("/t")
def t():
    return PlainTextResponse("hi")


@app.get("/missing")
def missing():
    raise HTTPException(404, "no")


c = TestClient(app)

print("JSON GET")
r = c.get("/j")
tag = r.headers.get("etag")
check("has an ETag", bool(tag))
check("must revalidate", r.headers.get("cache-control") == "private, no-cache")
check("body unchanged", r.json() == {"n": 1})

print("revalidating")
r2 = c.get("/j", headers={"If-None-Match": tag})
check("same body -> 304", r2.status_code == 304 and r2.content == b"")
check("304 repeats the ETag", r2.headers.get("etag") == tag)
check("nginx's weak form matches too", c.get("/j", headers={"If-None-Match": "W/" + tag}).status_code == 304)
state["n"] = 2
r3 = c.get("/j", headers={"If-None-Match": tag})
check("changed body -> 200", r3.status_code == 200 and r3.json() == {"n": 2})
check("with a new ETag", r3.headers.get("etag") not in (None, tag))

print("left alone")
check("POST", "etag" not in c.post("/j").headers)
check("non-JSON", "etag" not in c.get("/t").headers)
check("404", "etag" not in c.get("/missing").headers)

print("shared_json_response")
tmp = Path(tempfile.mkdtemp()) / "f.json"
storage._atomic_write(tmp, json.dumps({"a": "é", "b": [1, 2]}))
builds = []


def build():
    builds.append(1)
    return storage._load_json(tmp, {})


first = storage.shared_json_response("t", [tmp], build)
second = storage.shared_json_response("t", [tmp], build)
from fastapi.responses import JSONResponse  # noqa: E402
check("same bytes FastAPI would send", first.body == JSONResponse({"a": "é", "b": [1, 2]}).body)
check("built once while the file is unchanged", len(builds) == 1 and second.body == first.body)
storage._atomic_write(tmp, json.dumps({"a": "x"}))
third = storage.shared_json_response("t", [tmp], build)
check("a write is seen on the next call", json.loads(third.body) == {"a": "x"} and len(builds) == 2)

print()
if FAILS:
    print(f"{len(FAILS)} FAILED")
    sys.exit(1)
print("all checks passed")
