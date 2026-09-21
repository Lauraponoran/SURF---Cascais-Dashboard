#!/usr/bin/env python3
"""
Master Pipeline for [City] Cycling Dashboard

Trips only ever come from Supabase — there is no local/manual entry point.
This script just runs the two steps in order:

  1. generate_trips_geojson.py   — fetch every trip from Supabase, reconstruct
                                    GPS + road quality + braking, write trips.geojson
  2. road_averaging.py           — aggregate trips.geojson into per-road-segment
                                    averages, write road_segments_averaged.json

Usage:
  python master_pipeline.py

This is also what .github/workflows/generate-trips.yml runs on a schedule,
so in normal operation you never need to run this by hand — it's here for
local testing and manual re-runs.
"""

import subprocess
import sys
import time
from pathlib import Path


class Colors:
    HEADER = '\033[95m'
    CYAN   = '\033[96m'
    GREEN  = '\033[92m'
    YELLOW = '\033[93m'
    RED    = '\033[91m'
    END    = '\033[0m'
    BOLD   = '\033[1m'


def print_header(text):
    print(f"\n{Colors.HEADER}{Colors.BOLD}{'=' * 70}{Colors.END}")
    print(f"{Colors.HEADER}{Colors.BOLD}{text.center(70)}{Colors.END}")
    print(f"{Colors.HEADER}{Colors.BOLD}{'=' * 70}{Colors.END}\n")

def print_step(step_num, step_name):
    print(f"\n{Colors.CYAN}{Colors.BOLD}[STEP {step_num}] {step_name}{Colors.END}")
    print(f"{Colors.CYAN}{'─' * 70}{Colors.END}")

def print_success(text): print(f"{Colors.GREEN}✅ {text}{Colors.END}")
def print_error(text):   print(f"{Colors.RED}❌ {text}{Colors.END}")
def print_info(text):    print(f"{Colors.CYAN}ℹ️  {text}{Colors.END}")


def run_command(command, description):
    print_info(f"Running: {description}")
    print(f"{Colors.BOLD}Command:{Colors.END} {' '.join(str(c) for c in command)}\n")
    start = time.time()
    try:
        subprocess.run(command, check=True, text=True)
        print_success(f"{description} completed in {time.time() - start:.2f}s")
        return True
    except subprocess.CalledProcessError as e:
        print_error(f"{description} failed! (exit code {e.returncode})")
        return False
    except FileNotFoundError:
        print_error(f"Command not found: {command[0]}")
        return False


def _not_installed(pkg):
    try:
        __import__(pkg)
        return False
    except ImportError:
        return True


def check_prerequisites():
    print_step("0", "Checking Prerequisites")
    required = ["numpy", "psycopg2", "dotenv"]
    missing = [pkg for pkg in required if _not_installed(pkg)]
    if missing:
        print_error(f"Missing packages: {', '.join(missing)}")
        print_info("Install with: pip install psycopg2-binary python-dotenv numpy")
        return False

    for script in ("generate_trips_geojson.py", "road_averaging.py"):
        if not Path(script).exists():
            print_error(f"{script} not found")
            return False
        print_success(f"Found {script}")

    if not Path(".env").exists():
        print_error(".env not found — see README.md for the required SUPABASE_* variables")
        return False
    print_success("Found .env")
    return True


def main():
    print_header("CYCLING DASHBOARD — CLOUD PIPELINE")
    print_info(f"Python: {sys.executable} ({sys.version.split()[0]})\n")

    if not check_prerequisites():
        sys.exit(1)

    total_start = time.time()

    print_step("1", "Fetching trips from Supabase → trips.geojson")
    if not run_command([sys.executable, "generate_trips_geojson.py"], "trips.geojson generation"):
        sys.exit(1)

    print_step("2", "Averaging road segments → road_segments_averaged.json")
    if not run_command([sys.executable, "road_averaging.py"], "Road segment averaging"):
        print(f"{Colors.YELLOW}⚠️  Continuing without averaged segments{Colors.END}")

    print(f"\n{Colors.BOLD}Total time: {time.time() - total_start:.2f}s{Colors.END}")
    print_header("DONE")
    print(f"{Colors.CYAN}Next steps:{Colors.END}")
    print("  1. git add trips.geojson road_segments_averaged.json")
    print("  2. git commit -m 'Update trip data'")
    print("  3. git push")
    print("\n(Or just let .github/workflows/generate-trips.yml do this on schedule.)")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n{Colors.YELLOW}Pipeline interrupted by user{Colors.END}")
        sys.exit(1)
