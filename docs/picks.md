# Draft picks: corrections and protections

Correcting a pick's structure, and trading a protection away or lowering it. How to read a pick is in nbn-today's CLAUDE.md § Draft pick. Moved out of CLAUDE.md on 2026-11-16, word for word; CLAUDE.md keeps a one-line pointer per section.

## Correcting a pick's structure (`routers/picks_structure.py`)

A pick's bands and swaps live in `draft-conveyance-registry.json`, which
overrides the flat `draft-picks.csv` for any structured pick. Trades add to it
(`picks_conveyance.from_trade`); corrections go through one checked path,
`picks_conveyance/restructure.py`, used by both the rosters committee
(`POST /api/picks/structure/preview` / `apply`, `rosters` role, the "Set bands"
editor on `/committees/rosters` → Draft Picks) and the CLI
(`restructure_picks.py pick_rulings/<file>.json [--apply]`). Never hand-edit
the registry, and don't reach for `PUT /api/picks` for this: it writes only
the flat CSV.

Apply refuses a slot no band covers, a draft order that leaves a team without
a pick, a worked example that doesn't hold, a new parity finding, or a
registry that changed since the preview. Each applied change goes to
`edits.jsonl` and, with its reason and spec, to `pick-rulings.jsonl`.

A swap whose worse pick is protected (the 2029 MIA/MIN 1sts) is a swap group
whose priority slot is a `protected` node on `"__slot1__"`; since 2026-10-29
the validator accepts it and the owner field lists the slot's teams.

The registry was seeded once from `curated.py` on 2026-07-19, from the sheet.
Two of its entries were wrong (2027 LAC 2nd, 2029 DET 1st): the seed predated
or misread a trade the flat CSV had right. A band no trade ever gives a team
is the sign of that; the Picks tab's per-pick history shows it.

## Trading a protection away, or lowering it

A protected pick is bands, one per team. Removing the protection by trade is
the protecting team sending its band (a pick asset with that `leaf_id`) to the
team holding the rest; lowering it is the same with a smaller `protection`,
which splits the band (`registry.subdivide_protected_band`). After either,
`registry.normalize_protected` merges a team's neighbouring bands and drops the
entry once one team holds them all, so the pick is plainly owned again rather
than two claims the next trade would refuse as ambiguous; the flat `PROTECTED`
is cleared with it. `_pick_protection_problems` refuses a threshold outside the
sender's band (otherwise the registration fails open after the trade is
applied). `TradeAsset.claim` ("protection removed") is server-set by
`_describe_pick_claims` and read by every display. `GET /api/picks/{team}?claims=all`
lists picks where a team holds any band; the plain call doesn't, because `owner`
names only the unprotected recipient. Tests: `tests/test_pick_protection_trades.py`.

A **ladder** protection (keeper `from` keeps the pick if top N, else it goes to
`to`; if protected, `to` gets the fallback picks) isn't bands and isn't a leaf,
so the keeper names it with a plain pick asset sent to the ladder's `to`
(`_ladder_keeper`). Removing deletes the ladder, fallback included; lowering
is `registry.lower_ladder`. A re-trade of a ladder pick moves whichever side
sends it: until 2026-11-13 only `from` moved, which left `sas_tor` owed to TOR
after TOR traded the pick to NOP in Trade 44.
