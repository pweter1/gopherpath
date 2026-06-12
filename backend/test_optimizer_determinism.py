"""
test_optimizer_determinism.py
-----------------------------
Regression test: optimize_plan() must produce identical Liberal Ed
requirement resolutions across repeated runs with the same input.

Runs optimize_plan() 5 times with the same APAS data and preferences,
then asserts the course resolved for each Liberal Ed category is
identical across all runs — and that none of them is a TBD placeholder.

Usage:  python backend/test_optimizer_determinism.py
"""

import json
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from optimizer import optimize_plan  # noqa: E402

N_RUNS = 5


def _is_liberal_ed(category):
    # Match both bare and parser-prefixed names
    # (e.g. "Liberal Education - Diversified Core - ...")
    return "Diversified Core" in category or "Designated Themes" in category


def extract_le_resolutions(result):
    """Map Liberal Ed category -> resolved course code, from a plan result."""
    resolutions = {}
    for term in result["plan"]:
        for course in term["courses"]:
            cat = course["requirement_category"]
            if _is_liberal_ed(cat) and cat not in resolutions:
                resolutions[cat] = f"{course['subject']} {course['number']}"
    for course in result["unscheduled"]:
        cat = course["requirement_category"]
        if _is_liberal_ed(cat) and cat not in resolutions:
            resolutions[cat] = f"{course['subject']} {course['number']} (unscheduled)"
    return resolutions


def load_db_parse():
    """Latest stored APAS parse from the students table, or None."""
    import psycopg2
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
    try:
        conn = psycopg2.connect(os.getenv("DATABASE_URL"))
        cur = conn.cursor()
        cur.execute(
            "SELECT parsed_apas_json FROM students ORDER BY created_at DESC LIMIT 1"
        )
        row = cur.fetchone()
        cur.close()
        conn.close()
        return row[0] if row else None
    except Exception as exc:
        print(f"(skipping DB input: {exc})")
        return None


def run_suite(label, parsed_apas, preferences):
    """Run optimize_plan N_RUNS times; return list of failure strings."""
    print(f"=== Input: {label} ===")
    all_runs = []
    for i in range(N_RUNS):
        result = optimize_plan(parsed_apas, preferences)
        resolutions = extract_le_resolutions(result)
        all_runs.append(resolutions)
        print(f"--- Run {i + 1} ---")
        for cat, code in resolutions.items():
            print(f"  {cat}: {code}")
        print()

    failures = []
    baseline = all_runs[0]
    for i, run in enumerate(all_runs[1:], start=2):
        if run != baseline:
            diff_cats = {
                cat for cat in set(baseline) | set(run)
                if baseline.get(cat) != run.get(cat)
            }
            for cat in sorted(diff_cats):
                failures.append(
                    f"[{label}] NONDETERMINISTIC: {cat!r} resolved to "
                    f"{baseline.get(cat)!r} in run 1 but {run.get(cat)!r} in run {i}"
                )

    for cat, code in baseline.items():
        if code.startswith("TBD"):
            failures.append(f"[{label}] PLACEHOLDER: {cat!r} resolved to {code!r}")

    return failures


def main():
    # No free_text: Claude-based reordering is intentionally nondeterministic
    # and is exercised separately. This test isolates the DB/greedy pipeline.
    preferences = {"difficulty": "any", "timeline": "on_time", "free_text": ""}

    apas_path = os.path.join(os.path.dirname(__file__), "..", "parsed_apas.json")
    with open(apas_path) as f:
        file_parse = json.load(f)

    failures = run_suite("parsed_apas.json (file)", file_parse, preferences)

    # The stored parse exercises category-name variants the Claude APAS
    # parser produces (e.g. "Liberal Education - Diversified Core - ...").
    db_parse = load_db_parse()
    if db_parse:
        failures += run_suite("latest students-row parse (DB)", db_parse, preferences)

    if failures:
        print("FAILED:")
        for f_ in failures:
            print(f"  {f_}")
        sys.exit(1)

    print(f"PASSED: Liberal Ed resolutions identical across all {N_RUNS} runs, no TBD placeholders.")


if __name__ == "__main__":
    main()
