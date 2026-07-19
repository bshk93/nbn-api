"""Run the whole picks_conveyance test suite (each module as a subprocess).

    venv/bin/python -m picks_conveyance.tests.run_all
"""
import subprocess
import sys

MODULES = ["test_projection_parity", "test_resolver", "test_ladders",
           "test_curated", "test_projection_full"]


def main():
    failed = []
    for name in MODULES:
        print(f"\n===== {name} =====")
        rc = subprocess.call([sys.executable, "-m",
                              f"picks_conveyance.tests.{name}"])
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
