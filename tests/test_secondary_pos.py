"""Rulebook § 2.3: a player's primary is 2K's, frozen until he changes teams;
a secondary is one step from it or any position 2K has listed for him. A
team may still save one outside that; it is flagged for the streamer."""
import pytest
from fastapi import HTTPException

from routers import players


def test_options_are_the_neighbours_of_the_primary():
    assert players.secondary_pos_options("PG") == ["SG"]
    assert players.secondary_pos_options("SG") == ["PG", "SF"]
    assert players.secondary_pos_options("SF") == ["SG", "PF"]
    assert players.secondary_pos_options("PF") == ["SF", "C"]
    assert players.secondary_pos_options("C") == ["PF"]
    assert players.secondary_pos_options(None) == []


@pytest.fixture
def attrs(monkeypatch):
    data = {
        "wing": [{"2k_pos": ["PG"]}, {"2k_pos": ["SF", "PF"]}],  # latest snapshot wins
        "unscraped": [],
    }
    monkeypatch.setattr(players, "load_attributes", lambda: data)
    monkeypatch.setattr(players, "load_player_bios", lambda: {})


@pytest.mark.parametrize("sec", ["SG", "PF", "PG", "C", None, ""])
def test_any_other_position_or_blank_is_allowed(attrs, sec):
    players._check_secondary_pos("wing", sec)


@pytest.mark.parametrize("sec", ["SF", "G", "XX"])
def test_primary_itself_or_not_a_position_is_refused(attrs, sec):
    with pytest.raises(HTTPException) as e:
        players._check_secondary_pos("wing", sec)
    assert e.value.status_code == 422


def test_no_primary_means_no_secondary(attrs):
    with pytest.raises(HTTPException):
        players._check_secondary_pos("unscraped", "SG")
    players._check_secondary_pos("unscraped", None)


# ── The pool: one step, or anything 2K ever listed ──────────────────────────

@pytest.fixture
def world(monkeypatch):
    data = {
        # 2K had him at SG/SF, then re-rated him a PG.
        "guard": [{"2k_pos": ["SG", "SF"]}, {"2k_pos": ["PG"]}],
        "wing": [{"2k_pos": ["SF", "PF"]}],
        "odd": [{"2k_pos": ["PF", "PG"]}],   # 2K's own, two steps away
        "solo": [{"2k_pos": ["C"]}],
    }
    monkeypatch.setattr(players, "load_attributes", lambda: data)
    monkeypatch.setattr(players, "load_player_bios", lambda: {})
    return data


def frozen(primary, secondary=None, team="BOS", reason="initial", **extra):
    return {"position": {"primary": primary, "secondary": secondary, "team": team,
                         "set": "2026-10-24", "reason": reason}, **extra}


def test_options_are_one_step():
    assert players.secondary_pos_options("SF") == ["SG", "PF"]


def test_pool_adds_every_position_2k_has_listed(world):
    bio = frozen("SG", "SF")
    # One step from SG is PG/SF; 2K has also listed PG, SG, SF. Never SG itself.
    assert players.secondary_pool("guard", bio) == ["PG", "SF"]
    bio = frozen("PF")
    assert players.secondary_pool("odd", bio) == ["PG", "SF", "C"]


def test_legal_is_in_the_pool(world):
    bio = frozen("PF", "PG")
    assert players.secondary_is_legal("odd", bio, "PG")
    assert players.secondary_is_legal("odd", bio, "C")
    assert not players.secondary_is_legal("odd", bio, "SG")
    assert players.secondary_is_legal("odd", bio, None)
    assert players.secondary_is_legal("odd", bio, "none")


def test_frozen_primary_ignores_2k_today(world):
    # 2K now says PG; he was frozen at SG.
    assert players._primary_pos("guard", None, frozen("SG", "SF")) == "SG"
    # Nothing frozen yet: 2K's today.
    assert players._primary_pos("guard", None, {}) == "PG"


def test_unset_means_the_frozen_default(world):
    assert players.effective_secondary("guard", frozen("SG", "SF")) == "SF"
    assert players.effective_secondary("guard", frozen("SG", None)) is None
    # Nothing frozen: 2K's secondary, even two steps away (2K listed it).
    assert players.effective_secondary("odd", {}) == "PG"
    assert players.effective_secondary("solo", {}) is None


def test_teams_pick_wins_and_none_removes(world):
    assert players.effective_secondary("wing", frozen("SF", "PF", secondary_pos="SG")) == "SG"
    assert players.effective_secondary("wing", frozen("SF", "PF", secondary_pos="none")) is None


