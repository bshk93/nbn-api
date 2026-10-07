"""GET /api/players?slugs= returns only the bios asked for."""
from unittest.mock import patch

from routers import players

BIOS = {
    "curry-stephen": {"name": "CURRY, STEPHEN"},
    "conley-mike": {"name": "CONLEY, MIKE"},
    "paul-chris": {"name": "PAUL, CHRIS"},
}


def _get(slugs):
    with patch.object(players, "load_player_bios", return_value=dict(BIOS)):
        return players.get_players(slugs=slugs)


def test_no_filter_returns_everything():
    assert _get(None) == BIOS


def test_filter_returns_only_named():
    assert _get("curry-stephen, paul-chris") == {
        "curry-stephen": BIOS["curry-stephen"],
        "paul-chris": BIOS["paul-chris"],
    }


def test_unknown_and_blank_slugs_are_skipped():
    assert _get("nobody,,conley-mike,") == {"conley-mike": BIOS["conley-mike"]}


def test_empty_filter_returns_nothing():
    assert _get("") == {}
