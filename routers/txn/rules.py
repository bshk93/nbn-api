"""Contract and cap rules: scales, minimums and maximums, two-way terms, qualifying offers, FA holds, raises, exceptions, hard caps, extension eligibility.

Moved out of routers/transactions.py; see routers/txn/__init__.py."""
import re
from .. import season_calendar, years_of_service
from ..constants import ROSTER_CHARGE_MIN, ROSTER_MAX, ROSTER_MIN, ROSTER_OFFSEASON_MAX, SALARY_MATCH_TIER1_CAP, SALARY_MATCH_TIER2_CAP, TWO_WAY_MAX, VALID_TEAMS
from ..league_time import LEAGUE_TZ
from ..players import _display_name
from ..roster_picks import _bae_available, get_season_state
from ..storage import _parse_dollar, _season_shift, _season_start
from datetime import datetime, timedelta
from fastapi import HTTPException
from season_clock import season_shift, season_starting
from types import SimpleNamespace
from typing import Optional
from .models import CheckResult, ContractIn, SignDetails


def _option_fa_tag(bio: dict, hold_season: str) -> tuple[Optional[str], str]:
    """The hold a declined option leaves. A declined rookie-scale option (the
    3rd or 4th season of a first-round deal) is always UFA (§ 6.1, as in the
    NBA CBA): only a pick who plays out all four years gets a QO. Otherwise
    § 3.1: RFA (RFA-*eligible*: the QO still decides) for a player under 4
    years of experience entering `hold_season`, or finishing a first-round
    rookie-scale deal; UFA for anyone else. Returns (tag, why); tag is None
    when his experience isn't on file, which § 3.1 leaves to be judged by
    hand. Read before the decline is applied, while the year is still tagged
    TEAM_OPT."""
    if ((bio.get("cap_holds") or {}).get(hold_season) == "TEAM_OPT"
            and season_calendar.is_rookie_scale_option(bio, hold_season)):
        return "UFA", "it is a rookie-scale option, and declining one leaves him unrestricted (§ 6.1)"
    if _finishes_rookie_scale(bio, hold_season):
        return "RFA", "he is finishing his first-round rookie-scale deal"
    exp = _qo_years_exp(bio, hold_season)
    if exp is None:
        return None, "his NBA experience isn't on file"
    yrs = f"he will have {exp} year{'s' if exp != 1 else ''} of experience entering {hold_season}"
    return ("RFA", yrs) if exp < 4 else ("UFA", yrs)


def _eaps_unset(hold_season: str, cap_levels: dict) -> bool:
    return not (cap_levels.get(hold_season, {}) or {}).get("eaps")


def _dead_cap_from_schedule(schedule: list, salary: str, txn_date: str) -> Optional[str]:
    """Compute dead cap for a NON_GTD year given a multi-step guarantee schedule."""
    cumulative = 0.0
    fully_gtd = False
    for step in schedule:
        step_date = step.get("date") or ""
        if step_date and txn_date < step_date:
            continue
        step_amount = step.get("amount")
        if not step_amount:
            fully_gtd = True
            break
        cumulative += _parse_dollar(step_amount)

    if fully_gtd:
        return salary
    if cumulative > 0:
        return f"${round(cumulative):,}"
    return None


def _retained_history(bio: dict, cur_season: str):
    """Entries to keep when a new contract is applied to a player: every season
    already played (<= cur_season) plus any future season still guaranteed from a
    prior deal. Earnings history is never silently dropped — only unplayed,
    non-guaranteed future years are released. Returns
    (salaries, guaranteed, guarantee_dates, guarantee_schedule)."""
    prior_gtd = bio.get("guaranteed", {})
    keep = lambda s: s <= cur_season or s in prior_gtd
    return (
        {k: v for k, v in bio.get("salaries", {}).items() if keep(k)},
        {k: v for k, v in bio.get("guaranteed", {}).items() if keep(k)},
        {k: v for k, v in bio.get("guarantee_dates", {}).items() if keep(k)},
        {k: v for k, v in bio.get("guarantee_schedule", {}).items() if keep(k)},
    )


def _season_after(season: str) -> str:
    """'25-26' -> '26-27'."""
    return season_shift(season, 1)


# Roster-count-exempt bio types: two-way (own cap slot rules), draft-rights (no
# contract yet — just held rights, not a roster spot), dead (a cap hit with no
# active player). Only "player" (and unset "") occupy a standard roster spot.
_ROSTER_EXEMPT_TYPES = {"two-way", "draft-rights", "dead"}


def _is_standard_roster_slot(bio_type: str) -> bool:
    return bio_type not in _ROSTER_EXEMPT_TYPES


def _check_signing_method_declared(
    team: str, method: Optional[str], contract_type: str
) -> "CheckResult | None":
    """§ 3.1–§ 3.6 must be funded by *something*, and a blank method was silently
    exempt from proving it.

    `_check_signing_method_funding` returns early for any method outside the
    cap-space/MLE family — and `None` is outside it — so declaring nothing
    skipped funding validation altogether rather than failing it. Nine live FA
    offers reached the committee that way (2026-08-10), five of them for real
    money from teams over the Cap.

    Two-ways are exempt for real, not by omission: they carry $0 and sit outside
    Team Salary (§ 2.2), so there is no exception for them to be funded from.
    """
    if contract_type == "two-way" or method:
        return None
    return CheckResult(
        check=f"signing_method_declared_{team.lower()}", passed=False, level="error",
        message=(f"{team} must declare how this signing is funded — cap space, Bird "
                 f"Rights, an exception, or the minimum (§ 3.1–§ 3.6). Without a method "
                 f"nothing checks that the money is actually available."),
    )


def _two_way_slot_check(team: str, after: int, verb: str) -> "CheckResult | None":
    """§ 2.2: at most TWO_WAY_MAX two-way players, in G-League slots outside the
    standard limit.

    A hard error with no warning band, unlike `_roster_size_check`'s 16–20 — the
    offseason ceiling in § 2.1 is written into the rules, and § 2.2 grants no
    equivalent, so a fourth two-way is not a "trim it later" situation.

    Nothing enforced this before: `_validate_sign` skips the standard roster
    check for a two-way contract (correctly — they sit outside the 15), and every
    other check no-ops on the $0 salary a two-way carries, so a two-way signing
    was reaching `_apply_sign` having passed *zero* checks. The verdict was not
    "legal", it was unexamined.
    """
    if after > TWO_WAY_MAX:
        return CheckResult(
            check="two_way_slots", passed=False, level="error",
            message=(f"{team} would carry {after} two-way players, over the "
                     f"{TWO_WAY_MAX}-slot limit (§ 2.2) — convert or release a two-way "
                     f"before {verb}."),
        )
    return None


TWO_WAY_MAX_YEARS = 2         # § 2.2: maximum length, and maximum consecutive years with one team


TWO_WAY_MAX_EXPERIENCE = 3    # § 2.2: "3 or fewer years of NBA experience"


def _is_two_way_contract_entry(entry: dict) -> bool:
    """Whether a `contracts` history entry was a two-way deal. The entries don't
    record a type, but § 2.2 fixes a two-way at $0, and no standard deal is $0 in
    every year — so every real (non-hold) year being $0 identifies one."""
    holds = entry.get("cap_holds") or {}
    years = [y for y in (entry.get("salaries") or {}) if holds.get(y) not in _FA_HOLD_TYPES]
    return bool(years) and all(_parse_dollar(entry["salaries"][y]) == 0 for y in years)


def _check_two_way_terms(contract: "ContractIn", player: str, team: Optional[str],
                         bio: dict, season: str) -> list[CheckResult]:
    """§ 2.2's contract terms for a two-way, on both the `sign` and `sign_pick`
    paths. `two_way_slots` was the only § 2.2 check until 2026-09-25, and
    passing checks return nothing, so a two-way validated to an empty list: a
    5-year two-way for a veteran at $3M would have gone straight through.

    Salary and length are exact and are errors. Experience is **not** enforced:
    the only proxy on file is the draft-year one § 3.12 prices minimums with,
    and years since the draft is not NBA service. 10 of the 71 two-ways on the
    books when this was written read over 3 on it (Duop Reath reads 8) — real
    two-way deals the league mirrors, for players who spent those years overseas
    or in the G League. So it reports, and a human decides (§ 2.2 is 👁 for it).
    """
    checks = []
    holds = contract.cap_holds or {}
    years = sorted((y for y in (contract.salaries or {}) if holds.get(y) not in _FA_HOLD_TYPES),
                   key=_season_start)

    paid = [f"{y}: {contract.salaries[y]}" for y in years if _parse_dollar(contract.salaries[y]) != 0]
    if paid:
        checks.append(CheckResult(
            check="two_way_salary", passed=False, level="error",
            message=(f"A two-way contract carries $0 salary (§ 2.2) — this one pays "
                     f"{', '.join(paid)}. Sign a standard contract instead."),
        ))

    if len(years) > TWO_WAY_MAX_YEARS:
        checks.append(CheckResult(
            check="two_way_length", passed=False, level="error",
            message=(f"A two-way contract runs at most {TWO_WAY_MAX_YEARS} years (§ 2.2) — "
                     f"this one runs {len(years)} ({years[0]} to {years[-1]})."),
        ))

    name = bio.get("name") or player
    declared = getattr(contract, "years_experience", None)
    if declared is not None:
        exp, basis = int(declared), "declared on the contract"
    else:
        exp = years_of_service.years_of_service(bio, season, player)
        basis = "NBN years of service" if exp is not None else None
    if exp is None:
        msg = (f"{name}'s experience isn't on file — confirm by hand that it's "
               f"{TWO_WAY_MAX_EXPERIENCE} years or fewer (§ 2.2).")
    elif exp > TWO_WAY_MAX_EXPERIENCE:
        msg = (f"{name} reads {exp} years of experience ({basis}), over § 2.2's "
               f"{TWO_WAY_MAX_EXPERIENCE}. Confirm by hand: a season he was rostered "
               f"without playing may be missing, or one may be counted that shouldn't be.")
    else:
        msg = f"{name} reads {exp} years of experience ({basis}), within § 2.2's {TWO_WAY_MAX_EXPERIENCE}."
    checks.append(CheckResult(check="two_way_experience", passed=True, level="info", message=msg))

    # § 2.2: "the same player in a Two-Way spot for up to 2 consecutive years".
    # Built from `contracts`, which only exists for signings since the write
    # path began recording them, so a hit is real but a miss proves nothing —
    # and a standard deal in between could be missing too. Forceable.
    if team and years:
        prior = set()
        for entry in bio.get("contracts") or []:
            if (entry.get("team") or "").upper() == team.upper() and _is_two_way_contract_entry(entry):
                eh = entry.get("cap_holds") or {}
                prior |= {_season_start(y) for y in entry["salaries"] if eh.get(y) not in _FA_HOLD_TYPES}
        run, start = 0, _season_start(years[0]) - 1
        while start - run in prior:
            run += 1
        # run == 0 is a deal that's merely too long, which two_way_length already says.
        if run and run + len(years) > TWO_WAY_MAX_YEARS:
            checks.append(CheckResult(
                check="two_way_consecutive", passed=False, level="warning",
                message=(f"{name} has already spent {run} straight year(s) in a {team} two-way "
                         f"spot; this deal would make {run + len(years)}, over § 2.2's "
                         f"{TWO_WAY_MAX_YEARS} consecutive. Convert them to a standard "
                         f"contract, or force if the history is wrong."),
            ))
    return checks


