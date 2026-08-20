"""Plumbing tests for the five `/api/validate/*` endpoints — the request layer,
not the rules.

Every other suite here imports the validators and calls them directly
(`test_owner_self_serve.py` says so outright: "These call the pure functions
directly and never POST"). That covers the rules thoroughly and leaves the code
*around* them — the endpoint functions that read the request model and assemble
the response — never executed under test at all.

That gap has already cost us once. `_validate_offer_sheet` had 263 lines of
tests and was fine, while `validate_offer_sheet`, the ~40-line endpoint wrapping
it, still read `body.outcome` — a field dropped from `OfferSheetDetails` when
offer sheets were split into two transactions (§ 3.15). Every call returned 500,
including the simulator's whole offer-sheet mode, and nothing noticed because
nothing ever constructed a request. One call with any body would have caught it.

So these assert **shape, not legality**: that a well-formed request answers with
`{legal, checks, fact_sheet}` and never 5xx, that a request naming a team or
player that doesn't exist is refused with a 400 rather than scored vacuously
(`_require_validatable`'s entire reason for existing), and that a body missing an
optional field still works. A verdict of "illegal" is a perfectly good result
here — the rules suites are what judge whether it's the *right* verdict.

**These endpoints never write** — that is a documented invariant of the
simulator (no roster, bio, team-state or ledger change, and no API lock), which
is what makes it safe to point a test at real production data. `POST
/api/transactions` is a different thing entirely: it applies for real when
checks pass. Nothing here may ever call it.

Subjects are derived from live data at runtime rather than hardcoded, so the
suite doesn't rot the first time a player is traded or signed.

    venv/bin/python -m tests.test_validate_endpoints
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import routers.transactions as tx  # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


# The real router, so the real endpoint functions run. The validate endpoints
# are public, so no auth wiring is needed.
app = FastAPI()
app.include_router(tx.router)
client = TestClient(app)

VALIDATE_PATHS = [
    "/api/validate/trade",
    "/api/validate/sign",
    "/api/validate/offer_sheet",
    "/api/validate/offer_sheet_decision",
    "/api/validate/renounce",
    "/api/validate/sign_pick",
    "/api/validate/convert_twoway",
]


def post(path, body):
    return client.post(path, json=body)


def is_result_shape(data) -> bool:
    return (isinstance(data, dict)
            and isinstance(data.get("legal"), bool)
            and isinstance(data.get("checks"), list)
            and isinstance(data.get("fact_sheet"), dict))


# ── subjects, derived from live data ─────────────────────────────────────────

def pick_subjects():
    """(rostered player, their team, a UFA/RFA hold player, their team)."""
    bios = tx.load_player_bios()
    team_map = tx._build_team_map()
    season = tx._current_league_year()

    rostered = hold = None
    for slug, team in sorted(team_map.items()):
        bio = bios.get(slug) or {}
        held = (bio.get("cap_holds") or {}).get(season)
        if held in ("UFA", "RFA") and hold is None:
            hold = (slug, team)
        elif held not in ("UFA", "RFA") and rostered is None:
            rostered = (slug, team)
        if rostered and hold:
            break
    return rostered, hold


def rfa_subject():
    """A player sitting on a current-season RFA hold, plus a different team to
    offer for them — the only shape `/api/validate/offer_sheet` can resolve."""
    bios = tx.load_player_bios()
    team_map = tx._build_team_map()
    season = tx._current_league_year()
    for slug, team in sorted(team_map.items()):
        if ((bios.get(slug) or {}).get("cap_holds") or {}).get(season) == "RFA":
            other = next(t for t in sorted(tx.VALID_TEAMS) if t != team)
            return slug, team, other
    return None


def draft_rights_subject():
    """A player holding unsigned draft rights, plus their pick slot — the only
    shape `/api/validate/sign_pick` can resolve. Returns (slug, team, scale),
    where `scale` is the § 7.1 table entry or None (second-rounder, or a draft
    year with no table loaded)."""
    bios = tx.load_player_bios()
    team_map = tx._build_team_map()
    for slug, bio in sorted(bios.items()):
        if bio.get("type") != "draft-rights" or slug not in team_map:
            continue
        scale = tx._rookie_scale_contract(
            bio.get("draft_year"), bio.get("draft_round"), bio.get("draft_pick"))
        if scale:
            return slug, team_map[slug], scale
    for slug, bio in sorted(bios.items()):
        if bio.get("type") == "draft-rights" and slug in team_map:
            return slug, team_map[slug], None
    return None


def bird_tenure_subject():
    """A rostered player whose § 3.8 tenure resolves to a real tier — the only
    shape that reaches the declared-vs-derived comparison. Every earlier return
    in `_check_bird_rights_declaration` fires when the tier is None, so a
    fixture picked without this check tests the guard vacuously (it did: the
    500 this pins reproduced only against a player with derivable tenure)."""
    bios = tx.load_player_bios()
    team_map = tx._build_team_map()
    season = tx._current_league_year()
    for slug, team in sorted(team_map.items()):
        t = tx._bird_tenure(slug, team, season, bios.get(slug, {}) or {})
        if t.get("tier") is not None:
            return slug, team
    return None


def two_way_subject():
    """A player currently on a two-way contract, plus their team — the only
    shape `/api/validate/convert_twoway` can resolve."""
    bios = tx.load_player_bios()
    team_map = tx._build_team_map()
    for slug, bio in sorted(bios.items()):
        if bio.get("type") == "two-way" and slug in team_map:
            return slug, team_map[slug]
    return None


def main():
    rostered, hold = pick_subjects()
    rfa = rfa_subject()
    rights = draft_rights_subject()
    twoway = two_way_subject()
    bird = bird_tenure_subject()
    print(f"subjects: rostered={rostered} hold={hold} rfa={rfa} bird={bird}")
    print(f"          draft rights={rights[:2] if rights else None} "
          f"scale={'yes' if rights and rights[2] else 'no'} two-way={twoway}")
    if not rostered:
        print("no rostered players found — cannot evaluate")
        return 1

    slug, team = rostered
    other_team = next(t for t in sorted(tx.VALID_TEAMS) if t != team)
    contract = {"type": "player",
                "salaries": {tx._current_league_year(): "$5,000,000"},
                "cap_holds": {}}

    # ── every endpoint answers, none crashes ─────────────────────────────────
    print("\nNo endpoint 5xxes on a well-formed request")
    bodies = {
        "/api/validate/trade": {"transfers": [
            {"from_team": team, "to_team": other_team,
             "assets": [{"type": "player", "slug": slug}]}]},
        "/api/validate/sign": {"player": slug, "team": team, "contract": contract},
        "/api/validate/offer_sheet": {
            "player": rfa[0] if rfa else slug,
            "offering_team": rfa[2] if rfa else other_team,
            "contract": contract},
        # No open offer with this id: a 400 is the designed answer, and the
        # point of the assertion is that it isn't a 500.
        "/api/validate/offer_sheet_decision": {"offer_id": "nope", "outcome": "matched"},
        "/api/validate/renounce": {"player": hold[0] if hold else slug},
        # A pick signing derives its team from the roster, so the body carries
        # no team at all — the one endpoint here whose subject is a single
        # player. Sent at scale terms when there is a scale, so the shape
        # assertions aren't riding on an illegal verdict.
        "/api/validate/sign_pick": {
            "player": rights[0] if rights else slug,
            "contract": ({"type": "player", "salaries": rights[2]["salaries"],
                          "cap_holds": rights[2]["cap_holds"]}
                         if rights and rights[2] else contract)},
        # Same shape as sign_pick: no team in the body, derived from the roster.
        "/api/validate/convert_twoway": {
            "player": twoway[0] if twoway else slug,
            "contract": contract},
    }
    for path in VALIDATE_PATHS:
        r = post(path, bodies[path])
        check(f"{path} -> {r.status_code}", r.status_code < 500)

    # ── the four that should answer with a verdict, do ───────────────────────
    print("\nA verdict comes back in the documented shape")
    for path in [p for p in VALIDATE_PATHS if p != "/api/validate/offer_sheet_decision"]:
        r = post(path, bodies[path])
        ok = r.status_code == 200 and is_result_shape(r.json())
        check(f"{path} returns 200 + {{legal, checks, fact_sheet}}", ok)

    # ── the offer-sheet regression, named ────────────────────────────────────
    print("\nRegression: an offer sheet carries no `outcome` (§ 3.15 split)")
    r = post("/api/validate/offer_sheet", bodies["/api/validate/offer_sheet"])
    check("a body with no `outcome` field is accepted, not a 500", r.status_code == 200)
    if r.status_code == 200:
        fs = r.json().get("fact_sheet", {})
        check("the fact sheet describes the pending state, not a decided one",
              "outcome" not in fs)
        check("and names both sides' roles",
              fs.get("unresolved") is True
              or ("offering_team" in fs and "retaining_team" in fs))
    # An `outcome` sent anyway (the simulator still does) must be ignored, not
    # revive the old code path.
    r2 = post("/api/validate/offer_sheet",
              {**bodies["/api/validate/offer_sheet"], "outcome": "not_matched"})
    check("a stray `outcome` in the body is ignored", r2.status_code == 200)

    # ── unknown subjects are refused, never scored ───────────────────────────
    print("\n_require_validatable refuses unknowns instead of passing vacuously")
    unknown_cases = [
        ("/api/validate/sign", {"player": "nobody-atall", "team": team, "contract": contract}),
        ("/api/validate/sign", {"player": slug, "team": "ZZZ", "contract": contract}),
        ("/api/validate/offer_sheet",
         {"player": "nobody-atall", "offering_team": team, "contract": contract}),
        ("/api/validate/renounce", {"player": "nobody-atall"}),
        ("/api/validate/sign_pick",
         {"player": "nobody-atall", "contract": contract}),
        ("/api/validate/convert_twoway",
         {"player": "nobody-atall", "contract": contract}),
    ]
    for path, body in unknown_cases:
        r = post(path, body)
        who = body.get("player") if body.get("player") == "nobody-atall" else body.get("team")
        check(f"{path} with unknown {who!r} -> 400 (got {r.status_code})",
              r.status_code == 400)

    # Trades needed their own guard: `_require_validatable` takes one team and
    # one player, and validate_trade has many of each, so it had none at all.
    # An unknown team was *scored* — it reported ZZZ's projected salary as
    # within hard-cap limits and its roster as -1 players, both passing. Assert
    # the refusal specifically, not merely that the verdict came back illegal:
    # this trade is also illegal on salary matching, so a weaker assertion
    # passes whether or not the guard exists. (It did, until this test was
    # written.)
    for label, transfers in [
        ("unknown from_team",
         [{"from_team": "ZZZ", "to_team": other_team,
           "assets": [{"type": "player", "slug": slug}]}]),
        ("unknown to_team",
         [{"from_team": team, "to_team": "ZZZ",
           "assets": [{"type": "player", "slug": slug}]}]),
        ("unknown player in an asset",
         [{"from_team": team, "to_team": other_team,
           "assets": [{"type": "player", "slug": "nobody-atall"}]}]),
    ]:
        r = post("/api/validate/trade", {"transfers": transfers})
        check(f"trade with {label} -> 400 (got {r.status_code})", r.status_code == 400)

    # A pick asset names an `orig` team that may be any franchise and carries no
    # slug — the guard must not mistake either for an unknown subject.
    r = post("/api/validate/trade", {"transfers": [
        {"from_team": team, "to_team": other_team,
         "assets": [{"type": "pick", "year": 2030, "round": 1, "orig": team}]}]})
    check(f"a pick-only trade is still evaluated (got {r.status_code})",
          r.status_code == 200 and is_result_shape(r.json()))

    # ── a bad enum-ish value is a verdict, not a traceback ───────────────────
    # `bird_rights_type` is an unconstrained Optional[str], so an unrecognised
    # tier reaches _check_bird_rights_declaration and used to index
    # _BIRD_TIER_RANK directly -> bare KeyError -> 500 on a public endpoint.
    print("\nAn unrecognised bird_rights_type is refused, not a 500")
    bird_slug, bird_team = bird if bird else (slug, team)
    if not bird:
        print("  [skip] no player with derivable § 3.8 tenure — guard untested")
    for bad in ("full_bird", "Full Bird", "qvfa", ""):
        r = post("/api/validate/sign",
                 {"player": bird_slug, "team": bird_team, "contract": contract,
                  "signing_method": "bird_rights", "bird_rights_type": bad})
        ok = r.status_code == 200 and is_result_shape(r.json())
        check(f"bird_rights_type={bad!r} -> {r.status_code} (not 5xx)", ok)
        if ok and bad:
            names = [c["check"] for c in r.json()["checks"] if not c["passed"]]
            check(f"  and {bad!r} is reported as a failed bird_rights_tenure check",
                  "bird_rights_tenure" in names)

    # The real tiers still validate — the guard must not swallow them.
    for good in sorted(tx._BIRD_TIER_RANK):
        r = post("/api/validate/sign",
                 {"player": bird_slug, "team": bird_team, "contract": contract,
                  "signing_method": "bird_rights", "bird_rights_type": good})
        check(f"bird_rights_type={good!r} is still evaluated (got {r.status_code})",
              r.status_code == 200 and is_result_shape(r.json()))

    # ── malformed bodies are 422, not 500 ────────────────────────────────────
    print("\nMalformed bodies are rejected by the model, not by a traceback")
    for path in VALIDATE_PATHS:
        r = post(path, {"nonsense": True})
        check(f"{path} with a junk body -> {r.status_code}", r.status_code == 422)

    print("\n" + ("=" * 40))
    if FAILS:
        print(f"FAILED: {FAILS}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
