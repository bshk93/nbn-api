"""Run the whole top-level test suite (each module as a subprocess).

    venv/bin/python -m tests.run_all
"""
import subprocess
import sys

MODULES = ["test_stepien_rule", "test_picks_matching", "test_tpe_and_hardcap",
           "test_cap_room_contagion",
           "test_signing_method_funding", "test_two_way_slots",
           "test_two_way_hard_cap",
           "test_exception_absorption_split",
           "test_fa_hold_calc", "test_room_exception_july1",
           "test_bird_rights_tenure", "test_signing_eligibility",
           "test_owner_self_serve", "test_discord_notify",
           "test_offer_sheets", "test_suggestions", "test_fa_pool",
           "test_fa_offers", "test_fa_notify", "test_auth_session",
           "test_contract_shorthand", "test_validate_endpoints",
           "test_roster_log_relay",
           "test_minimum_contract_raises", "test_tradeblock_notify",
           "test_waivers", "test_sign_requires_salary",
           "test_one_year_min_cap_hit_consistency",
           "test_inbox", "test_inbox_wiring", "test_cleanup",
           "test_stats_harness", "test_stats_writer",
           "test_stats_pipeline", "test_stats_cutover",
           "test_allstats_guard", "test_stats_integrity",
           "test_boxscore_provenance", "test_drive_backup",
           "test_data_paths"]


def main():
    failed = []
    for name in MODULES:
        print(f"\n===== {name} =====")
        rc = subprocess.call([sys.executable, "-m", f"tests.{name}"])
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
