"""Run the whole top-level test suite (each module as a subprocess).

    venv/bin/python -m tests.run_all
"""
import subprocess
import sys

MODULES = ["test_route_bindings",
           "test_stepien_rule", "test_pick_advance_limit", "test_picks_matching", "test_tpe_and_hardcap",
           "test_cap_room_contagion",
           "test_signing_method_funding", "test_two_way_slots", "test_two_way_terms",
           "test_two_way_hard_cap", "test_convert_twoway_minimum",
           "test_exception_absorption_split",
           "test_fa_hold_calc", "test_room_exception_july1",
           "test_bird_rights_tenure", "test_signing_eligibility",
           "test_owner_self_serve", "test_stash", "test_discord_notify",
           "test_offer_sheets", "test_suggestions", "test_fa_pool",
           "test_fa_offers", "test_fa_notify", "test_auth_session",
           "test_trade_requests", "test_apply_trade_warnings", "test_season_start_rules", "test_restructure_picks",
           "test_poext", "test_poext_notify",
           "test_contract_shorthand", "test_validate_endpoints",
           "test_rulebook_coverage",
           "test_themes",
           "test_roster_log_relay",
           "test_minimum_contract_trade_exception",
           "test_minimum_contract_raises", "test_second_round_scale",
           "test_tradeblock_notify", "test_roster_move_notify",
           "test_waivers", "test_sign_requires_salary",
           "test_one_year_min_cap_hit_consistency",
           "test_inbox", "test_inbox_wiring", "test_schedule",
           "test_coaching_settings", "test_irl", "test_streaming_days", "test_donations",
           "test_news_rankings", "test_news_rankings_routes", "test_tips", "test_nbyen", "test_markets",
           "test_news_credited",
           "test_og", "test_health", "test_audit_log", "test_cap_history",
           "test_poopoo_summary",
           "test_stats_harness", "test_stats_writer",
           "test_stats_pipeline", "test_stats_cutover", "test_season_clock",
           "test_allstats_guard", "test_allstats_files", "test_stats_integrity",
           "test_stats_checks", "test_allstats_edit",
           "test_boxscore_provenance", "test_boxscore_upload_format",
           "test_boxscore_player_slugs", "test_boxscore_commit_gate",
           "test_boxscore_screenshots", "test_game_day_notify", "test_game_highs",
           "test_player_insights", "test_players_slugs_filter",
           "test_drive_backup",
           "test_data_paths"]

# The picks_conveyance package keeps its tests beside it. They were never in
# this list, which is how test_curated sat broken for two months (2026-09-30).
PICKS_MODULES = ["picks_conveyance.tests." + n for n in (
    "test_curated", "test_from_trade", "test_ladders", "test_ownership",
    "test_parity", "test_projection_full", "test_projection_parity",
    "test_registry", "test_resolver", "test_resync", "test_retrade",
    "test_validation_hardening")]


def main():
    failed = []
    for name in MODULES + PICKS_MODULES:
        print(f"\n===== {name} =====")
        module = name if "." in name else f"tests.{name}"
        rc = subprocess.call([sys.executable, "-m", module])
        if rc:
            failed.append(name)
    print("\n" + ("=" * 40))
    if failed:
        print(f"SUITE FAILED: {failed}")
        return 1
    print("ALL SUITES PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
