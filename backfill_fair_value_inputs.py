#!/usr/bin/env python3
"""One-off, 2026-11-28: record the inputs of every fair value day the hourly
run priced before it started recording them (`fair_value.record_inputs`).

    venv/bin/python backfill_fair_value_inputs.py            # check only, write nothing
    venv/bin/python backfill_fair_value_inputs.py --write    # record every day that passes

Each day is the league as it stood at its end (midnight league time): the data
dir's backup repo (`/var/lib/nbs-backup.git`) at its last snapshot before
then, plus the edit log's roster, dead cap and contract changes between that
snapshot and midnight. That second part matters for 2026-11-02 to 11-14: the
backup took no snapshot from 11-03 03:40 to 11-16 01:52 UTC, and the edit log
has every roster, dead cap and contract write in that window. Ratings are the
OVR log's last entries as the snapshot held them; inside the gap, the log as
the first snapshot after it held it, read by date (see `ovr_in_gap`).

The rule throughout is "as it stood": a correction (a staff tenure added
later, a fixed pick ladder, a repriced contract) counts from the day it was
made, as it did for the live run, not back to the start.

Nothing is written unless every check passes:

  1. the edit log replay reaches the next snapshot exactly (rosters, dead cap,
     contract fields), across the gap and across ordinary days;
  2. every day the live run priced, each team's roster strength, injured
     strength and payroll on the reconstructed league equal the row it wrote
     that day. A difference is printed with its team and day.
"""
import argparse
import csv
import fcntl
import io
import json
import subprocess
import sys
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone

from routers import fair_value as fv
from routers import game_highs as gh
from routers import playoff_odds as po
from routers.cap_history import _payroll
from routers.constants import VALID_TEAMS
from routers.league_time import LEAGUE_TZ, league_today_str
from season_clock import current_season

GIT = ["git", "--git-dir=/var/lib/nbs-backup.git"]
EDITS = fv.DATA_DIR / "edits.jsonl"
TEAMS = sorted(VALID_TEAMS)
GAP = ("2026-11-03T03:40:05+00:00", "2026-11-16T01:52:28+00:00")
# The pick store changed twice inside the gap (the snapshots either side, diffed
# field by field): the sas_tor ladder was corrected (edit log, 11-13 20:06 UTC)
# and Trade 54 conveyed four picks (11-15 23:27 UTC, after the gap's last day).
LADDER_FIX_AT = "2026-11-13T20:06:11+00:00"
TRADE_54_PICKS = (108, 169, 173, 296)
# The 2K sync dates an entry by the server's UTC day, so an evening update
# carries the next league day's date. With no snapshot inside the gap to say
# when an entry landed, it is read by its date, except the 2026-11-07 update
# (171 players): the live row for 11-06 had already priced it, so it landed
# that evening, league time.
LANDED_DAY_BEFORE = {"2026-11-07"}


def ovr_in_gap(history: dict, day: str) -> dict[str, float]:
    """slug -> OVR at the end of `day`, from a log kept after it."""
    out = {}
    for s, h in history.items():
        e = None
        for x in h or []:
            label = x.get("date") or ""
            landed = (date.fromisoformat(label) - timedelta(days=1)).isoformat() if label in LANDED_DAY_BEFORE else label
            if landed <= day:
                e = x
        if e is not None and e.get("ovr") is not None:
            out[s] = float(e["ovr"])
    return out


def games_in(sha: str, season: str, day: str) -> list[dict]:
    """The games whose box scores the snapshot held, played by the end of `day`."""
    seen = set()
    for r in rows_of(show(sha, f"allstats-{season}.csv")):
        if r.get("gametype") == "REG" and r["DATE"] <= day:
            seen.add((r["DATE"], tuple(sorted((r["TEAM"], r["OPP"])))))
    return [{"date": d, "home": a, "away": b} for d, (a, b) in sorted(seen)]


def git(*args: str) -> str:
    return subprocess.run(GIT + list(args), capture_output=True, text=True, check=True).stdout


