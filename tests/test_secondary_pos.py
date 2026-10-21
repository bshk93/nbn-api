"""Rulebook § 2.3: a secondary position is one step from the 2K primary."""
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


@pytest.mark.parametrize("sec", ["SG", "PF", None, ""])
def test_neighbour_or_blank_is_allowed(attrs, sec):
    players._check_secondary_pos("wing", sec)


@pytest.mark.parametrize("sec", ["SF", "PG", "C"])
def test_primary_itself_or_two_steps_away_is_refused(attrs, sec):
    with pytest.raises(HTTPException) as e:
        players._check_secondary_pos("wing", sec)
    assert e.value.status_code == 422
    assert "SG or PF" in e.value.detail


def test_no_2k_primary_means_no_secondary(attrs):
    with pytest.raises(HTTPException):
        players._check_secondary_pos("unscraped", "SG")
    players._check_secondary_pos("unscraped", None)


# The secondary a player actually has: his team's pick, or 2K's when the team
# hasn't chosen. "none" removes 2K's.
@pytest.fixture
def twok(monkeypatch):
    data = {
        "wing": [{"2k_pos": ["SF", "PF"]}],
        "odd": [{"2k_pos": ["PF", "PG"]}],   # 2K's own, not one step: not allowed
        "solo": [{"2k_pos": ["C"]}],
    }
    monkeypatch.setattr(players, "load_attributes", lambda: data)


def test_unset_means_2ks_secondary(twok):
    assert players.effective_secondary("wing", {}) == "PF"
    assert players.effective_secondary("wing", {"secondary_pos": None}) == "PF"
    assert players.effective_secondary("odd", {}) is None
    assert players.effective_secondary("solo", {}) is None


def test_teams_pick_wins_and_none_removes(twok):
    assert players.effective_secondary("wing", {"secondary_pos": "SG"}) == "SG"
    assert players.effective_secondary("wing", {"secondary_pos": "none"}) is None


def test_none_is_a_valid_choice(twok):
    players._check_secondary_pos("wing", "none")


def test_streamer_has_nothing_to_do_when_2k_already_has_it(twok):
    from routers import coaching_settings as cs
    bios = {"wing": {"secondary_pos": "PF"}, "solo": {"secondary_pos": "none"}}
    assert cs.position_items("MIL", {}, bios, ["wing", "solo"], "2026-10-20") == []


def test_streamer_removes_a_2k_secondary_that_isnt_one_step(twok):
    from routers import coaching_settings as cs
    items = cs.position_items("MIL", {}, {"odd": {}}, ["odd"], "2026-10-20")
    assert [(i["was"], i["secondary"]) for i in items] == [("PG", None)]


def test_streamer_sees_a_change_from_2ks(twok):
    from routers import coaching_settings as cs
    bios = {"wing": {"secondary_pos": "SG"}, "odd": {"secondary_pos": "none"}}
    items = {i["slug"]: i for i in cs.position_items("MIL", {}, bios, ["wing", "odd"], "2026-10-20")}
    assert (items["wing"]["was"], items["wing"]["secondary"]) == ("PF", "SG")
    assert (items["odd"]["was"], items["odd"]["secondary"]) == ("PG", None)
    # Once entered, what the streamer set is the baseline, not 2K's.
    rec = {"entered": {"wing": "SG", "odd": None}}
    assert cs.position_items("MIL", rec, bios, ["wing", "odd"], "2026-10-20") == []


def test_a_2k_secondary_that_isnt_one_step_doesnt_count_for_starting(twok):
    from routers import coaching_settings as cs
    assert "PG" not in cs.starter_positions("PF", players.effective_secondary("odd", {}))


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
    monkeypatch.setattr(players, "_check_secondary_pos", lambda slug, sec: None)
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