def _roster_size_check(team: str, after: int, verb: str,
                       txn_date: Optional[str] = None) -> "CheckResult | None":
    """§ 2.1 roster-size ceiling, in the same shape the trade validator uses
    (`_validate_trade`'s "Roster size" block).

    During the regular season (opening night through the last regular-season
    game, `season_calendar.is_regular_season`) anything over 15 is a hard
    block. The rest of the year ≤15 passes, 16-20 is a warning (legal, but
    flagged to trim before the season), and only >20 is a hard block.
    Signing-side validators used to hard-block at 15 unconditionally, which
    meant a team at exactly 15 couldn't even make an offseason FA offer; then,
    until 2026-09-30, they never blocked at 15 at all, because nothing knew
    when the season started. `txn_date` is what tells them.
    """
    if txn_date and season_calendar.is_regular_season(txn_date) and after > ROSTER_MAX:
        return CheckResult(
            check="roster_size", passed=False, level="error",
            message=(f"{team} would carry {after} standard players, over the {ROSTER_MAX}-man "
                     f"regular-season limit (§ 2.1) — release a player before {verb}."),
        )
    if after > ROSTER_OFFSEASON_MAX:
        return CheckResult(
            check="roster_size", passed=False, level="error",
            message=(f"{team} would carry {after} standard players, over the "
                     f"{ROSTER_OFFSEASON_MAX}-player offseason maximum — release a player before {verb}."),
        )
    if after > ROSTER_MAX:
        return CheckResult(
            check="roster_size", passed=False, level="warning",
            message=(f"{team} would carry {after} standard players — over the {ROSTER_MAX}-man "
                     f"regular-season limit (offseason ceiling {ROSTER_OFFSEASON_MAX}); trim to "
                     f"{ROSTER_MAX} before the season."),
        )
    return None


def _rookie_min_salary(season: str, cap_levels: dict) -> int:
    """The 0-years-experience tier of that season's minimum salary scale
    (§ 1.7) — the figure an Empty Roster Charge (§ 2.1a) uses per open slot."""
    return (cap_levels.get(season, {}).get("min_salary_scale") or {}).get("0", 0)


# § 7.1 second-round minimum-scale structures: the tier (years-of-experience
# key into that season's min_salary_scale) for each year of the deal.
#
# Each deal starts on a fixed tier and **climbs one tier per contract year**,
# priced on each season's own scale -- the same rule § 3.12 states for every
# multi-year minimum. The 3-year (2+1) deal starts at tier 1, the 4-year (3+1)
# at tier 2. This is the league office's own published table (the "2026
# Rookie Contracts" tab of the league sheet):
#   2+1: $2,185,116 / $2,571,895 / $2,791,275            (tiers 1, 2, 3)
#   3+1: $2,449,421 / $2,664,401 / $2,888,193 / $3,272,766 (tiers 2, 3, 4, 5)
# and the commissioner's rulings in #fa-news (2026-07-17, 2026-08-28): "3+1 ...
# starts at the 2yr vet min", "2+1 ... starts at the 1yr vet min".
#
# Until 2026-09-24 this table was flat ([1, 1, 1] and [2, 1, 1, 1]), on a
# reading of Otega Oweh's deal that took $2,571,895 for a tier-1 figure. It is
# the tier-2 figure for 27-28 ($2,294,372 is tier 1), so Oweh's deal climbs.
_SECOND_ROUND_SCALE_TIERS = {
    3: [1, 2, 3],
    4: [2, 3, 4, 5],
}


# 1-based year index -> cap_holds tag. Year 1 (and, on the 4-year deal, Year 2)
# carry no tag at all — they're plain guaranteed money.
_SECOND_ROUND_SCALE_TAGS = {
    3: {2: "NON_GTD", 3: "TEAM_OPT"},
    4: {3: "NON_GTD", 4: "TEAM_OPT"},
}


def _second_round_scale_contract(contract: "ContractIn", cap_levels: dict) -> Optional[dict]:
    """§ 7.1's prescribed second-round minimum-scale deal for the seasons the
    contract actually declares, or None when it isn't a recognizable 3- or
    4-year second-round shape.

    Anchored on the contract's own earliest declared season rather than the
    player's draft year, so a pick signed after their draft season (a
    holdover unsigned draft-rights case) is still scored correctly. Mirrors
    `_rookie_scale_contract`'s one-reader shape: the validator scores a
    submitted contract against this, so the schedule is stated once.

    Deliberately **not** run through `_check_contract_raises` (§ 3.9's 5%
    ladder) — same reasoning as the first-round scale: the steps between
    tiers here are routinely far bigger than 5%, and the 4-year option's
    Year 1 -> Year 2 step isn't even a raise.

    A trailing UFA/RFA season — the § 3.10 hold the deal rolls into once it
    ends — is not a contract year (same convention `contract.js`'s
    `isFaHold` enforces everywhere else). Counting it inflated a 3-year deal
    to 4 salary entries, which picked the wrong tier sequence entirely and
    scored a correctly-priced 3-year deal against the 4-year table.
    """
    fa_hold_seasons = {
        yr for yr, ht in (contract.cap_holds or {}).items() if ht in _FA_HOLD_TYPES
    }
    salary_seasons = sorted(
        (yr for yr in (contract.salaries or {}) if yr not in fa_hold_seasons),
        key=lambda y: (_season_start(y), y),
    )
    num_years = len(salary_seasons)
    tiers = _SECOND_ROUND_SCALE_TIERS.get(num_years)
    if not tiers:
        return None
    first = salary_seasons[0]
    seasons = [first] + [_season_shift(first, i) for i in range(1, num_years)]
    salaries = {}
    for season, tier in zip(seasons, tiers):
        scale = _min_scale_for_season(season, cap_levels)
        amt = scale.get(str(tier))
        if not amt:
            return None  # no scale configured that far out — can't score it
        salaries[season] = f"${amt:,}"
    cap_holds = {
        seasons[idx - 1]: tag
        for idx, tag in _SECOND_ROUND_SCALE_TAGS.get(num_years, {}).items()
    }
    return {"salaries": salaries, "cap_holds": cap_holds, "seasons": seasons}


def _check_second_round_scale_terms(contract: "ContractIn", bio: dict,
                                    cap_levels: dict) -> Optional[CheckResult]:
    """§ 7.1: score a second-round pick's deal against whichever fixed
    structure (3-year or 4-year) matches how many salary years it declares.

    Returns None (nothing to check) rather than a passing result when the
    year count doesn't match either structure — that isn't necessarily an
    off-scale deal, it might be a two-way or a shape this function can't
    recognize, and callers fall back to the generic minimum/ladder checks in
    that case.
    """
    scale = _second_round_scale_contract(contract, cap_levels)
    if not scale:
        return None
    num_years = len(scale["salaries"])
    fa_hold_seasons = {
        yr for yr, ht in (contract.cap_holds or {}).items() if ht in _FA_HOLD_TYPES
    }
    label = f"pick {bio.get('draft_pick')} of {bio.get('draft_year')} (2nd round)"
    problems = []
    for season, expected in scale["salaries"].items():
        got = contract.salaries.get(season)
        if got is None:
            problems.append(f"{season}: missing (scale: {expected})")
        elif _parse_dollar(got) != _parse_dollar(expected):
            problems.append(f"{season}: {got} (scale: {expected})")
    extra = sorted(set(contract.salaries) - set(scale["salaries"]) - fa_hold_seasons)
    for season in extra:
        problems.append(f"{season}: not a year of the § 7.1 second-round scale")

    wrong_tags = []
    for season, want in scale["cap_holds"].items():
        got = (contract.cap_holds or {}).get(season)
        if got != want:
            wrong_tags.append(f"{season} should be {want}, got {got or 'guaranteed'}")

    if not problems and not wrong_tags:
        return CheckResult(
            check="second_round_scale", passed=True, level="info",
            message=f"Terms match the § 7.1 {num_years}-year second-round scale for {label}.",
        )
    detail = "; ".join(problems + wrong_tags)
    return CheckResult(
        check="second_round_scale", passed=False, level="error",
        message=f"§ 7.1 sets the {num_years}-year second-round scale for {label} — {detail}.",
    )


def _min_salary_scale_tier(years_exp: int) -> str:
    """Maps a raw years-of-NBA-experience count to a min_salary_scale key
    ("0".."9","10+")."""
    return "10+" if years_exp >= 10 else str(max(0, years_exp))


def _contract_first_season(contract) -> Optional[str]:
    """The first real season of a contract: its earliest salary year that
    isn't a trailing UFA/RFA hold. None when the contract declares no years."""
    salaries = getattr(contract, "salaries", None) or {}
    holds = getattr(contract, "cap_holds", None) or {}
    years = [yr for yr in salaries if holds.get(yr) not in _FA_HOLD_TYPES]
    return min(years, key=_season_start) if years else None


