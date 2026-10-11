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