def show(sha: str, path: str) -> str | None:
    r = subprocess.run(GIT + ["show", f"{sha}:{path}"], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def commits() -> list[tuple[str, datetime]]:
    out = []
    for line in git("log", "--since=2026-10-10", "--format=%H %ct").splitlines():
        sha, ct = line.split()
        out.append((sha, datetime.fromtimestamp(int(ct), timezone.utc)))
    return sorted(out, key=lambda c: c[1])


def end_of(day: str) -> datetime:
    """Midnight league time after `day`, in UTC."""
    d = date.fromisoformat(day) + timedelta(days=1)
    return datetime.combine(d, time(0), LEAGUE_TZ).astimezone(timezone.utc)


def rows_of(text: str | None) -> list[dict]:
    return list(csv.DictReader(io.StringIO(text))) if text else []


def league_at(sha: str) -> dict:
    """Rosters, dead cap and contract fields as the snapshot held them."""
    bios = json.loads(show(sha, "player-bios.json"))
    return {
        "rosters": {t: [(r.get("SLUG") or "").strip() for r in rows_of(show(sha, f"{t.lower()}-roster.csv"))
                        if (r.get("SLUG") or "").strip()] for t in TEAMS},
        "deadcap": {t: rows_of(show(sha, f"{t.lower()}-deadcap.csv")) for t in TEAMS},
        "bios": {s: {k: b[k] for k in fv.BIO_FIELDS if k in b} for s, b in bios.items()},
    }


def load_edits() -> list[dict]:
    out = []
    for line in EDITS.read_text().splitlines():
        if line.strip():
            e = json.loads(line)
            f = e.get("file") or ""
            if f.endswith("-roster.csv") or f.endswith("-deadcap.csv") or f == "player-bios.json":
                out.append(e)
    return out


def _json_value(v):
    if isinstance(v, str) and v[:1] in ("{", "["):
        return json.loads(v)
    return v


def replay(league: dict, edits: list[dict], after: datetime, upto: datetime) -> list[str]:
    """Apply the edit log's changes in (after, upto] to `league`, in place.
    Returns what it could not apply."""
    problems = []
    for e in edits:
        ts = datetime.fromisoformat(e["ts"])
        if not (after < ts <= upto):
            continue
        f = e["file"]
        for d in e.get("diff") or []:
            key, has_from, has_to = d["key"], "from" in d, "to" in d
            if f.endswith("-roster.csv"):
                roster = league["rosters"][f.split("-")[0].upper()]
                if has_to and not has_from and key not in roster:
                    roster.append(key)
                elif has_from and not has_to and key in roster:
                    roster.remove(key)
                elif has_from and has_to:
                    pass
                else:
                    problems.append(f"{e['ts']} {f} {d}")
            elif f.endswith("-deadcap.csv"):
                rows = league["deadcap"][f.split("-")[0].upper()]
                slug, _, col = key.partition(".")
                mine = [r for r in rows if r.get("SLUG") == slug]
                if not col and has_to and not has_from:
                    rows.append(json.loads(d["to"]))
                elif not col and has_from and not has_to and len(mine) == 1:
                    rows.remove(mine[0])
                elif col and len(mine) == 1:
                    if has_to:
                        mine[0][col] = d["to"] if d["to"] is not None else ""
                    else:
                        mine[0].pop(col, None)
                else:
                    problems.append(f"{e['ts']} {f} {d}")
            else:
                slug, _, rest = key.partition(".")
                field, _, sub = rest.partition(".")
                if field not in fv.BIO_FIELDS:
                    continue
                bio = league["bios"].setdefault(slug, {})
                if sub:
                    part = bio.setdefault(field, {})
                    if has_to and d["to"] is not None:
                        part[sub] = d["to"]
                    else:
                        part.pop(sub, None)
                elif has_to:
                    bio[field] = _json_value(d["to"])
                else:
                    bio.pop(field, None)
    return problems


def same_league(a: dict, b: dict) -> list[str]:
    """Where two leagues differ, in what the model reads."""
    out = []
    for t in TEAMS:
        if a["rosters"][t] != b["rosters"][t]:
            out.append(f"{t} roster: {a['rosters'][t]} vs {b['rosters'][t]}")
        if a["deadcap"][t] != b["deadcap"][t]:
            out.append(f"{t} dead cap differs")
    for s in sorted(set(a["bios"]) | set(b["bios"])):
        x, y = a["bios"].get(s, {}), b["bios"].get(s, {})
        for k in fv.BIO_FIELDS:
            if k != "name" and (x.get(k) or None) != (y.get(k) or None):
                out.append(f"{s}.{k}: {x.get(k)} vs {y.get(k)}")
    return out


def picks_for(day_end: datetime, sha: str, pre: str, post: str) -> dict:
    """The pick store at the end of a day: the snapshot's, except inside the gap."""
    if not (datetime.fromisoformat(GAP[0]) <= day_end < datetime.fromisoformat(GAP[1])):
        return json.loads(show(sha, "draft-conveyance.json"))
    before = json.loads(show(pre, "draft-conveyance.json"))
    if day_end < datetime.fromisoformat(LADDER_FIX_AT):
        return before
    fixed = deepcopy(json.loads(show(post, "draft-conveyance.json")))
    for i in TRADE_54_PICKS:
        fixed["picks"][i]["conveyance"]["team"] = before["picks"][i]["conveyance"]["team"]
    return fixed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()
    season = current_season()
    cs = commits()
    edits = load_edits()
    pre = max((c for c in cs if c[1] <= datetime.fromisoformat(GAP[0])), key=lambda c: c[1])[0]
    post = min((c for c in cs if c[1] >= datetime.fromisoformat(GAP[1])), key=lambda c: c[1])[0]

    def snapshot_before(when: datetime) -> tuple[str, datetime]:
        return max((c for c in cs if c[1] <= when), key=lambda c: c[1])

    # Check 1: the replay reaches the next snapshot, across the gap and on ordinary days.
    failures = []
    windows = [(pre, post)]
    for a, b in (("2026-10-28T12:00:00+00:00", "2026-10-31T12:00:00+00:00"),
                 ("2026-11-16T03:00:00+00:00", "2026-11-23T03:00:00+00:00"),
                 ("2026-11-23T03:00:00+00:00", "2026-11-27T03:00:00+00:00")):
        windows.append((snapshot_before(datetime.fromisoformat(a))[0], snapshot_before(datetime.fromisoformat(b))[0]))
    when = dict(cs)
    for a, b in windows:
        league = league_at(a)
        problems = replay(league, edits, when[a], when[b])
        diff = same_league(league, league_at(b))
        print(f"replay {when[a]:%m-%d %H:%M} -> {when[b]:%m-%d %H:%M}: "
              f"{'exact' if not diff and not problems else f'{len(diff)} differences, {len(problems)} unapplied'}")
        for x in (problems + diff)[:15]:
            print("   ", x)
        if diff or problems:
            failures.append(f"replay {a[:8]}..{b[:8]}")

    # Every day from opening night to today, rebuilt and checked against the live row.
    index, _ = gh.get_index()
    results = po.season_results(index, season)
    opening = min(g["date"] for g in po.load_schedule(season) if g.get("date"))
    today = league_today_str()
    records, checked, day = {}, 0, date.fromisoformat(opening)
    while day.isoformat() <= today:
        d = day.isoformat()
        day += timedelta(days=1)
        end = min(end_of(d), datetime.now(timezone.utc))
        sha, at = snapshot_before(end)
        league = league_at(sha)
        unapplied = replay(league, edits, at, end)
        if unapplied:
            failures.append(f"{d}: {len(unapplied)} edits unapplied")
        in_gap = datetime.fromisoformat(GAP[0]) <= end < datetime.fromisoformat(GAP[1])
        later = post if in_gap else sha
        members = json.loads(show(sha, "members.json"))
        # Injuries are dated, not recorded; the check below reads the list as
        # the snapshot held it, to compare with what the live run saw. The log
        # began 2026-10-23, before the first day the live run priced.
        log = show(later, "injury-history.jsonl") or show(post, "injury-history.jsonl")
        injury_log = [json.loads(x) for x in log.splitlines() if x.strip()]
        ovr_history = json.loads(show(later, "ovr-history.json"))
        ovr = ovr_in_gap(ovr_history, d) if in_gap else fv.ovr_now(ovr_history)
        schedule = [g for g in json.loads(show(sha, "schedule-26-27.json")).get("games", [])
                    if g.get("home_team") and g.get("away_team")]
        inputs = fv.gather_inputs(
            league["rosters"], league["deadcap"], league["bios"], ovr, picks_for(end, sha, pre, post),
            json.loads(show(sha, "cap-levels.json")), schedule,
            fv.playoff_edges(season, d, TEAMS, members=members))
        records[d] = inputs

        # Check 2: what the live run saw that day.
        hist = show(post if in_gap else sha, "fair-value-history.jsonl") or ""
        row = next((r for r in map(json.loads, hist.splitlines()) if r["season"] == season and r["date"] == d
                    and not r.get("backfill")), None)
        if row is None:
            print(f"{d}  {sha[:7]}{' +edit log' if in_gap else ''}  no live row to check against")
            continue
        # The live row saw only the box scores and injury reports in by its last
        # run; it is compared with those (the day's snapshot). Inside the gap
        # there is no snapshot to say, so a difference that the day before's
        # games and reports explain is a report or box score that came late.
        if in_gap:
            then = (fv.unplayed_games(inputs["schedule"], [g for g in results if g["date"] <= d]),
                    fv.injuries_as_of(d, injury_log))
            prev = (date.fromisoformat(d) - timedelta(days=1)).isoformat()
            late_then = (fv.unplayed_games(inputs["schedule"], [g for g in results if g["date"] <= prev]),
                         fv.injuries_as_of(prev, injury_log))
        else:
            then = (fv.unplayed_games(inputs["schedule"], games_in(sha, season, d)), fv.injuries_as_of(d, injury_log))
            late_then = None
        late = []
        bad = []
        for t in TEAMS:
            rostered = {s: inputs["ovr"][s] for s in inputs["rosters"][t] if s in inputs["ovr"]
                        and (inputs["bios"].get(s) or {}).get("type") not in ("draft-rights", "dead")}
            mine = {"strength": round(fv._strength(list(rostered.values())), 2),
                    "strength_injured": round(fv.injured_strength(t, rostered, then[1], then[0])[0], 2),
                    "payroll": _payroll(t, inputs["bios"], season, inputs["rosters"][t], inputs["deadcap"][t])[0]}
            was = row["teams"][t]
            if late_then and was.get("strength_injured") != mine["strength_injured"] and \
                    was.get("strength_injured") == round(fv.injured_strength(t, rostered, late_then[1], late_then[0])[0], 2):
                late.append(f"{t} {was['strength_injured']} -> {mine['strength_injured']}")
                mine["strength_injured"] = was["strength_injured"]
            for k, v in mine.items():
                if k in was and was[k] != v:
                    bad.append(f"{t} {k} {was[k]} -> {v}")
        checked += 1
        print(f"{d}  {sha[:7]}{' +edit log' if in_gap else ''}  live row {row['model']}: "
              f"{'all 30 teams match' if not bad else f'{len(bad)} differences'}"
              + (f" (a report or box score dated that day came after its last run: {', '.join(late)})" if late else ""))
        for x in bad[:12]:
            print("   ", x)
        if bad:
            failures.append(f"{d}: {len(bad)} differences")

    print(f"\n{len(records)} days reconstructed, {checked} checked against a live row")
    if failures:
        print("NOT WRITTEN. Failures:\n  " + "\n  ".join(failures))
        return 1
    if args.write:
        # The hourly run records today's inputs in the same manifest.
        lock = open(fv.DATA_DIR / ".playoff-odds.lock", "w")
        fcntl.flock(lock, fcntl.LOCK_EX)
        for d, inputs in records.items():
            fv.record_inputs(season, d, inputs)
        print(f"recorded {len(records)} days in {fv.INPUTS_DIR}")
    else:
        print("all checks passed; nothing written (pass --write)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