def _contract_years_exp(contract, season: str) -> Optional[int]:
    """Years of NBA experience for pricing `season` of a contract that declares
    its experience. `ContractIn.years_experience` is the player's experience in
    the contract's **first** season, and the tier climbs one row per contract
    year after that (§ 3.12: "the tier then steps up one row per contract
    year"). A 1-year deal never climbs.

    This was flat from 2026-08-13 to 2026-09-24, on a reading of Jamison
    Battle's deal that took $2,664,401 for his tier-2 figure in 27-28. It is
    the tier-3 figure ($2,571,892 is tier 2), so the league sheet climbs, and
    so does the league office's own table for minimum and second-round deals
    (the "2026 Rookie Contracts" tab). See `_SECOND_ROUND_SCALE_TIERS`.

    Returns None when nothing is declared, which callers must read as "fall
    back to years of service" (`years_of_service`), not
    as zero experience.
    """
    declared = getattr(contract, "years_experience", None) if contract else None
    if declared is None:
        return None
    first = _contract_first_season(contract)
    offset = max(0, _season_start(season) - _season_start(first)) if first else 0
    return max(0, int(declared)) + offset


def _max_salary_pct(years_exp: int) -> float:
    """§ 3.11 tier: max Year 1 salary as a fraction of that season's Salary Cap,
    by years of NBA experience. Unlike the minimum scale, this is a clean
    percentage of the cap rather than its own negotiated table, so it needs
    no separate per-season entry in cap-levels.json."""
    if years_exp >= 10:
        return 0.35
    if years_exp >= 7:
        return 0.30
    return 0.25


def _max_salary(bio: dict, season: str, cap_levels: dict) -> Optional[int]:
    """§ 3.11: the player's maximum Year 1 salary for `season`, derived from
    the season's cap and the player's years of service
    (`years_of_service`). Returns None if experience or the season's cap
    can't be determined (undrafted player, cap not configured yet) — callers
    should treat that as "can't check," not "no cap applies."
    """
    cap = (cap_levels.get(season, {}) or {}).get("cap")
    years_exp = years_of_service.years_of_service(bio, season) if cap else None
    if years_exp is None:
        return None
    return int(cap * _max_salary_pct(years_exp))


_BIRD_HOLD_PCT = {"EQVFA": 1.3, "Non-QVFA": 1.2}


def _rfa_eligibility(player: str, bios: dict, cur_season: str,
                     as_of: Optional[str] = None) -> tuple[bool, str]:
    """Whether `player` is a restricted free agent who can receive an offer sheet
    (§ 3.15), as ``(ok, reason)``.

    One rule, shared by `_apply_offer_sheet` and `_validate_offer_sheet`, which
    used to test different things: the apply path asked whether the player's
    *earliest* cap hold was an RFA no later than the upcoming season, while the
    validator asked about `cap_holds[current_season]`.

    The current-season test is the correct one, because an offer sheet is only
    worth extending if it can actually be executed — and `_apply_sign` refuses a
    cross-team signing unless the player carries a UFA/RFA hold **for the current
    season** (anything else means they're still under contract). An "earliest
    hold" reading accepts a player whose deal runs through this season and whose
    RFA year is still ahead of them; the offer would validate, sit on the books
    holding real cap room, and then fail at the decision with "already on ATL".
    """
    holds = ((bios.get(player) or {}).get("cap_holds") or {})
    if not holds:
        return False, "no cap hold on file"
    current = holds.get(cur_season)
    if current == "RFA":
        # The tag only makes him RFA-*eligible*; the qualifying offer is what
        # makes him an RFA (§ 3.1). `as_of` is today's league year when the
        # caller is asking about a later class (the FA pool), where an
        # undecided QO is still pending rather than lapsed.
        status = _qo_status(bios[player], cur_season, as_of or cur_season)
        if status in ("extended", "pending"):
            return True, ""
        if status == "withdrawn":
            return False, f"{cur_season} qualifying offer was withdrawn — he is a UFA (§ 3.1)"
        if status == "accepted":
            return False, f"he accepted his {cur_season} qualifying offer"
        return False, (f"no qualifying offer was extended for {cur_season} by the July 1 "
                       f"deadline — he is a UFA (§ 3.1)")
    if current:
        return False, f"cap hold for {cur_season} is {current}, not RFA"
    upcoming = sorted(y for y, t in holds.items() if t == "RFA" and y > cur_season)
    if upcoming:
        return False, (f"still under contract for {cur_season}; becomes an RFA in "
                       f"{upcoming[0]}, so an offer sheet has to wait for that league year")
    return False, f"no cap hold for {cur_season}"


# NBA rookie-scale QO: the raise over the 4th-year salary, by draft slot (2017
# CBA Exhibit B). The 2023 CBA added 10 points from the 2023 draft class on.
_ROOKIE_QO_RAISE = (
    0.300, 0.305, 0.312, 0.319, 0.326, 0.334, 0.341, 0.348, 0.355, 0.362,
    0.369, 0.376, 0.383, 0.391, 0.398, 0.405, 0.412, 0.419, 0.426, 0.433,
    0.441, 0.448, 0.455, 0.462, 0.469, 0.476, 0.483, 0.490, 0.500, 0.500,
)


_ROOKIE_QO_BUMP_FROM_CLASS = 2023


_ROOKIE_QO_BUMP = 0.10


def _finishes_rookie_scale(bio: dict, hold_season: str) -> bool:
    """§ 3.1's carve-out: a first-round pick whose four-year rookie-scale deal
    (§ 7.1) ends right before `hold_season`. He is RFA-eligible even though he
    has four years of experience by then."""
    dy, pick = bio.get("draft_year"), bio.get("draft_pick")
    if bio.get("draft_round") != 1 or not dy or not pick or not 1 <= int(pick) <= 30:
        return False
    return _season_start(hold_season) + 2000 == int(dy) + 4


def _last_contract_ns(bio: dict):
    """The contract a hold follows, shaped for `_contract_years_exp`, or None
    when it declared no experience (callers fall back to the draft year)."""
    last = (bio.get("contracts") or [None])[-1] or {}
    if last.get("years_experience") is None:
        return None
    return SimpleNamespace(years_experience=last["years_experience"],
                           salaries=last.get("salaries") or {},
                           cap_holds=last.get("cap_holds") or {})


def _qo_years_exp(bio: dict, hold_season: str) -> Optional[int]:
    """Years of NBA experience entering `hold_season` (§ 3.1): the experience
    declared on the contract, climbing a row a year, else years of service
    (`years_of_service`). None when neither exists — an undrafted player is
    judged by hand."""
    declared = _contract_years_exp(_last_contract_ns(bio), hold_season)
    if declared is not None:
        return declared
    return years_of_service.years_of_service(bio, hold_season)


def _qo_amount(bio: dict, hold_season: str, cap_levels: dict) -> Optional[int]:
    """§ 3.1 qualifying offer amount, or None when it can't be priced.

    - Two-way player: a one-year two-way contract, so no cap figure (0).
    - First-round pick finishing his rookie scale: the 4th-year salary plus the
      slot's raise (`_ROOKIE_QO_RAISE`).
    - Everyone else: the greater of the player's minimum (§ 3.12) and 125% of
      his prior salary.

    The prior salary is the season before the hold — never the hold season's
    own `salaries` entry, which *is* the hold (that mistake priced Jaden
    Hardy's QO off his $12.3M hold instead of his $4.1M salary)."""
    if bio.get("type") == "two-way":
        return 0
    prior = _parse_dollar((bio.get("salaries") or {}).get(_season_shift(hold_season, -1)) or "")
    if _finishes_rookie_scale(bio, hold_season):
        if not prior:
            return None
        pct = _ROOKIE_QO_RAISE[int(bio["draft_pick"]) - 1]
        if int(bio["draft_year"]) >= _ROOKIE_QO_BUMP_FROM_CLASS:
            pct += _ROOKIE_QO_BUMP
        return round(prior * (1 + pct))
    min_amt = _min_salary_for(bio, hold_season, cap_levels, _last_contract_ns(bio))
    raise_amt = round(prior * 1.25) if prior else None
    candidates = [v for v in (min_amt, raise_amt) if v]
    return max(candidates) if candidates else None


def _qo_record(bio: dict, season: str) -> dict:
    return (bio.get("qualifying_offers") or {}).get(season) or {}


def _qo_status(bio: dict, season: str, as_of: str) -> Optional[str]:
    """The QO decision for `season`'s hold, as of league year `as_of`:
    a recorded status, or "pending" (RFA-tagged, deadline still ahead), or
    "lapsed" (RFA-tagged, deadline passed, nothing extended). None when the
    hold isn't RFA-eligible at all."""
    status = _qo_record(bio, season).get("status")
    if status:
        return status
    if (bio.get("cap_holds") or {}).get(season) != "RFA":
        return None
    return "pending" if _season_start(season) > _season_start(as_of) else "lapsed"


def _qo_target_season(bio: dict, action: str) -> Optional[str]:
    """The hold season a QO action is about: the earliest UFA/RFA hold for an
    extension, the extended QO for a withdrawal or acceptance."""
    if action == "extend":
        fa = sorted((y for y, t in (bio.get("cap_holds") or {}).items() if t in _FA_HOLD_TYPES),
                    key=_season_start)
        return fa[0] if fa else None
    live = sorted((s for s, r in (bio.get("qualifying_offers") or {}).items()
                   if r.get("status") == "extended"), key=_season_start)
    return live[0] if live else None


def _close_end(closes: str) -> datetime:
    """The moment a window closes: the end of its last day, league time."""
    return datetime.strptime(closes, "%Y-%m-%d").replace(tzinfo=LEAGUE_TZ) + timedelta(days=1)


def _offer_sheet_outcome_team(details: dict) -> Optional[str]:
    """Which team ends up with the player, from a resolved offer sheet's details.

    `teams` is stored ``[offering, retaining]``. A match leaves the player with
    the retaining (incumbent) team; an unmatched offer sends them to the offering
    team. Works on both the legacy combined entry and the split decision, since
    both carry `outcome` plus the same team fields.
    """
    outcome = details.get("outcome")
    if outcome not in ("matched", "not_matched"):
        return None
    teams = [t.upper() for t in (details.get("teams") or []) if t]
    offering = (details.get("offering_team") or (teams[0] if teams else "")).upper()
    retaining = (details.get("retaining_team") or "").upper() or \
        next((t for t in teams if t != offering), "")
    team = retaining if outcome == "matched" else offering
    return team if team in VALID_TEAMS else None


