"""GET /api/players?slugs= returns only the bios asked for."""
import json
from unittest.mock import patch

from routers import players, storage

BIOS = {
    "curry-stephen": {"name": "CURRY, STEPHEN"},
    "conley-mike": {"name": "CONLEY, MIKE"},
    "paul-chris": {"name": "PAUL, CHRIS"},
}


def _get(slugs):
    # The whole-file answer is a cached, pre-encoded Response; clear the cache
    # so the mocked bios are what gets encoded, and decode it back.
    storage._SHARED_RESPONSES.clear()
    with patch.object(players, "player_bios_shared", return_value=dict(BIOS)):
        r = players.get_players(slugs=slugs)
    storage._SHARED_RESPONSES.clear()
    return json.loads(r.body) if hasattr(r, "body") else r


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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  [ok] {name}")
