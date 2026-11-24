"""Run the whole top-level test suite (each module as a subprocess).

    venv/bin/python -m tests.run_all
"""
import subprocess
import sys

MODULES = [
    "test_route_bindings", "test_league_time_dates", "test_stepien_rule",
    "test_pick_advance_limit", "test_picks_matching", "test_tpe_and_hardcap",
    "test_cap_room_contagion", "test_signing_method_funding", "test_two_way_slots",
    "test_two_way_terms", "test_two_way_hard_cap", "test_convert_twoway_minimum",
    "test_exception_absorption_split", "test_fa_hold_calc",
    "test_bird_rights_endpoint", "test_signing_eligibility", "test_reacquisition", "test_owner_self_serve",
    "test_stash", "test_discord_notify", "test_offer_sheets", "test_suggestions",
    "test_fa_pool", "test_fa_offers", "test_fa_notify", "test_auth_session",
    "test_trade_requests", "test_pick_protection_trades", "test_apply_trade_warnings",
    "test_season_start_rules", "test_restructure_picks", "test_poext",
    "test_poext_notify", "test_contract_shorthand", "test_rulebook_coverage",
    "test_themes", "test_roster_log_relay", "test_minimum_contract_trade_exception",
    "test_minimum_contract_raises", "test_tradeblock_notify", "test_tradeblock_nudge",
    "test_roster_move_notify", "test_waivers", "test_one_year_min_cap_hit_consistency",
    "test_inbox", "test_inbox_wiring", "test_discord_lineups", "test_schedule",
    "test_stream_sim", "test_coaching_settings", "test_coaching_history",
    "test_historical_convert", "test_minimum_proration_apply", "test_ratings_updated", "test_ratings_special",
    "test_ratings_changes_cache", "test_roster_entry", "test_dna_position_check",
    "test_irl", "test_streaming_days", "test_stream_planner", "test_youtube_vods",
    "test_donations", "test_news_rankings", "test_tips", "test_nbyen", "test_markets",
    "test_news_credited", "test_lore", "test_og", "test_names", "test_tonight",
    "test_stream_live", "test_stream_notify", "test_audit_log", "test_cap_history",
    "test_poopoo_summary", "test_stats_writer", "test_seed_order", "test_season_clock",
    "test_allstats_guard", "test_allstats_files", "test_stats_integrity",
    "test_stats_checks", "test_allstats_edit", "test_boxscore_provenance",
    "test_boxscore_upload_format", "test_parse_request", "test_boxscore_player_slugs",
    "test_team_season_lines", "test_home_team", "test_shared_json", "test_member_seen",
    "test_api_compat", "test_gamelog_index", "test_etag", "test_boxscore_commit_gate",
    "test_boxscore_screenshots", "test_game_day_notify", "test_game_highs",
    "test_game_starters", "test_sixth_man", "test_on_this_day", "test_player_insights",
    "test_players_slugs_filter", "test_playoff_odds", "test_fair_value",
    "test_drive_backup", "test_declared_experience", "test_years_of_service",
    "test_two_way_release", "test_trade_releases", "test_signing_releases",
    "test_apply_extension", "test_extension_trade_freeze", "test_nbnfl",
    "test_signing_books", "test_split_players", "test_trc_discord", "test_injuries",
    "test_tetris", "test_quadrants", "test_headshots", "test_tweets", "test_js_mirrors", "test_tradeblock_names", "test_private_writes", "test_ratings_changes_race", "test_txn_package", "test_create_player_lock", "test_migrate_dead_type", "test_arenas",
    "test_box_health", "test_fa_hold_sweep", "test_game_day", "test_invest",
    "test_nbntv_schedule", "test_pick_swaps", "test_pickem", "test_picks_structure", "test_picks_resolve",
    "test_team_option_sweep"]

# Pytest-style modules (fixtures, parametrize), run through pytest.
PYTEST_MODULES = ["test_secondary_pos"]

# Modules that read the server's real data directory (DATA_DIR in
# routers/constants.py). They run on the box, where that directory exists,
# and are skipped -- loudly, by name -- anywhere it doesn't (CI, a laptop).
# A test belongs here only if it genuinely checks live data; one that merely
# forgot to isolate itself should be fixed to use a temp dir instead.
LIVE_DATA_MODULES = [
    "test_room_exception_july1", "test_bird_rights_tenure", "test_validate_endpoints",
    "test_second_round_scale", "test_sign_requires_salary",
    "test_news_rankings_routes", "test_health", "test_stats_harness",
    "test_stats_cutover", "test_extensions", "test_qualifying_offers",
    "test_stats_pipeline", "test_data_paths", "test_advanced", "test_finances",
    "test_player_value", "test_boxscore_grouping"]

# The picks_conveyance package keeps its tests beside it. They were never in
# this list, which is how test_curated sat broken for two months (2026-09-30).
PICKS_MODULES = ["picks_conveyance.tests." + n for n in (
    "test_curated", "test_from_trade", "test_ladders",
    "test_parity", "test_resolver", "test_retrade",
    "test_validation_hardening")]
PICKS_LIVE_DATA_MODULES = ["picks_conveyance.tests." + n for n in (
    "test_ownership", "test_projection_full", "test_projection_parity",
    "test_registry", "test_resync")]


from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def unlisted() -> list[str]:
    """Every tests/test_*.py must be in one of the lists above. Fifteen sat
    outside MODULES for weeks and never ran (found 2026-11-16)."""
    listed = set(MODULES) | set(PYTEST_MODULES) | set(LIVE_DATA_MODULES)
    on_disk = {p.stem for p in (ROOT / "tests").glob("test_*.py")}
    return sorted(on_disk - listed)


def live_data_present() -> bool:
    sys.path.insert(0, str(ROOT))
    from routers.constants import DATA_DIR
    return (DATA_DIR / "player-bios.json").exists()


def main():
    failed = []
    missing = unlisted()
    if missing:
        print(f"UNLISTED TEST MODULES (add each to a list in tests/run_all.py): {missing}")
        failed += missing
    live = live_data_present()
    runs = [(n, [sys.executable, "-m", n if "." in n else f"tests.{n}"]) for n in MODULES + PICKS_MODULES]
    runs += [(n, [sys.executable, "-m", "pytest", "-q", f"tests/{n}.py"]) for n in PYTEST_MODULES]
    skipped = []
    if live:
        runs += [(n, [sys.executable, "-m", n if "." in n else f"tests.{n}"])
                 for n in LIVE_DATA_MODULES + PICKS_LIVE_DATA_MODULES]
    else:
        skipped = LIVE_DATA_MODULES + PICKS_LIVE_DATA_MODULES
    for name, cmd in runs:
        print(f"\n===== {name} =====")
        if subprocess.call(cmd, cwd=ROOT):
            failed.append(name)
    print("\n" + ("=" * 40))
    if skipped:
        print(f"SKIPPED {len(skipped)} live-data modules (no data dir here): {skipped}")
    if failed:
        print(f"SUITE FAILED: {failed}")
        return 1
    print(f"ALL SUITES PASS ({len(runs)} modules)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