def _offer_deadline(date_str: Optional[str]) -> Optional[str]:
    """§ 3.15 gives the retaining team 48 hours to match. Recorded and displayed,
    but never acted on automatically — an offer past its deadline nags rather
    than resolving itself, because silently moving a real player on a timer is
    worse than a late decision."""
    if not date_str:
        return None
    try:
        return (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=2)).strftime("%Y-%m-%d")
    except ValueError:
        return None


def _offer_hold_amount(contract: dict) -> int:
    """§ 3.15's "cap hold equal to the offer sheet value" — read as the offer's
    **Year 1** salary.

    A cap hold is a single-season charge against a single season's cap, so the
    total across all years can't be the figure meant: a 4yr/$200M offer would
    otherwise put a $200M hold on a $165M cap. Year 1 is also what every other
    hold in the system charges, and what the contract's Year 1 would cost if the
    offer converts.
    """
    salaries = (contract or {}).get("salaries") or {}
    years = sorted(y for y in salaries if _YEAR_RE_TXN.match(y))
    return _parse_dollar(salaries[years[0]]) if years else 0


_YEAR_RE_TXN = re.compile(r"^\d{2}-\d{2}$")


def _season_start_of(date: str) -> str:
    """The league-year season string a calendar date falls in. The league year
    turns over on July 1, so anything from July on belongs to YY-(YY+1)."""
    y, m = int(date[:4]), int(date[5:7])
    return season_starting(y if m >= 7 else y - 1)


def _compute_fa_hold_amount(
    prev_salary: str,
    bird_tier: str,
    hold_season: str,
    cap_levels: dict,
    eaps_assumption: Optional[str] = None,
    min_amt: Optional[int] = None,
    max_amt: Optional[int] = None,
) -> tuple[int, Optional[str]]:
    """§ 3.10 veteran free-agent hold: `bird_tier`'s percentage of
    `prev_salary` (the last real salary before the hold season), clamped to
    [min_amt, max_amt]. Returns (amount, note) — note is set only when the
    figure rests on an eaps_assumption rather than a real cap number, so
    callers can flag the result as a placeholder pending real EAPS data.

    Doesn't implement § 3.10's rookie-scale-final-year (250%/300%) carve-out,
    which still needs a manually-entered cap_hold_amount. The
    coming-off-a-minimum-contract carve-out isn't a percentage at all, so it
    lives in `_autofill_fa_hold_amounts` (`_minimum_contract_hold`).
    """
    prev = _parse_dollar(prev_salary)
    note = None
    if bird_tier == "QVFA":
        eaps = (cap_levels.get(hold_season, {}) or {}).get("eaps") or None
        if eaps:
            pct = 1.5 if prev > eaps else 1.9
        else:
            if eaps_assumption not in ("above", "below"):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Full Bird (QVFA) hold for {hold_season} needs to know whether the "
                        f"previous salary (${prev:,}) is above or below that season's EAPS to "
                        f"pick 150% vs 190% (§ 3.10), and {hold_season} has no EAPS on file yet. "
                        f'Pass eaps_assumption: "above" or "below" to compute a placeholder.'
                    ),
                )
            pct = 1.5 if eaps_assumption == "above" else 1.9
            note = (f"placeholder — {hold_season} EAPS not yet set; assumed previous salary is "
                    f"{eaps_assumption} EAPS ({int(pct * 100)}% of ${prev:,})")
    else:
        pct = _BIRD_HOLD_PCT.get(bird_tier, 1.2)
    amount = round(prev * pct)
    if max_amt:
        amount = min(amount, max_amt)
    if min_amt:
        amount = max(amount, min_amt)
    return amount, note


def _minimum_contract_hold(bio: dict, season: str, cap_levels: dict) -> Optional[int]:
    """§ 3.10: a player coming off a minimum contract carries a hold of the
    minimum salary, capped at the 2-year veteran minimum — the same figure
    § 3.12 charges for a 1-year minimum deal, so it is that helper's answer
    for the hold season. Experience comes from the contract the hold follows
    when it declared one (climbing a row per year, `_contract_years_exp`),
    else years of service (`years_of_service`). None when the season has no scale yet."""
    last = (bio.get("contracts") or [None])[-1] or {}
    contract = None
    if last.get("years_experience") is not None:
        contract = SimpleNamespace(years_experience=last["years_experience"],
                                   salaries=last.get("salaries") or {},
                                   cap_holds=last.get("cap_holds") or {})
    return _one_year_min_cap_hit(bio, season, cap_levels, contract=contract)


def _one_year_min_cap_hit(bio: dict, season: str, cap_levels: dict,
                          contract=None) -> Optional[int]:
    """§ 3.12: a 1-year minimum contract's cap hit is capped at the 2-year
    veteran minimum, regardless of the player's actual NBA experience tier —
    the league reimburses the difference, mirroring the NBA's veteran-minimum
    hardship exception. Experience is the contract's declared figure, else
    the player's years of service (`years_of_service`). Returns None if experience or the season's scale
    can't be determined (e.g. undrafted player, scale not configured yet) —
    callers should treat that as "can't check," not "no cap applies."
    """
    scale = (cap_levels.get(season, {}) or {}).get("min_salary_scale") or {}
    if not scale:
        return None
    years_exp = _contract_years_exp(contract, season)
    if years_exp is None:
        years_exp = years_of_service.years_of_service(bio, season)
        if years_exp is None:
            return None
    tier_amt = scale.get(_min_salary_scale_tier(years_exp), 0)
    two_yr_amt = scale.get("2", 0)
    if tier_amt <= 0:
        return None
    # Only 2+ year tiers get capped down — the 0/1-yr tiers already sit at or
    # below the 2-yr number, so there's nothing to reimburse for them.
    if _min_salary_scale_tier(years_exp) in ("0", "1") or not two_yr_amt:
        return tier_amt
    return min(tier_amt, two_yr_amt)


def _empty_roster_charge(standard_count_after: int, season: str, cap_levels: dict) -> tuple[int, int]:
    """Returns (deficiency, charge) for a team projected below the ROSTER_MIN
    (14) standard-roster minimum, for TRADE-LEGALITY purposes only — § 2.1a.
    This is a wider floor than ROSTER_CHARGE_MIN (12), which is where a real,
    persisted Empty Roster Charge actually posts to a team's roster/guaranteed
    salary (see team.js's computeEmptyRosterCharge for that one — it isn't
    computed here, since it isn't specific to trades). This function's charge
    applies immediately and year-round for the hard-cap comparison (not gated
    on the § 2.1 one-week grace period, which separately governs when activity
    strikes escalate for a team that stays non-compliant): a team can't use a
    trade to duck under a hard cap by shedding headcount, since the league
    treats the empty slot(s) up to 14 as costing at least that season's
    rookie minimum, whether or not the team is actually below 12 yet."""
    deficiency = max(0, ROSTER_MIN - standard_count_after)
    if deficiency == 0:
        return 0, 0
    return deficiency, deficiency * _rookie_min_salary(season, cap_levels)


def _real_empty_roster_charge(standard_count: int, season: str, cap_levels: dict) -> tuple[int, int]:
    """Returns (deficiency, charge) for the REAL Empty Roster Charge a team is
    carrying right now — § 2.1a's narrow 12-player floor, not the 14-player
    trade-legality mock in `_empty_roster_charge` above.

    The difference between the two matters and they are not interchangeable:
    this one is a team's actual books ("what does this roster cost today"), the
    other is a hypothetical the trade validator folds into a *projection*
    ("what would this roster cost if every slot up to 14 were filled"). A team
    at 13 carries a mock of one slot and a real charge of zero.

    `standard_count` is the caller's own count so this does not re-read the
    roster CSV — every caller already has it. It is the count as the books
    stand, never a post-transaction one: the charge on a projection has to be
    recomputed from the count *after* the move, since a signing that fills the
    slot erases the charge it was carrying.

    This is the Python twin of `computeEmptyRosterCharge` in team.js, which is
    what puts the same charge on the team page as a roster line item and into
    the Cap Health card's Team Salary. Keep the two in step.
    """
    deficiency = max(0, ROSTER_CHARGE_MIN - standard_count)
    if deficiency == 0:
        return 0, 0
    return deficiency, deficiency * _rookie_min_salary(season, cap_levels)


_FA_HOLD_TYPES = {"UFA", "RFA"}


def _hard_cap_check(team: str, projected: int, season: str,
                    team_state: dict, cap_levels: dict) -> Optional[CheckResult]:
    ts  = get_season_state(team_state, team, season)
    hc  = ts.get("hard_cap")
    if not hc:
        return None
    cl  = cap_levels.get(season, {})
    limit = cl.get("apron1") if hc == "first_apron" else cl.get("apron2")
    if limit is None or projected <= limit:
        return None
    label = "First Apron" if hc == "first_apron" else "Second Apron"
    over  = projected - limit
    return CheckResult(
        check=f"hard_cap_{team.lower()}",
        passed=False,
        level="error",
        message=(
            f"{team} would be ${over:,.0f} over their hard cap ({label}: ${limit:,}) "
            f"— projected salary ${projected:,}."
        ),
    )


def _universal_hard_cap_check(team: str, projected: int, season: str,
                               cap_levels: dict) -> Optional[CheckResult]:
    cl    = cap_levels.get(season, {})
    limit = cl.get("hard_cap") or 0
    if not limit or projected <= limit:
        return None
    over = projected - limit
    return CheckResult(
        check=f"hard_cap_league_{team.lower()}",
        passed=False,
        level="error",
        message=(
            f"{team} would be ${over:,.0f} over the league Hard Cap (${limit:,}) "
            f"— projected salary ${projected:,}."
        ),
    )


def _salary_match_limit(outgoing: int) -> int:
    """Max incoming salary allowed under standard (below First Apron) tiered matching (§ 4.2)."""
    if outgoing <= SALARY_MATCH_TIER1_CAP:
        return 2 * outgoing + 250_000
    elif outgoing <= SALARY_MATCH_TIER2_CAP:
        return outgoing + 8_527_000
    else:
        return round(1.25 * outgoing) + 250_000


