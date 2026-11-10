"""Pin years of service to `season - draft_year` for tests whose fixture
players have a draft year and no box scores.

Those tests are about pricing (a tier, a cap hit, a hold), not about counting
seasons, and were written when experience *was* the draft-year count. Live
service data would read their made-up slugs as never rostered. The count
itself is tested in `test_years_of_service`.
"""
import routers.years_of_service as yos


def draft_year_count(bio, season, slug=None):
    dy = (bio or {}).get("draft_year")
    return max(0, 2000 + int(season[:2]) - int(dy)) if dy else None


def pin():
    yos.years_of_service = draft_year_count