def test_eligible_is_primary_then_secondary(world):
    assert players.eligible_positions("wing", frozen("SF", "PF")) == ["SF", "PF"]
    assert players.eligible_positions("wing", frozen("SF", "PF", secondary_pos="none")) == ["SF"]


# ── Freezing ─────────────────────────────────────────────────────────────────

def test_first_run_freezes_from_2k_today(world):
    bios = {"guard": {"pos": ["SG"]}, "wing": {}}
    changed = players.reconcile_positions(bios, {"guard": "BOS"}, world, "2026-10-24")
    assert sorted(changed) == ["guard", "wing"]
    assert bios["guard"]["position"] == {"primary": "PG", "secondary": None, "team": "BOS",
                                         "set": "2026-10-24", "reason": "initial"}
    assert bios["guard"]["pos"] == ["PG"]
    assert bios["wing"]["position"]["team"] is None
    assert bios["wing"]["pos"] == ["SF", "PF"]


def test_a_2k_change_does_not_move_him(world):
    bios = {"guard": frozen("SG", "SF")}
    players.reconcile_positions(bios, {"guard": "BOS"}, world, "2026-10-25")
    assert bios["guard"]["position"]["primary"] == "SG"
    assert bios["guard"]["pos"] == ["SG", "SF"]


def test_a_team_change_refreezes_and_drops_the_old_pick(world):
    bios = {"guard": frozen("SG", "SF", secondary_pos="PG")}
    changed = players.reconcile_positions(bios, {"guard": "NYK"}, world, "2026-10-25")
    assert changed == ["guard"]
    rec = bios["guard"]["position"]
    assert (rec["primary"], rec["secondary"], rec["team"], rec["reason"]) == ("PG", None, "NYK", "team change")
    assert bios["guard"]["secondary_pos"] is None
    assert bios["guard"]["pos"] == ["PG"]


def test_free_agency_is_not_a_change(world):
    bios = {"guard": frozen("SG", "SF", secondary_pos="PG")}
    # Released: on no roster. Nothing moves.
    assert players.reconcile_positions(bios, {}, world, "2026-10-25") == ["guard"]  # pos synced only
    assert bios["guard"]["position"]["primary"] == "SG"
    # Re-signed by the same team: still nothing.
    players.reconcile_positions(bios, {"guard": "BOS"}, world, "2026-10-26")
    assert bios["guard"]["position"]["primary"] == "SG"
    assert bios["guard"]["secondary_pos"] == "PG"


def test_frozen_with_no_team_then_signed_refreezes(world):
    bios = {"guard": frozen("SG", "SF", team=None)}
    players.reconcile_positions(bios, {"guard": "MIA"}, world, "2026-10-25")
    assert bios["guard"]["position"]["primary"] == "PG"
    assert bios["guard"]["position"]["team"] == "MIA"


def test_unrated_player_seeds_from_bio_then_2k_takes_over(world):
    bios = {"rookie": {"pos": ["SF", "SG"]}}
    players.reconcile_positions(bios, {"rookie": "BOS"}, world, "2026-10-24")
    assert bios["rookie"]["position"]["reason"] == "bio"
    assert bios["rookie"]["position"]["primary"] == "SF"
    world["rookie"] = [{"2k_pos": ["SG", "PG"]}]
    players.reconcile_positions(bios, {"rookie": "BOS"}, world, "2026-10-30")
    assert bios["rookie"]["position"]["primary"] == "SG"
    assert bios["rookie"]["position"]["reason"] == "initial"


def test_nothing_to_do_changes_nothing(world):
    bios = {"wing": frozen("SF", "PF", pos=["SF", "PF"])}
    assert players.reconcile_positions(bios, {"wing": "BOS"}, world, "2026-10-25") == []


# ── The streamer's checklist ─────────────────────────────────────────────────

def test_streamer_has_nothing_to_do_when_2k_already_has_it(world):
    from routers import coaching_settings as cs
    bios = {"wing": frozen("SF", "PF", secondary_pos="PF"), "solo": frozen("C", secondary_pos="none")}
    assert cs.position_items("MIL", {}, bios, ["wing", "solo"], "2026-10-20", world) == []


def test_streamer_sees_a_change_from_2ks(world):
    from routers import coaching_settings as cs
    bios = {"wing": frozen("SF", "PF", secondary_pos="SG"), "odd": frozen("PF", "PG", secondary_pos="none")}
    items = {i["slug"]: i for i in cs.position_items("MIL", {}, bios, ["wing", "odd"], "2026-10-20", world)}
    assert (items["wing"]["was"], items["wing"]["secondary"]) == ("PF", "SG")
    assert (items["odd"]["was"], items["odd"]["secondary"]) == ("PG", None)
    # Once entered, what the streamer set is the baseline, not 2K's.
    rec = {"entered": {"wing": "SG", "odd": None}}
    assert cs.position_items("MIL", rec, bios, ["wing", "odd"], "2026-10-20", world) == []


