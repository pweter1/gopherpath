"""
test_optimizer_determinism.py
-----------------------------
Regression test: optimize_plan() must produce identical Liberal Ed
requirement resolutions across repeated runs with the same input.

Runs optimize_plan() 5 times with the same APAS data and preferences,
then asserts:
  - the course resolved for each Liberal Ed category is identical
    across all runs
  - none of them is a TBD placeholder
  - every recommended Liberal Ed course meets UMN's per-course credit
    minimum (3cr; 4cr for Biological & Physical Sciences)
  - no recommended course is an Honors-restricted (H-suffix) section

Usage:  python backend/test_optimizer_determinism.py
"""

import json
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from optimizer import (  # noqa: E402
    optimize_plan,
    is_restricted_section,
    MIN_LE_COURSE_CREDITS,
    MIN_BIO_PHYS_COURSE_CREDITS,
)

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


def check_le_eligibility(label, result):
    """
    Verify every Liberal Ed course in the plan is actually eligible to
    fulfill its requirement: meets the UMN per-course credit minimum and
    is not an Honors-restricted (H-suffix) section.
    """
    failures = []
    for term in result["plan"]:
        for course in term["courses"]:
            cat = course["requirement_category"]
            if not _is_liberal_ed(cat) or course["subject"] == "TBD":
                continue
            code = f"{course['subject']} {course['number']}"
            if is_restricted_section(course["number"]):
                failures.append(
                    f"[{label}] RESTRICTED SECTION (H/V): {code} recommended for {cat!r}"
                )
            min_credits = (
                MIN_BIO_PHYS_COURSE_CREDITS
                if "Biological & Physical Sciences" in cat
                else MIN_LE_COURSE_CREDITS
            )
            if course["credits"] < min_credits:
                failures.append(
                    f"[{label}] BELOW CREDIT MINIMUM: {code} "
                    f"({course['credits']}cr < {min_credits}cr) for {cat!r}"
                )
    # unscheduled courses carry no credits in the formatted output;
    # still catch Honors-restricted recommendations there
    for course in result["unscheduled"]:
        cat = course["requirement_category"]
        if not _is_liberal_ed(cat) or course["subject"] == "TBD":
            continue
        if is_restricted_section(course["number"]):
            failures.append(
                f"[{label}] RESTRICTED SECTION (H/V, unscheduled): "
                f"{course['subject']} {course['number']} for {cat!r}"
            )
    return failures


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
    failures = []
    for i in range(N_RUNS):
        result = optimize_plan(parsed_apas, preferences)
        resolutions = extract_le_resolutions(result)
        all_runs.append(resolutions)
        if i == 0:
            failures += check_le_eligibility(label, result)
        print(f"--- Run {i + 1} ---")
        for cat, code in resolutions.items():
            print(f"  {cat}: {code}")
        print()
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
        # timeline=asap sorts candidates by credits ASC — the ordering that
        # surfaced 1-credit courses (NURS 4402, FOST 3331H) before the
        # eligibility filters existed. Keep it covered.
        asap_prefs = {"difficulty": "any", "timeline": "asap", "free_text": "",
                      "max_credits_per_semester": 18}
        failures += run_suite("DB parse, timeline=asap", db_parse, asap_prefs)

    if failures:
        print("FAILED:")
        for f_ in failures:
            print(f"  {f_}")
        sys.exit(1)

    print(f"PASSED: Liberal Ed resolutions identical across all {N_RUNS} runs, no TBD placeholders.")


if __name__ == "__main__":
    main()