def _check_contract_raises(
    contract: ContractIn, bird_pct: bool, cur_season: str,
    bio: Optional[dict] = None, cap_levels: Optional[dict] = None,
    pct: Optional[float] = None,
) -> Optional[CheckResult]:
    """§ 3.9 / § 3.13: no year-over-year change above 5% (8% with Full or
    Early Bird) of Year 1.

    **Minimum-scale years are exempt.** § 3.12 says a multi-year minimum deal
    pays "the scale for the player's years of experience that season," and the
    scale's own steps are far bigger than 5% at the bottom — a 26-27 rookie
    minimum rising to the 1-year tier is a 61% raise. Applying the ladder to it
    rejected a contract the rulebook explicitly prescribes. Pass `bio` and
    `cap_levels` to enable the exemption; without them the ladder applies to
    every year (the old behaviour), since there is no scale to compare against.

    `pct` overrides the `bird_pct`-derived figure outright — § 6.2 rule 8 uses
    the same ladder but with its own ceilings (8% normal, 5% extend-and-trade),
    the inverse of a signing's 5%/8% split, so `bird_pct` alone can't express
    it. Leave unset for the signing path's existing behaviour.
    """
    if contract.type == "two-way":
        return None

    if pct is None:
        pct = 0.08 if bird_pct else 0.05
    pct_label = f"{pct * 100:.0f}%"

    _HOLD_TYPES = {"UFA", "RFA", "PLAYER_OPT", "TEAM_OPT"}
    hold_years = {yr for yr, ht in (contract.cap_holds or {}).items() if ht in _HOLD_TYPES}

    years = sorted(
        (yr for yr in contract.salaries if yr >= cur_season and yr not in hold_years),
        key=lambda y: (_season_start(y), y),
    )
    if len(years) < 2:
        return None

    yr1_sal = _parse_dollar(contract.salaries[years[0]])
    if yr1_sal == 0:
        return None

    def _at_minimum(yr: str, salary: int) -> bool:
        # 5% of grace above the scale figure — the same margin the ladder
        # itself allows — because the alternative is failing a rules check
        # over a rounded entry: a team that types "$1,400,000" for a
        # $1,357,763 rookie minimum has still written a minimum deal. It
        # can't disguise a real raise, since both ends of the step must sit
        # on their own season's minimum before anything is exempted.
        if not cap_levels or (bio is None and _contract_years_exp(contract, yr) is None):
            return False
        ceiling = _minimum_year_ceiling(bio or {}, yr, cap_levels, contract)
        return ceiling is not None and salary <= round(ceiling * 1.05) + 1

    max_step = round(pct * yr1_sal)
    violations, exempt = [], []
    for i in range(1, len(years)):
        prev_sal = _parse_dollar(contract.salaries[years[i - 1]])
        this_sal = _parse_dollar(contract.salaries[years[i]])
        diff = abs(this_sal - prev_sal)
        if diff <= max_step + 1:
            continue
        if _at_minimum(years[i - 1], prev_sal) and _at_minimum(years[i], this_sal):
            exempt.append(years[i])
            continue
        violations.append(
            f"{years[i]}: ${prev_sal:,} → ${this_sal:,} "
            f"(Δ${diff:,}, max ${max_step:,})"
        )

    if violations:
        return CheckResult(
            check="raise_limit",
            passed=False,
            level="error",
            message=(
                f"Raise/decrease limit violated ({pct_label} of Year 1 = ${max_step:,}/yr): "
                + "; ".join(violations)
            ),
        )
    if exempt:
        return CheckResult(
            check="raise_limit",
            passed=True,
            message=(
                f"Every year is at the § 3.12 minimum salary scale, so the {pct_label} "
                f"raise ladder doesn't apply to the step into "
                + ", ".join(exempt) + "."
            ),
        )
    return None


def _tpe_bankable(
    outgoing: int,
    incoming: int,
    team_salary_after: int,
    cap: Optional[int],
    used_exception: bool,
) -> Optional[int]:
    """§ 4.1a: the Trade Exception this team banks out of a trade, or None.

    Three independent gates, all of which must pass:
      * it sent out more salary than it took back (there is a difference);
      * it did not absorb incoming salary through an exception or another TPE
        in this same trade (can't absorb via one and bank another);
      * it is over the Salary Cap after the trade — a team under the Cap
        absorbs the difference into cap room instead (§ 4.2), and handing it an
        exception on top would let the same space be spent twice.

    `team_salary_after` is the full post-trade team salary *including* cap
    holds, matching how cap-room absorption measures room."""
    if cap is None or used_exception:
        return None
    if outgoing <= incoming:
        return None
    if team_salary_after <= cap:
        return None
    return outgoing - incoming


def _check_bae_eligibility(
    team: str,
    team_salary_ex_holds_before: int,
    cl: dict,
    team_state: Optional[dict],
    season: str,
) -> Optional[CheckResult]:
    """Shared BAE eligibility gate (§ 3.5), used by both the free-agent
    signing path and the trade-absorption path: below the First Apron, not
    already used this season, not used in the immediately prior season
    (`_bae_available`), and not already used alongside cap space or the Room
    Exception this same season. Returns None when eligible."""
    check_name = f"salary_matching_{team.lower()}"
    apron1 = cl.get("apron1")
    ts = get_season_state(team_state or {}, team, season)

    if apron1 is None or team_salary_ex_holds_before >= apron1:
        return CheckResult(
            check=check_name, passed=False, level="error",
            message=(f"{team} is not eligible for the Bi-Annual Exception — Team Salary "
                      f"(${team_salary_ex_holds_before:,}) is at/above the First Apron "
                      f"({'$' + format(apron1, ',') if apron1 is not None else 'unset'}) — § 3.5."),
        )
    if ts.get("bae_used"):
        return CheckResult(
            check=check_name, passed=False, level="error",
            message=f"{team} has already used the Bi-Annual Exception this season (§ 3.5).",
        )
    if ts.get("mle_type") in ("room", "room_exception"):
        return CheckResult(
            check=check_name, passed=False, level="error",
            message=(f"{team} has already used cap space or the Room Exception this season — "
                      "the BAE cannot also be used the same season (§ 3.5)."),
        )
    if not _bae_available(team_state or {}, team, season):
        return CheckResult(
            check=check_name, passed=False, level="error",
            message=f"{team} used the Bi-Annual Exception last season — unavailable in consecutive years (§ 3.5).",
        )
    return None


def _mle_zone_from_salary(
    team_salary_full: Optional[int], team_salary_ex_holds: Optional[int], cl: dict
) -> Optional[str]:
    """§ 3.2 zone table (room / ntmle / tmle) off live Team Salary — the same
    rule `computeMleType` in teams/team.js uses to decide what a team's page
    shows it holds. `_resolve_mle_bucket` falls through to this so an
    unassigned generic "mle" resolves to whichever bucket the team page
    already displays, rather than a hardcoded default.

    Returns None (not a guess) when the inputs aren't available — an
    unconfigured cap-levels season, or a caller that only has scalar figures
    (this module's own test suite exercising the pre-existing scalar path).
    """
    if team_salary_full is None or team_salary_ex_holds is None:
        return None
    cap, apron1, ntmle = cl.get("cap"), cl.get("apron1"), cl.get("ntmle_amount")
    if cap is None or apron1 is None or ntmle is None:
        return None
    if cap - team_salary_full > ntmle:
        return "room"
    if apron1 - team_salary_ex_holds >= ntmle:
        return "ntmle"
    return "tmle"


def _resolve_mle_bucket(
    method: Optional[str], ts: dict,
    team_salary_full: Optional[int] = None,
    team_salary_ex_holds: Optional[int] = None,
    cl: Optional[dict] = None,
) -> Optional[tuple[str, str, str]]:
    """Which exception bucket an exception-funded `signing_method` actually
    draws on, as ``(resolved_method, cap_levels_amount_key, display_label)``.

    A generic "mle" resolves against whatever bucket the team is already
    locked into this season (`mle_type`). If nothing is locked yet, it falls
    to `_mle_zone_from_salary` — the § 3.2 zone table off live Team Salary —
    rather than a hardcoded default, so this can't disagree with what the
    team page shows the team holds. Only degrades to "ntmle" when the zone
    genuinely can't be computed (no salary figures supplied). Returns None
    for methods that aren't a running dollar balance (cap space, Bird Rights,
    minimum, BAE, sign-and-trade) — those have dedicated checks.

    Shared by `_check_signing_method_funding` and the simulator's fact sheet so
    the balance shown is the balance the validator charged against.
    """
    resolved = method
    if resolved == "mle":
        resolved = (ts.get("mle_type")
                    or _mle_zone_from_salary(team_salary_full, team_salary_ex_holds, cl or {})
                    or "ntmle")
    if resolved == "room":          # mle_type stores the short form
        resolved = "room_exception"
    meta = {
        "ntmle":          ("ntmle_amount", "Non-Taxpayer MLE"),
        "tmle":           ("tmle_amount",  "Taxpayer MLE"),
        "room_exception": ("room_amount",  "Room Exception"),
    }.get(resolved)
    return (resolved, meta[0], meta[1]) if meta else None


def _check_bae_absorption(
    team: str,
    incoming: int,
    team_salary_ex_holds_before: int,
    cl: dict,
    team_state: Optional[dict],
    season: str,
) -> CheckResult:
    """BAE trade absorption (§ 4.2a, extended to include the BAE alongside
    NTMLE/TMLE/Room Exception). Unlike those three, the BAE isn't a running
    dollar balance — it's a once-per-season boolean (`bae_used`), so it can't
    share `_check_exception_absorption`'s amount-key/`mle_used` bookkeeping."""
    check_name = f"salary_matching_{team.lower()}"
    if (r := _check_bae_eligibility(team, team_salary_ex_holds_before, cl, team_state, season)):
        return r

    total = cl.get("bae_amount", 0) or 0
    if incoming > total:
        over = incoming - total
        return CheckResult(
            check=check_name, passed=False, level="error",
            message=(f"{team}: Bi-Annual Exception absorption failed — incoming salary ${incoming:,} "
                      f"exceeds the BAE amount of ${total:,} by ${over:,}."),
        )
    return CheckResult(
        check=check_name, passed=True,
        message=(f"{team}: incoming salary ${incoming:,} absorbed via the Bi-Annual Exception "
                  f"(${total:,} available this season) — no salary match required."),
    )