def test_streamer_sets_a_primary_2k_moved(world):
    from routers import coaching_settings as cs
    bios = {"guard": frozen("SG", None)}
    items = cs.position_items("MIL", {}, bios, ["guard"], "2026-10-20", world)
    assert [(i["primary_was"], i["primary"], i["primary_changed"]) for i in items] == [("PG", "SG", True)]
    rec = {"entered": {"guard": None}, "entered_primary": {"guard": "SG"}, "entered_2k": {"guard": ["PG"]}}
    assert cs.position_items("MIL", rec, bios, ["guard"], "2026-10-20", world) == []


def test_a_2k_rerating_resets_what_was_entered(world):
    from routers import coaching_settings as cs
    bios = {"guard": frozen("SG", None)}
    # Entered when 2K still had him at SG/SF; 2K has re-rated him since.
    rec = {"entered": {"guard": None}, "entered_primary": {"guard": "SG"}, "entered_2k": {"guard": ["SG", "SF"]}}
    items = cs.position_items("MIL", rec, bios, ["guard"], "2026-10-20", world)
    assert [(i["primary_was"], i["primary_changed"]) for i in items] == [("PG", True)]


def test_a_2k_secondary_two_steps_away_counts_for_starting(world):
    from routers import coaching_settings as cs
    bio = frozen("PF", "PG")
    assert "PG" in cs.starter_positions("PF", cs.counted_secondary("odd", bio, world, {}))


