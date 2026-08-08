"""Run the whole top-level test suite (each module as a subprocess).

    venv/bin/python -m tests.run_all
"""
import subprocess
import sys

MODULES = ["test_stepien_rule", "test_picks_matching", "test_tpe_and_hardcap",
           "test_signing_method_funding", "test_exception_absorption_split",
           "test_fa_hold_calc", "test_room_exception_july1",
           "test_bird_rights_tenure", "test_signing_eligibility",
           "test_owner_self_serve", "test_discord_notify",
           "test_offer_sheets", "test_suggestions", "test_fa_pool",
           "test_fa_offers"]


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