def _exception_absorption_split(
    details,
    team: str,
    incoming_total: int,
    in_players: dict,
    bios: dict,
    season: str,
) -> tuple[int, int, Optional[str]]:
    """How a team's incoming salary divides between its exception and ordinary
    salary matching.

    § 4.2a is per-player — "a team uses exactly one of the four for a given
    incoming player" — so a trade can legitimately match some incoming players
    against outgoing salary while funding another out of an exception. When
    `exception_players[team]` names slugs, only those are absorbed and the rest
    still has to match. When it doesn't, the whole incoming total is treated as
    absorbed, which is the original all-or-nothing behavior.

    Returns (absorbed, matched, error). `error` is a message when the named
    slugs don't correspond to players this team is actually receiving — the
    caller turns that into a failed check rather than silently mis-splitting.

    Shared by the validator and _apply_trade so the amount that gets checked is
    always the amount that gets billed to the exception's balance.
    """
    slugs = [s for s in (getattr(details, "exception_players", None) or {}).get(team, []) if s]
    if not slugs:
        return incoming_total, 0, None

    received = in_players.get(team, [])
    unknown = [s for s in slugs if s not in received]
    if unknown:
        return 0, 0, (
            f"{team}: exception_players names {', '.join(sorted(unknown))}, "
            f"which {'is' if len(unknown) == 1 else 'are'} not incoming to {team} in this trade."
        )

    absorbed = sum(
        _parse_dollar((bios.get(s, {}).get("salaries") or {}).get(season, ""))
        for s in set(slugs)
    )
    return absorbed, max(incoming_total - absorbed, 0), None


def _cap_room_absorbed(team_salary_before: int, outgoing: int, incoming: int, cap: Optional[int]) -> bool:
    """§ 4.2 cap-room absorption: true when `incoming` can be covered purely by
    the team's own room under the Salary Cap, with no salary match required.

    Shared by `_check_salary_matching` and the § 4.3/§ 4.3 contagion checks —
    a trade genuinely funded this way carries no apron consequence, matching
    real NBA behavior: a cap-room acquisition never touches the apron, only
    the named exceptions (NTMLE/TMLE/BAE) and over-the-cap tiered matching do.
    """
    if cap is None or team_salary_before >= cap:
        return False
    return team_salary_before - outgoing + incoming <= cap


def _check_trailing_hold(contract) -> Optional[CheckResult]:
    """§ 3.10: a contract rolls into a free-agent hold the season after it
    ends, carried as a UFA/RFA tag on that season. Nothing adds the tag for
    the office — its contract helpers default to one, but a hand-built deal
    can leave it off, and 24 of the 2026 FA wave went in that way, each
    silently dropping a hold from its team's out-year books (2026-09-24).
    A warning, not an error: a deal that truly ends with no hold (a
    retirement) can be forced through."""
    salaries = contract.salaries or {}
    holds = contract.cap_holds or {}
    if not salaries or contract.type == "two-way":
        return None
    years = [yr for yr in salaries if holds.get(yr) not in _FA_HOLD_TYPES]
    if not years:
        return None
    last = max(years, key=_season_start)
    after = _season_shift(last, 1)
    if holds.get(after) in _FA_HOLD_TYPES:
        return CheckResult(check="trailing_hold", passed=True, level="warning",
                           message=f"Rolls into a {holds[after]} hold for {after} (§ 3.10).")
    return CheckResult(
        check="trailing_hold", passed=False, level="warning",
        message=(f"This contract ends in {last} with no UFA or RFA hold for {after}. An expiring "
                 f"contract rolls into a free-agent hold (§ 3.10). Add a trailing hold row, or "
                 f"force this through only if the player won't carry one."),
    )


def _min_scale_for_season(season: str, cap_levels: dict) -> dict:
    """`season`'s minimum salary scale, falling back to the latest configured
    season at or before it. Later league years routinely have no scale on file
    yet — a contract signed today can easily run past the last season the
    office has set figures for."""
    scale = (cap_levels.get(season, {}) or {}).get("min_salary_scale") or {}
    if scale:
        return scale
    for yr in sorted(cap_levels, key=_season_start, reverse=True):
        if _season_start(yr) <= _season_start(season):
            older = (cap_levels.get(yr) or {}).get("min_salary_scale") or {}
            if older:
                return older
    return {}


def _min_salary_floor(season: str, cap_levels: dict) -> int:
    """The 0-years tier for `season`, falling back to the latest configured
    season when that one has no scale yet. Minimum scales only ever rise, so
    an older season's figure is a conservative floor for a later one — it can
    under-flag, never produce a false positive."""
    return _min_scale_for_season(season, cap_levels).get("0", 0)


def _minimum_year_ceiling(bio: dict, season: str, cap_levels: dict,
                          contract=None) -> Optional[int]:
    """The most a § 3.12 minimum contract can pay this player in `season` — the
    figure that decides whether a contract year is a minimum year at all.

    The contract's own declared experience when it has one, else their tier by
    the draft year, else the top (10+) row, since nothing above the veteran
    minimum is a minimum contract for anyone. Returns None when no scale is
    configured at or before `season`, which callers must read as "can't tell",
    not "not a minimum"."""
    scale = _min_scale_for_season(season, cap_levels)
    if not scale:
        return None
    years_exp = _contract_years_exp(contract, season)
    if years_exp is None:
        years_exp = years_of_service.years_of_service(bio or {}, season)
    if years_exp is not None:
        amt = scale.get(_min_salary_scale_tier(years_exp))
        if amt:
            return amt
    return max(scale.values())


def _prorate(amount: int, proration: Optional[dict]) -> int:
    """`amount` scaled by a `_minimum_proration` result, rounded to the
    dollar (half up, so the transactions form's Math.round agrees)."""
    if not proration:
        return amount
    return int(amount * proration["days"] / proration["of"] + 0.5)


def _check_declared_experience(contract, player: str, bios: dict) -> Optional[CheckResult]:
    """§ 3.12: a contract's declared years of experience, against the
    player's years of service on file (`years_of_service`). Declaring fewer
    prices a minimum on a cheaper tier, and can read a player as two-way
    eligible (§ 2.2) when he isn't, so it is a warning a person has to confirm.

    Not an error: the count on file can miss a season he sat on a roster
    without playing. Declaring more only costs the team, so that passes.
    Added after Blake Hinson's ORL minimum (2026-10-08), which declared 0
    against a draft-year count of 2. Under the 2026-11-10 ruling (NBN years,
    not NBA) his 0 was right: he hadn't been on an NBN roster.

    None when the contract declares nothing or the count can't be made."""
    declared = getattr(contract, "years_experience", None)
    bio = bios.get(player) or {}
    first = _contract_first_season(contract)
    if declared is None or not first:
        return None
    expected = years_of_service.years_of_service(bio, first, player)
    if expected is None:
        return None
    name = _display_name(bio.get("name") or "") or player
    if int(declared) < expected:
        return CheckResult(
            check="declared_experience", passed=False, level="warning",
            message=(f"The contract gives {name} {int(declared)} years of experience in {first}, "
                     f"but his years of service on file give {expected} (§ 3.12). Fewer years "
                     f"means a cheaper minimum tier. Use {expected} unless the count is wrong."),
        )
    return CheckResult(
        check="declared_experience", passed=True, level="info",
        message=(f"Declared experience ({int(declared)} in {first}) is not below his "
                 f"{expected} years of service on file (§ 3.12)."),
    )


def _min_salary_for(bio: dict, season: str, cap_levels: dict,
                    contract=None) -> Optional[int]:
    """That season's minimum for this player's experience tier, or None when
    experience can't be established (nothing declared on the contract, no
    draft year, or scale not configured)."""
    scale = (cap_levels.get(season, {}) or {}).get("min_salary_scale") or {}
    if not scale:
        return None
    years_exp = _contract_years_exp(contract, season)
    if years_exp is None:
        years_exp = years_of_service.years_of_service(bio, season)
        if years_exp is None:
            return None
    return scale.get(_min_salary_scale_tier(years_exp)) or None


def _check_max_salary(details: SignDetails, bios: dict, season: str,
                       cap_levels: dict) -> Optional[CheckResult]:
    """§ 3.11: a signed player's current-season salary shouldn't exceed their
    experience-tier maximum (25/30/35% of that season's Salary Cap). Skipped
    for two-way contracts (no cap salary) and undrafted players (no
    draft_year experience proxy). Advisory only — § 3.11 is manual review,
    not system-enforced, and years of NBA experience is inferred from
    draft_year, not independently verified.
    """
    if details.contract.type == "two-way":
        return None
    salaries = details.contract.salaries or {}
    if season not in salaries:
        return None
    expected = _max_salary(bios.get(details.player, {}), season, cap_levels)
    if expected is None:
        return None
    submitted = _parse_dollar(salaries[season])
    if submitted <= expected:
        return CheckResult(
            check="max_salary", passed=True,
            message=f"Year 1 salary (${submitted:,}) is within the § 3.11 max of ${expected:,}.",
        )
    return CheckResult(
        check="max_salary",
        passed=False,
        level="warning",
        message=(f"Submitted Year 1 salary (${submitted:,}) exceeds the § 3.11 max of ${expected:,} "
                 f"for this player's years of service — double check before submitting."),
    )


def _release_contract_years(bio: dict, cur_season: str) -> list[str]:
    """The seasons from `cur_season` on that carry a real contract to release.
    A player whose every remaining season is a bare UFA/RFA/TEAM_OPT hold (or
    who has no salaries at all) has none — they're renounceable instead
    (§ 3.10), and releasing them would just ceremonially reproduce a renounce
    through the wrong transaction.

    A two-way's contract years count even though they pay $0 (§ 2.2). Testing
    for salary alone left a two-way with no way off a roster: release said
    "nothing to release, renounce instead", and renounce said "under contract,
    release instead"."""
    holds = bio.get("cap_holds") or {}
    salaries = bio.get("salaries") or {}
    two_way = bio.get("type") == "two-way"
    return [
        s for s, sal in salaries.items()
        if s >= cur_season and holds.get(s) not in ("UFA", "RFA", "TEAM_OPT")
        and (two_way or _parse_dollar(sal))
    ]


def _released_counts(slugs: list[str], bios: dict) -> tuple[int, int]:
    """(standard, two-way) bodies among `slugs` — which pool each release frees."""
    tw = sum(1 for s in slugs if (bios.get(s) or {}).get("type") == "two-way")
    std = sum(1 for s in slugs if _is_standard_roster_slot((bios.get(s) or {}).get("type", "")))
    return std, tw


