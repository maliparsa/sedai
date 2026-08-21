#!/usr/bin/env python3
"""Run all offline test suites and report combined results."""

import subprocess
import sys

test_suites = [
    "test_setup.py",
    "test_settings.py",
    "test_ui.py",
    "test_integration.py",
    "test_help.py",
    "test_styles.py",
    "test_input_flow.py",
    "test_images.py",
]

def main():
    print("Running all offline test suites...\n")
    all_passed = True

    for suite in test_suites:
        print(f"\n{'=' * 60}")
        print(f"Running {suite}...")
        print(f"{'=' * 60}")
        result = subprocess.run([sys.executable, suite], cwd=__import__('os').path.dirname(__file__))
        if result.returncode != 0:
            all_passed = False

    print(f"\n{'=' * 60}")
    if all_passed:
        print("All test suites passed!")
        return 0
    else:
        print("Some test suites failed.")
        return 1

if __name__ == "__main__":
    sys.exit(main())
