"""Run the whole top-level test suite (each module as a subprocess).

    venv/bin/python -m tests.run_all
"""
import subprocess
import sys

MODULES = ["test_stepien_rule", "test_picks_matching", "test_tpe_and_hardcap",
           "test_signing_method_funding"]


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