def test_parallel_team_settings_saves_all_land(monkeypatch):
    # The team page sends every changed player at once. Each save must keep
    # the others: a load-modify-save without the lock let only the last one
    # survive (2026-10-21, "none" picks that wouldn't stick).
    import copy, threading, time
    from routers import coaching_settings
    slugs = [f"p{i}" for i in range(8)]
    disk = {"bios": {s: {"secondary_pos": None} for s in slugs}}

    def load():
        data = copy.deepcopy(disk["bios"])
        time.sleep(0.01)  # widen the window a racing save would hit
        return data

    monkeypatch.setattr(players, "load_player_bios", load)
    monkeypatch.setattr(players, "save_player_bios", lambda d: disk.update(bios=copy.deepcopy(d)))
    monkeypatch.setattr(players, "_build_team_map", lambda: {s: "MIL" for s in slugs})
    monkeypatch.setattr(players, "_check_secondary_pos", lambda slug, sec, bio=None: None)
    monkeypatch.setattr(players, "reconcile_positions_now", lambda force=False: [])
    monkeypatch.setattr(players, "log_write", lambda *a, **k: None)
    monkeypatch.setattr(coaching_settings, "has_position_changes", lambda team: True)

    info = {"roles": ["mil"], "name": "owner"}
    threads = [threading.Thread(target=players.update_team_settings,
                                args=(s, players.TeamSettingsUpdate(secondary_pos="none"), info))
               for s in slugs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert {s: b["secondary_pos"] for s, b in disk["bios"].items()} == {s: "none" for s in slugs}


@pytest.mark.parametrize("coaching_waiting,notified", [(False, True), (True, False)])
def test_one_message_when_coaching_is_already_waiting(monkeypatch, coaching_waiting, notified):
    # The streamer enters positions and coaching from one card, so a team
    # that already has coaching settings waiting has already pinged them.
    from routers import coaching_settings, game_day_notify
    disk = {"bios": {"p0": {"secondary_pos": None}}}
    monkeypatch.setattr(players, "load_player_bios", lambda: disk["bios"])
    monkeypatch.setattr(players, "save_player_bios", lambda d: disk.update(bios=d))
    monkeypatch.setattr(players, "_build_team_map", lambda: {"p0": "MIL"})
    monkeypatch.setattr(players, "_check_secondary_pos", lambda slug, sec, bio=None: None)
    monkeypatch.setattr(players, "reconcile_positions_now", lambda force=False: [])
    monkeypatch.setattr(players, "log_write", lambda *a, **k: None)
    calls = iter([False, True])  # nothing waiting before the save, a change after
    monkeypatch.setattr(coaching_settings, "has_position_changes", lambda team: next(calls))
    monkeypatch.setattr(coaching_settings, "coaching_pending", lambda team: coaching_waiting)
    monkeypatch.setattr(coaching_settings, "note_open", lambda team: False)
    sent = []
    monkeypatch.setattr(game_day_notify, "positions_changed", lambda team, by: sent.append(team))
    players.update_team_settings("p0", players.TeamSettingsUpdate(secondary_pos="SG"),
                                 {"roles": ["mil"], "name": "owner"})
    assert bool(sent) == notified


def test_streamer_sees_a_jersey_change_only_once_recorded(world):
    # No scrape of 2K's numbers, so a number with no record is not a change.
    from routers import coaching_settings as cs
    bios = {"solo": {"jersey_number": "7"}}
    assert cs.position_items("MIL", {}, bios, ["solo"], "2026-10-20") == []
    rec = {"jerseys": {"solo": "3"}}
    [item] = cs.position_items("MIL", rec, bios, ["solo"], "2026-10-20")
    assert (item["jersey_was"], item["jersey"], item["jersey_changed"], item["due"]) == ("3", "7", True, True)
    assert item["changed"] is False
    # Entered: 2K has what the team picked.
    assert cs.position_items("MIL", {"jerseys": {"solo": "7"}}, bios, ["solo"], "2026-10-20") == []
    # A cleared number isn't: 2K always shows one, so there is nothing to enter.
    assert cs.position_items("MIL", rec, {"solo": {}}, ["solo"], "2026-10-20") == []


def test_a_jersey_save_records_what_2k_had(monkeypatch):
    from routers import coaching_settings, game_day_notify
    disk = {"bios": {"p0": {"jersey_number": "3"}}, "checks": {}}
    monkeypatch.setattr(players, "load_player_bios", lambda: disk["bios"])
    monkeypatch.setattr(players, "save_player_bios", lambda d: disk.update(bios=d))
    monkeypatch.setattr(players, "_build_team_map", lambda: {"p0": "MIL"})
    monkeypatch.setattr(players, "_check_secondary_pos", lambda slug, sec, bio=None: None)
    monkeypatch.setattr(players, "reconcile_positions_now", lambda force=False: [])
    monkeypatch.setattr(players, "log_write", lambda *a, **k: None)
    monkeypatch.setattr(coaching_settings, "load_position_checks", lambda: disk["checks"])
    monkeypatch.setattr(coaching_settings, "save_position_checks", lambda d: disk.update(checks=d))
    monkeypatch.setattr(coaching_settings, "coaching_pending", lambda team: False)
    monkeypatch.setattr(coaching_settings, "note_open", lambda team: False)
    monkeypatch.setattr(coaching_settings, "_roster_slugs", lambda team: ["p0"])
    monkeypatch.setattr(coaching_settings, "position_items",
                        lambda team, rec, bios, roster, today, attrs=None: [{"changed": False, "primary_changed": False, "jersey_changed": bool(rec.get("jerseys"))}])
    sent = []
    monkeypatch.setattr(game_day_notify, "positions_changed", lambda team, by: sent.append(team))
    info = {"roles": ["mil"], "name": "owner"}
    players.update_team_settings("p0", players.TeamSettingsUpdate(jersey_number="7"), info)
    assert disk["checks"]["MIL"]["jerseys"] == {"p0": "3"}
    assert sent == ["MIL"]
    # A second change before it's entered: 2K still has the first number.
    players.update_team_settings("p0", players.TeamSettingsUpdate(jersey_number="9"), info)
    assert disk["checks"]["MIL"]["jerseys"] == {"p0": "3"}
    assert sent == ["MIL"]


def test_reconcile_now_skips_when_no_roster_moved(monkeypatch, world):
    # It runs after every API write, so it must cost nothing when no roster
    # file changed, and take the lock and save only when one did.
    disk = {"bios": {"wing": {}}, "loads": 0, "saves": 0}

    def load():
        disk["loads"] += 1
        return disk["bios"]

    stamp = {"v": (1,)}
    monkeypatch.setattr(players, "_roster_stamp", lambda: stamp["v"])
    monkeypatch.setattr(players, "_roster_mtimes", None)
    monkeypatch.setattr(players, "load_player_bios", load)
    monkeypatch.setattr(players, "save_player_bios", lambda d: disk.update(saves=disk["saves"] + 1))
    monkeypatch.setattr(players, "_build_team_map", lambda: {"wing": "BOS"})
    assert players.reconcile_positions_now() == ["wing"]
    assert players.reconcile_positions_now() == []
    assert (disk["loads"], disk["saves"]) == (1, 1)
    stamp["v"] = (2,)   # a roster moved; nothing new for him, so no save
    assert players.reconcile_positions_now() == []
    assert (disk["loads"], disk["saves"]) == (2, 1)