def _is_exempt_minimum_contract(slug: str, season: str, bios: dict) -> bool:
    """§ 4.2: a player on a minimum contract of 2 seasons or fewer doesn't
    count as incoming salary for trade matching purposes. A minimum contract
    of more than 2 seasons does count.

    Derived from the player's own `contracts` ledger entry (signing_method +
    the count of salaried years on that specific deal) rather than a stored
    tag, so it can never drift from the deal that was actually signed — the
    same "denormalized copy" trap this repo already got burned by once (the
    old roster-CSV OVR column, never kept in sync and always empty). Only
    available for players signed through this system — a historical/
    backfilled deal has no `contracts` entry, so it's conservatively treated
    as non-exempt (no regression from before this existed).
    """
    for entry in (bios.get(slug, {}).get("contracts") or []):
        salaries = entry.get("salaries") or {}
        if season in salaries and entry.get("signing_method") == "minimum":
            return len(salaries) <= 2
    return False


def _trade_flows(details, bios: dict, season: str) -> tuple[dict, dict, dict, dict]:
    """Per-team salary and player-slug flows for a trade.

    Returns (outgoing_salary, incoming_salary, out_players, in_players), each
    keyed by team abbreviation. Pick assets carry no salary or roster slot, so
    only player assets are accumulated. Shared by the validator and fact sheet
    so the two never diverge on what "this trade moves" means.
    """
    outgoing: dict[str, int] = {}
    incoming: dict[str, int] = {}
    out_players: dict[str, list[str]] = {}
    in_players: dict[str, list[str]] = {}
    for xfer in details.transfers:
        from_t = xfer.from_team.upper()
        to_t   = xfer.to_team.upper()
        for asset in xfer.assets:
            if asset.type != "player" or not asset.slug:
                continue
            sal = _parse_dollar((bios.get(asset.slug, {}).get("salaries") or {}).get(season, ""))
            outgoing[from_t] = outgoing.get(from_t, 0) + sal
            incoming[to_t]   = incoming.get(to_t, 0)   + sal
            out_players.setdefault(from_t, []).append(asset.slug)
            in_players.setdefault(to_t, []).append(asset.slug)
    return outgoing, incoming, out_players, in_players


def _add_months(d: datetime, months: int) -> datetime:
    """Calendar-correct month addition — no `dateutil` in this project, and
    a fixed day-count (182/183?) is exactly the kind of rounding a real
    six-month legal deadline shouldn't run on. Clamps the day for a target
    month shorter than the source (Aug 31 + 6mo -> Feb 28/29, not Mar 3)."""
    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    days_in_month = [31, 29 if (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)) else 28,
                     31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    day = min(d.day, days_in_month[month - 1])
    return d.replace(year=year, month=month, day=day)


def _option_window_check(bio: dict, year: str, date: str, name: str) -> CheckResult:
    """§ 6.1's decision windows (league office, 2026-10-19). A rookie-scale
    option opens with the league year before it (2026-11-02), is due by the
    deadline and is declined automatically after it; any
    other team option is decided between the end of the previous regular
    season and June 30. An error, so an owner can't decide outside it; the
    office can still force one through for a correction."""
    w = season_calendar.team_option_window(bio, year)
    when = lambda d: datetime.strptime(d, "%Y-%m-%d").strftime("%b %-d, %Y")
    if w["kind"] == "rookie":
        if w["opens"] and date < w["opens"]:
            return CheckResult(check="option_window", passed=False, level="error",
                               message=f"{name}'s {year} option is a rookie-scale option, decided from "
                                       f"{when(w['opens'])} through {when(w['closes'])}. Only next "
                                       f"season's options can be decided now (§ 6.1).")
        if w["closes"] and date > w["closes"]:
            return CheckResult(check="option_window", passed=False, level="error",
                               message=f"{name}'s {year} option is a rookie-scale option, due by "
                                       f"{when(w['closes'])}. That deadline has passed, and an option "
                                       f"not exercised by then is declined automatically (§ 6.1).")
        return CheckResult(check="option_window", passed=True,
                           message=f"Rookie-scale option, due by {when(w['closes'])} (§ 6.1)."
                                   if w["closes"] else "Rookie-scale option (§ 6.1).")
    if w["closes"] and date > w["closes"]:
        return CheckResult(check="option_window", passed=False, level="error",
                           message=f"The window for {name}'s {year} option closed on {when(w['closes'])}, "
                                   f"before that league year began (§ 6.1).")
    if not w["opens"]:
        return CheckResult(check="option_window", passed=False, level="warning",
                           message=f"{name}'s {year} option can only be decided after the regular season "
                                   f"ends, and that season's schedule isn't on file to check (§ 6.1).")
    if date < w["opens"]:
        return CheckResult(check="option_window", passed=False, level="error",
                           message=f"{name}'s {year} option isn't a rookie-scale option, so it can only be "
                                   f"decided once the regular season ends: from {when(w['opens'])} "
                                   f"through {when(w['closes'])} (§ 6.1).")
    return CheckResult(check="option_window", passed=True,
                       message=f"Inside the option window, open through {when(w['closes'])} (§ 6.1).")


def _extension_contract_years(bio: dict) -> list[str]:
    """Real contract years on the bio, sorted — the trailing UFA/RFA hold
    season is not a contract year (§ 6.2 step 0 in extensions.md § 4): it
    carries a salary figure (auto-filled by _autofill_fa_hold_amounts) that
    would otherwise read as one more year of the deal."""
    salaries = bio.get("salaries") or {}
    cap_holds = bio.get("cap_holds") or {}
    return sorted(
        (s for s in salaries if _YEAR_RE_TXN.match(s) and cap_holds.get(s) not in _FA_HOLD_TYPES),
        key=_season_start,
    )


def _extension_year1_ceiling(prior_salary: int, cl: dict) -> dict:
    """§ 6.2's Year 1 ceiling for a veteran extension: the greater of 140% of
    prior salary and 140% of the first extended season's EAPS. One helper so
    the validator and the fact sheet can't disagree. `ceiling` is None when
    neither half can be priced."""
    eaps = cl.get("eaps") or None
    halves = []
    if prior_salary:
        halves.append((round(prior_salary * 1.4), "prior_salary",
                       f"140% of prior salary ${prior_salary:,}"))
    if eaps:
        halves.append((round(eaps * 1.4), "eaps", f"140% of EAPS ${eaps:,}"))
    best = max(halves, key=lambda h: h[0]) if halves else (None, None, None)
    return {"ceiling": best[0], "basis": best[1], "label": best[2],
            "eaps_set": bool(eaps), "eaps_estimate": bool(eaps and cl.get("is_estimate"))}


def _extension_is_rookie_scale(bio: dict, frame: dict) -> bool:
    """§ 6.3's rookie-scale bucket, read off the bio rather than taken from
    the proposal: a first-round pick whose four-year rookie deal is the
    contract being extended — the same test § 3.1 uses for RFA eligibility,
    asked about the season after the deal ends."""
    end = frame.get("contract_end")
    return bool(end) and _finishes_rookie_scale(bio, _season_shift(end, 1))


def _final_guaranteed_year(bio: dict) -> Optional[str]:
    """§ 6.2 rule 10 / extensions.md § 4: the last season of the existing
    contract the extension's new money starts after.

    Rule 1: where `guaranteed` explicitly marks a season as fully guaranteed
    (guaranteed[s] >= salaries[s]), the last such season wins.
    Rule 2 (the one that actually fires — `guaranteed` is populated for a
    sliver of bios): every remaining contract year is treated as guaranteed
    **except** NON_GTD/PLAYER_OPT/TEAM_OPT years, ratified 2026-08-18
    (poext-extension-pipeline.md D11/D2 in extensions.md § 9) — an option
    year is treated as declined outright by the act of extending, so it never
    counts as "guaranteed" and the extension supersedes it rather than
    colliding with it.
    """
    contract_years = _extension_contract_years(bio)
    if not contract_years:
        return None
    guaranteed = bio.get("guaranteed") or {}
    fully_gtd = [
        s for s in contract_years
        if guaranteed.get(s) and _parse_dollar(guaranteed[s]) >= _parse_dollar((bio.get("salaries") or {}).get(s))
    ]
    if fully_gtd:
        return max(fully_gtd, key=_season_start)
    cap_holds = bio.get("cap_holds") or {}
    non_gtd_types = {"NON_GTD", "PLAYER_OPT", "TEAM_OPT"}
    candidate = [s for s in contract_years if cap_holds.get(s) not in non_gtd_types]
    if candidate:
        return max(candidate, key=_season_start)
    return None


