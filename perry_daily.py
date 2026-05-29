#!/home/skim/projects/nbn-api/venv/bin/python3
"""Daily Perry Game reset: awards prizes for outgoing day, posts Discord, generates new puzzle."""
import sys
sys.path.insert(0, "/home/skim/projects/nbn-api")

from routers.perry import (
    _load_perry, _save_perry, _generate_puzzle, _award_prizes,
    _archive_state, _discord_daily_results, _today_et,
)

def main():
    old = _load_perry()
    if old and old.get("date"):
        _archive_state(old)
        if old.get("entries"):
            _award_prizes(old)
        _discord_daily_results(old)

    today = _today_et()
    new_state = _generate_puzzle(today)
    _save_perry(new_state)
    print(f"Perry reset for {today}: teams={new_state['teams']}, solution_score={new_state['solution_score']}")

if __name__ == "__main__":
    main()