def _extension_eligibility_check(player: str, frame: dict, cur_season: str,
                                 attested_contract_start: Optional[str] = None) -> CheckResult:
    """§ 6.2 rules 1-2, split out of `_validate_extension` so `GET
    /api/poext/eligible` (routers/poext.py) can answer "who can we extend?"
    off the exact same logic the validator runs — two copies of this is
    precisely the kind of drift that made the ledger-vs-bio confusion in § 3.8
    Bird tenure real in the first place."""
    if frame["attestation_contradicted"]:
        return CheckResult(
            check="extension_eligibility", passed=False, level="error",
            message=(
                f"The attested start ({attested_contract_start}) is earlier than a "
                f"real event on file for {player} with a different team — the attestation "
                f"can't be right."
            ),
        )
    if frame["contract_start"] is None or frame["contract_end"] is None:
        return CheckResult(
            check="extension_eligibility", passed=True, level="warning",
            message=(
                f"No acquisition record on file for {player} and no attested start date — "
                f"contract length and position in the deal can't be verified (§ 6.2 rules 1-2). "
                f"A gap here can only make a legal extension look ineligible, never the reverse."
            ),
        )
    length_ok = frame["contract_length"] is not None and frame["contract_length"] >= 3
    is_final_year = cur_season == frame["contract_end"]
    is_year4_of_5 = frame["contract_length"] == 5 and frame["position_in_deal"] == 4
    position_ok = is_final_year or is_year4_of_5
    definite = frame["start_basis"] in ("ledger", "draft")
    if length_ok and position_ok:
        return CheckResult(
            check="extension_eligibility", passed=True,
            message=(
                f"{frame['contract_length']}-year contract ({frame['contract_start']}–"
                f"{frame['contract_end']}), extending from "
                f"{'the final year' if is_final_year else 'Year 4 of 5'} "
                f"(basis: {frame['start_basis']})."
            ),
        )
    reason = []
    if not length_ok:
        reason.append(f"contract length is {frame['contract_length']} years, needs ≥ 3")
    if not position_ok:
        reason.append(
            f"currently Year {frame['position_in_deal']} of {frame['contract_length']}, "
            f"not the final year or Year 4 of a 5-year deal"
        )
    if definite:
        return CheckResult(
            check="extension_eligibility", passed=False, level="error",
            message=f"§ 6.2 rules 1-2: {'; '.join(reason)} (basis: {frame['start_basis']}).",
        )
    # `trade_floor` (or any other non-definite basis): `contract_start` is a
    # LOWER BOUND — the real start can only be the same or earlier, never
    # later, since the acquiring team inherits accrual from before the
    # earliest event on file. A longer real start means a longer real
    # contract_length and a later real position_in_deal than what was just
    # computed, so a *failing* derived check here is not evidence of
    # ineligibility, only of a floor that isn't long enough to prove
    # eligibility either. Must warn-and-allow, matching § 3.8's identical
    # asymmetry for the same basis (nbn-api/CLAUDE.md, "Only over-declaration
    # errors, and only from a definite basis") — this was the actual bug
    # behind Tyler Herro (and 31 other real rostered players, checked
    # 2026-08-21) reading as ineligible off a 2-year floor that was really
    # "at least 2 years, true length unknown."
    return CheckResult(
        check="extension_eligibility", passed=True, level="warning",
        message=(
            f"§ 6.2 rules 1-2: can't confirm — {'; '.join(reason)}, but the start season "
            f"is a lower bound (basis: {frame['start_basis']}), so the real contract could "
            f"be longer and further along than what's visible. A floor here can only make "
            f"a legal extension look ineligible, never the reverse."
        ),
    )


def _check_rookie_scale_terms(contract: ContractIn, scale: dict,
                              bio: dict) -> CheckResult:
    """§ 7.1: a first-rounder's deal is the scale — every year, to the dollar.

    Deliberately **not** run through `_check_contract_raises`: the scale's own
    Year 1 → Year 2 step lands a few hundred dollars either side of exactly 5%
    (pick 1 of 2026 rises 5.0024%), so the § 3.9 ladder rejects roughly half of
    the contracts § 7.1 prescribes. The scale comparison below is both stricter
    and more useful — it names the year and the difference.
    """
    label = f"pick {bio.get('draft_pick')} of {bio.get('draft_year')}"
    problems = []
    for season, expected in scale["salaries"].items():
        got = contract.salaries.get(season)
        if got is None:
            problems.append(f"{season}: missing (scale: {expected})")
        elif _parse_dollar(got) != _parse_dollar(expected):
            problems.append(f"{season}: {got} (scale: {expected})")
    extra = sorted(set(contract.salaries) - set(scale["salaries"]))
    for season in extra:
        problems.append(f"{season}: not a year of the rookie scale")

    wrong_tags = []
    for season, want in scale["cap_holds"].items():
        got = (contract.cap_holds or {}).get(season)
        if got != want:
            wrong_tags.append(f"{season} should be {want}, got {got or 'guaranteed'}")

    if not problems and not wrong_tags:
        return CheckResult(
            check="rookie_scale", passed=True, level="info",
            message=f"Terms match the § 7.1 rookie scale for {label}.",
        )
    detail = "; ".join(problems + wrong_tags)
    return CheckResult(
        check="rookie_scale", passed=False, level="error",
        message=f"§ 7.1 sets this contract for {label} — {detail}.",
    )


def _qo_contract(rec: dict, cur: str) -> ContractIn:
    """The contract a qualifying offer signs: one year at its amount (or a
    one-year two-way), rolling into a UFA hold."""
    nxt = _season_after(cur)
    if rec.get("two_way"):
        # Two-way deals carry "$1" on the trailing hold so the FA pool sees it.
        return ContractIn(type="two-way", salaries={cur: "$0", nxt: "$1"}, cap_holds={nxt: "UFA"})
    return ContractIn(type="player", salaries={cur: f"${rec['amount']:,}"}, cap_holds={nxt: "UFA"})


def _current_two_way_deal(bio: dict) -> Optional[dict]:
    """The two-way deal a player is on now, as ``{salaries, cap_holds}``.

    The last `contracts` entry when there is one. About half the league's
    two-ways were signed before the write path recorded `contracts`, so for
    those the deal is read off the bio's own lines instead: the trailing
    UFA/RFA hold, plus the unbroken run of $0 seasons just before it (a
    two-way pays $0, § 2.2, so the first season with real money is an older
    deal). Two back-to-back two-ways read as one long deal this way; the
    caller's 2-year cap refuses that rather than guessing. None when there
    is nothing to read.
    """
    contracts = bio.get("contracts") or []
    if contracts:
        last = contracts[-1]
        return {"salaries": last.get("salaries") or {}, "cap_holds": last.get("cap_holds") or {}}
    salaries = bio.get("salaries") or {}
    holds = bio.get("cap_holds") or {}
    seasons = sorted(salaries)
    while seasons and holds.get(seasons[-1]) in _FA_HOLD_TYPES:
        seasons.pop()
    trailing = sorted(set(salaries) - set(seasons))
    deal = []
    while seasons and _parse_dollar(salaries[seasons[-1]]) == 0:
        deal.insert(0, seasons.pop())
    if not deal:
        return None
    return {"salaries": {y: salaries[y] for y in deal + trailing},
            "cap_holds": {y: holds[y] for y in deal + trailing if y in holds}}


def _self_convert_twoway_contract(bio: dict, player: str, cap_levels: dict, cur_season: str) -> dict[str, str]:
    """Builds the minimum-scale standard contract a self-serve two-way
    conversion would submit — N years (N = the player's existing two-way
    deal's own length, § 2.2-capped at 2) at the minimum for their tier.
    Raises HTTPException on anything that makes this player ineligible for
    the self-serve path at all. Shared by the endpoint and its own preview,
    so the roster page can show the exact figures that would be submitted
    rather than a client-side re-derivation.

    Each season is priced via `_min_salary_for` with no `years_experience`
    declared on the built contract, so the figure comes from the
    draft-year-inferred tier for *that* season specifically, which climbs
    with the calendar the same way a declared tier climbs per contract year
    (`_contract_years_exp`).
    """
    if bio.get("type") != "two-way":
        raise HTTPException(status_code=422, detail=f"{player!r} is not on a two-way contract.")

    prior = _current_two_way_deal(bio)
    if not prior:
        raise HTTPException(
            status_code=422,
            detail=(f"Can't tell how long {player!r}'s two-way deal is, so there's no length to "
                    "match. Ask the rosters committee to enter the conversion."),
        )
    # A trailing UFA/RFA line is the hold the deal rolls into, not a contract
    # year — a 2-year two-way with its RFA hold has three salary entries.
    prior_holds = prior["cap_holds"]
    prior_length = sum(1 for y in prior["salaries"] if prior_holds.get(y) not in _FA_HOLD_TYPES)
    if prior_length not in (1, 2):
        raise HTTPException(
            status_code=422,
            detail=(f"{player!r}'s two-way deal reads as {prior_length} years — § 2.2 caps a "
                    "two-way at 2, so there's no real length to match. Ask the rosters committee "
                    "to enter the conversion."),
        )

    seasons = [cur_season] if prior_length == 1 else [cur_season, _season_after(cur_season)]
    salaries: dict[str, str] = {}
    for season in seasons:
        expected = _min_salary_for(bio, season, cap_levels, contract=None)
        if expected is None:
            raise HTTPException(
                status_code=422,
                detail=(f"Can't confirm the minimum salary for {season} — {player}'s years of NBA "
                        "experience can't be established (no draft year on file). Ask the rosters "
                        "committee to enter it, or use the PDC pitch-and-bid process (§ 2.2)."),
            )
        salaries[season] = f"${expected:,}"
    return salaries


# § 2.2: the second year of a converted 2-year deal may be fully guaranteed,
# a team option or non-guaranteed. The key is what the owner picks; the value
# is the year's cap_holds tag (None for plain guaranteed money).
SELF_CONVERT_YEAR2_TERMS = {"guaranteed": None, "team_option": "TEAM_OPT", "non_guaranteed": "NON_GTD"}


def _self_convert_twoway_holds(bio: dict, salaries: dict[str, str],
                               year2: str = "guaranteed") -> dict[str, str]:
    """The new deal's cap_holds: the Year 2 term (§ 2.2), plus the trailing
    UFA/RFA hold the converted deal rolls into — the same tag the two-way deal
    ended in, moved to the season after the new deal's last year.
    `_apply_convert_twoway` replaces the bio's `cap_holds` with the new
    contract's, so a deal built without the trailing hold drops it entirely.
    Its amount is left to `_autofill_fa_hold_amounts`, which prices a hold
    coming off a minimum contract at the minimum (§ 3.10)."""
    if year2 not in SELF_CONVERT_YEAR2_TERMS:
        raise HTTPException(status_code=422, detail=f"Unknown Year 2 term {year2!r}.")
    holds: dict[str, str] = {}
    tag = SELF_CONVERT_YEAR2_TERMS[year2]
    if tag:
        if len(salaries) != 2:
            raise HTTPException(
                status_code=422,
                detail="Only the second year of a 2-year conversion can be a team option or non-guaranteed (§ 2.2).",
            )
        holds[max(salaries)] = tag
    prior_holds = (_current_two_way_deal(bio) or {}).get("cap_holds") or {}
    trailing = [t for _, t in sorted(prior_holds.items()) if t in _FA_HOLD_TYPES]
    if trailing:
        holds[_season_after(max(salaries))] = trailing[-1]
    return holds


def _self_convert_twoway_guarantee_dates(cap_holds: dict[str, str]) -> dict[str, str]:
    """A non-guaranteed Year 2 (§ 2.2) becomes guaranteed on January 10 of
    that season at the latest. The team can guarantee it sooner with a
    `guarantee` transaction; a release before the date leaves no dead cap
    for that year (§ 5.1)."""
    return {season: f"20{season[3:]}-01-10" for season, tag in cap_holds.items() if tag == "NON_GTD"}
