"""
test_plan_soundness.py
----------------------
Correctness baseline for the optimizer: generates one plan from student 17's
stored APAS data (neutral preferences are currently forced inside
optimize_plan) and asserts a checklist of soundness rules:

  1. No placeholders        — no TBD/0000 courses anywhere in the plan
  2. Prereqs satisfied      — every scheduled course's prerequisites (per
                              extract_simple_prereqs + prereqs_satisfied,
                              the same logic the scheduler uses) appear in
                              completed courses or a strictly earlier term
  3. No duplicates          — no (subject, number) scheduled twice, none
                              already in the student's completed courses
  4. Credit caps            — every term <= 16 credits (on_time cap)
  5. Offering correctness   — no fall-only course in spring terms and
                              vice versa, per the courses table
  6. Liberal Ed minimums    — scheduled Designated Themes / Diversified Core
                              courses meet UMN's per-course credit minimum.
                              Verified policy (curricularhub.umn.edu,
                              Undergraduate Liberal Education Guidelines):
                              >= 3 credits, or >= 4 credits for Biological &
                              Physical Sciences (which also require a
                              lab/field component we cannot check from the
                              schema).
  7. No restricted sections — no scheduled catalog_number ending in 'H' or
                              'V' (Honors / honors-variant sections)
  8. Requirements coverage  — every non-meta remaining requirement is either
                              addressed by a scheduled course or listed in
                              unscheduled; nothing silently dropped
  9. Term structure         — plan terms are derived from the student's
                              expected_graduation: first term is the next
                              term after today, last term is at/before the
                              graduation term, count is 1..MAX_PLAN_TERMS, and
                              the term codes match build_terms() exactly

Usage:  python backend/test_plan_soundness.py
"""

import os
import sys
import datetime

sys.path.insert(0, os.path.dirname(__file__))

from optimizer import (  # noqa: E402
    optimize_plan,
    build_terms,
    get_course_from_db,
    extract_simple_prereqs,
    prereqs_satisfied,
    is_parent_header_requirement,
    is_restricted_section,
    SKIP_META_KEYWORDS,
    MIN_LE_COURSE_CREDITS,
    MIN_BIO_PHYS_COURSE_CREDITS,
    DEFAULT_MAX_CREDITS,
    ASAP_MAX_CREDITS,
    MAX_PLAN_TERMS,
)

STUDENT_ID = 17


def load_student_parse(student_id):
    import psycopg2
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    cur = conn.cursor()
    cur.execute("SELECT parsed_apas_json FROM students WHERE id = %s", (student_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        raise SystemExit(f"Student id {student_id} not found in database.")
    return row[0]


def is_meta_requirement(category):
    return any(kw.lower() in category.lower() for kw in SKIP_META_KEYWORDS)


def expected_first_term_code(today):
    """
    The next plannable term after `today`, computed independently of
    optimizer internals (Jan-May spring, Jun-Aug summer, Sep-Dec fall;
    a summer start is skipped to the following fall).
    """
    season = 0 if today.month <= 5 else (1 if today.month <= 8 else 2)
    idx = today.year * 3 + season + 1
    if idx % 3 == 1:  # summer -> skip to fall
        idx += 1
    year, s = divmod(idx, 3)
    yy = year % 100
    return f"F{yy:02d}" if s == 2 else (f"SP{yy:02d}" if s == 0 else f"SU{yy:02d}")


def code_to_index(term_code):
    """Convert a term code ('F26','SP27','SU27') to a monotonic term index."""
    if term_code.startswith("SP"):
        season, year = 0, int(term_code[2:])
    elif term_code.startswith("SU"):
        season, year = 1, int(term_code[2:])
    else:  # F##
        season, year = 2, int(term_code[1:])
    return (year + 2000) * 3 + season


def run_checks(parsed_apas, result, max_credits_cap=DEFAULT_MAX_CREDITS):
    failures = []     # (rule, message)
    warnings = []
    double_dips = []  # informational: one course satisfying 2+ requirements

    terms = result["plan"]
    scheduled = [
        (term, course) for term in terms for course in term["courses"]
    ]

    completed_codes = {
        f"{c['subject']} {c['number']}"
        for c in parsed_apas.get("completed_courses", [])
        if not c.get("is_withdrawn")
    }

    # ── Rule 1: no placeholders ──────────────────────────────────────────
    for term, c in scheduled:
        if c["subject"] == "TBD" or c["number"] == "0000":
            failures.append((
                "1 No placeholders",
                f"{term['term_code']}: TBD placeholder for "
                f"{c['requirement_category']!r}"
            ))
    for c in result["unscheduled"]:
        if c["subject"] == "TBD" or c["number"] == "0000":
            failures.append((
                "1 No placeholders",
                f"unscheduled: TBD placeholder for {c['requirement_category']!r}"
            ))

    # ── Rule 2: prerequisites satisfied ──────────────────────────────────
    # satisfied set per term = completed courses + everything in earlier terms
    satisfied = set(completed_codes)
    for term in terms:
        term_codes = {f"{c['subject']} {c['number']}" for c in term["courses"]}
        for c in term["courses"]:
            if c["subject"] == "TBD":
                continue
            code = f"{c['subject']} {c['number']}"
            db_course = get_course_from_db(c["subject"], c["number"])
            if db_course is None:
                warnings.append(
                    f"{code} ({term['term_code']}) not found in courses table — "
                    f"prereq/offering checks skipped"
                )
                continue
            prereqs = extract_simple_prereqs(c["subject"], c["number"])
            if prereqs and not prereqs_satisfied(
                prereqs, db_course.get("prereq_raw"), satisfied
            ):
                failures.append((
                    "2 Prerequisites satisfied",
                    f"{code} in {term['term_code']} requires {prereqs} "
                    f"(raw: {(db_course.get('prereq_raw') or '')[:90]!r}) — "
                    f"not satisfied by completed courses or earlier terms"
                ))
        satisfied |= term_codes

    # ── Rule 3: no duplicates ────────────────────────────────────────────
    seen = {}
    for term, c in scheduled:
        if c["subject"] == "TBD":
            continue
        code = f"{c['subject']} {c['number']}"
        if code in seen:
            failures.append((
                "3 No duplicates",
                f"{code} scheduled twice: {seen[code]} and {term['term_code']}"
            ))
        seen[code] = term["term_code"]
        if code in completed_codes:
            failures.append((
                "3 No duplicates",
                f"{code} scheduled in {term['term_code']} but already completed"
            ))

    # ── Rule 4: credit caps ──────────────────────────────────────────────
    for term in terms:
        if term["total_credits"] > max_credits_cap:
            failures.append((
                "4 Credit caps",
                f"{term['term_code']} has {term['total_credits']} credits "
                f"(cap {max_credits_cap})"
            ))

    # ── Rule 5: offering correctness ─────────────────────────────────────
    for term in terms:
        is_fall = term["term_code"].startswith("F")
        for c in term["courses"]:
            if c["subject"] == "TBD":
                continue
            db_course = get_course_from_db(c["subject"], c["number"])
            if db_course is None:
                continue  # warned in rule 2
            if is_fall and db_course["offered_fall"] is False:
                failures.append((
                    "5 Offering correctness",
                    f"{c['subject']} {c['number']} in {term['term_code']} "
                    f"but offered_fall=False"
                ))
            if not is_fall and db_course["offered_spring"] is False:
                failures.append((
                    "5 Offering correctness",
                    f"{c['subject']} {c['number']} in {term['term_code']} "
                    f"but offered_spring=False"
                ))

    # ── Rule 6: Liberal Ed credit minimums ───────────────────────────────
    # A course must meet the minimum for EVERY LE category it claims to
    # satisfy (double-dip courses claim two).
    for term, c in scheduled:
        if c["subject"] == "TBD":
            continue
        for cat in c.get("satisfies_categories", [c["requirement_category"]]):
            if "Designated Themes" not in cat and "Diversified Core" not in cat:
                continue
            min_credits = (
                MIN_BIO_PHYS_COURSE_CREDITS
                if "Biological & Physical Sciences" in cat
                else MIN_LE_COURSE_CREDITS
            )
            if c["credits"] < min_credits:
                failures.append((
                    "6 Liberal Ed credit minimums",
                    f"{c['subject']} {c['number']} ({c['credits']}cr) in "
                    f"{term['term_code']} for {cat!r} — below {min_credits}cr minimum"
                ))

    # ── Rule 7: no restricted-section courses (Honors H/V suffixes) ─────
    for term, c in scheduled:
        if c["subject"] != "TBD" and is_restricted_section(c["number"]):
            failures.append((
                "7 No restricted sections",
                f"{c['subject']} {c['number']} ({c.get('title', '')!r}) in "
                f"{term['term_code']} — restricted (Honors H/V) catalog number"
            ))

    # ── Rule 8: requirements coverage ────────────────────────────────────
    # A scheduled course may satisfy multiple requirements (double-dip), so
    # credit every category in its satisfies_categories, not just its primary
    # requirement_category. Also map each non-meta requirement category to the
    # scheduled course(s) covering it, to surface double-dips for visibility.
    scheduled_categories = set()
    category_to_courses = {}   # category -> set of "SUBJ NUMBER"
    for term, c in scheduled:
        if c["subject"] == "TBD":
            continue
        code = f"{c['subject']} {c['number']}"
        for cat in c.get("satisfies_categories", [c["requirement_category"]]):
            scheduled_categories.add(cat)
            category_to_courses.setdefault(cat, set()).add(code)
    unscheduled_categories = {
        cat
        for c in result["unscheduled"]
        for cat in c.get("satisfies_categories", [c["requirement_category"]])
    }
    remaining = parsed_apas.get("remaining_requirements", [])

    # Double-dip visibility: one scheduled course covering 2+ requirements.
    course_to_categories = {}   # code -> set of non-meta requirement categories
    req_categories = {
        r.get("category", "") for r in remaining
        if not is_meta_requirement(r.get("category", ""))
    }
    for cat, codes in category_to_courses.items():
        if cat not in req_categories:
            continue
        for code in codes:
            course_to_categories.setdefault(code, set()).add(cat)
    for code, cats in sorted(course_to_categories.items()):
        if len(cats) > 1:
            double_dips.append((code, sorted(cats)))

    def is_accounted_for(r):
        cat = r.get("category", "")
        return (
            is_meta_requirement(cat)
            or cat in scheduled_categories
            or cat in unscheduled_categories
        )

    for req in remaining:
        category = req.get("category", "")
        if is_meta_requirement(category):
            continue
        if category in scheduled_categories or category in unscheduled_categories:
            continue
        # Parent/group header rows (credits_needed=None with separately
        # tracked children) are intentionally skipped by the optimizer —
        # acceptable ONLY if every child is itself accounted for. A
        # credits_needed=None row with no children never reaches this branch
        # (is_parent_header_requirement returns False) and fails below.
        if is_parent_header_requirement(req, remaining):
            children = [
                r for r in remaining
                if r.get("category", "").startswith(category + " - ")
            ]
            unaccounted = [
                r["category"] for r in children if not is_accounted_for(r)
            ]
            if not unaccounted:
                continue
            failures.append((
                "8 Requirements coverage",
                f"parent header {category!r} skipped, but its children are "
                f"not all accounted for: {unaccounted}"
            ))
            continue
        failures.append((
            "8 Requirements coverage",
            f"{category!r} (credits_needed={req.get('credits_needed')}, "
            f"options={len(req.get('options') or [])}) — silently dropped: "
            f"no scheduled course and no unscheduled entry"
        ))

    # ── Rule 9: term structure (dynamic terms from expected_graduation) ──
    grad = parsed_apas.get("student", {}).get("expected_graduation")
    plan_codes = [t["term_code"] for t in result["plan"]]
    today = datetime.date.today()

    if not plan_codes:
        failures.append(("9 Term structure", "plan has no terms"))
    else:
        # 9a: first term is the next plannable term after today
        expected_first = expected_first_term_code(today)
        if plan_codes[0] != expected_first:
            failures.append((
                "9 Term structure",
                f"first term {plan_codes[0]} != expected next term "
                f"{expected_first} after {today}"
            ))
        # 9b: last term is at or before the graduation term
        from optimizer import _parse_graduation  # noqa: E402
        parsed_grad = _parse_graduation(grad)
        if parsed_grad:
            grad_idx = parsed_grad[0] * 3 + parsed_grad[1]
            if code_to_index(plan_codes[-1]) > grad_idx:
                failures.append((
                    "9 Term structure",
                    f"last term {plan_codes[-1]} is after graduation {grad!r}"
                ))
        # 9c: term count is sane
        if not (1 <= len(plan_codes) <= MAX_PLAN_TERMS):
            failures.append((
                "9 Term structure",
                f"term count {len(plan_codes)} outside 1..{MAX_PLAN_TERMS}"
            ))
        # 9d: plan terms match build_terms() exactly (optimize_plan used it)
        expected_codes = [t["code"] for t in build_terms(grad, today)]
        if plan_codes != expected_codes:
            failures.append((
                "9 Term structure",
                f"plan terms {plan_codes} != build_terms() {expected_codes}"
            ))

    return failures, warnings, double_dips


DIFFICULTIES = ["easy", "medium", "hard", "any"]
TIMELINES = ["asap", "on_time"]


def numeric_catalog(number):
    """Leading digits of a catalog number ('3562W' -> 3562), or None."""
    digits = ""
    for ch in number:
        if ch.isdigit():
            digits += ch
        else:
            break
    return int(digits) if digits else None


def scheduled_codes_of(result):
    return {
        f"{c['subject']} {c['number']}"
        for term in result["plan"] for c in term["courses"]
        if c["subject"] != "TBD"
    }


def nonempty_term_count(result):
    return sum(1 for term in result["plan"] if term["courses"])


def open_selections(result):
    """
    Open-requirement (is_pinned=False, non-TBD) course picks, including
    unscheduled ones, as requirement_category -> "SUBJ NUMBER".
    These are the picks difficulty is supposed to influence.
    """
    picks = {}
    for term in result["plan"]:
        for c in term["courses"]:
            if not c.get("is_pinned") and c["subject"] != "TBD":
                picks[c["requirement_category"]] = f"{c['subject']} {c['number']}"
    for c in result["unscheduled"]:
        if not c.get("is_pinned") and c["subject"] != "TBD":
            picks.setdefault(c["requirement_category"], f"{c['subject']} {c['number']}")
    return picks


def print_plan(result):
    print(f"  status: {result['status']} — {result['message']}")
    for term in result["plan"]:
        codes = ", ".join(f"{c['subject']} {c['number']}" for c in term["courses"])
        print(f"    {term['term_code']} ({term['total_credits']}cr): {codes or '(empty)'}")
    if result["unscheduled"]:
        print("    unscheduled: " + ", ".join(
            f"{c['subject']} {c['number']}" for c in result["unscheduled"]))


def analyze_timeline_effect(results):
    """For each difficulty, compare asap vs on_time and describe the effect."""
    print("\n=== Timeline effect (asap vs on_time, per difficulty) ===")
    for diff in DIFFICULTIES:
        asap = results[(diff, "asap")]
        ontime = results[(diff, "on_time")]
        asap_sched = scheduled_codes_of(asap)
        ontime_sched = scheduled_codes_of(ontime)

        ent_recovered = "ENT 1001" in asap_sched and "ENT 1001" not in ontime_sched
        gained = asap_sched - ontime_sched
        fewer_terms = nonempty_term_count(asap) < nonempty_term_count(ontime)

        if ent_recovered:
            note = "(a) ENT 1001 scheduled under asap but not on_time"
        elif fewer_terms:
            note = (f"(b) asap uses {nonempty_term_count(asap)} terms vs "
                    f"{nonempty_term_count(ontime)} for on_time")
        elif gained:
            note = (f"asap scheduled {len(gained)} course(s) on_time did not: "
                    f"{sorted(gained)}")
        else:
            asap_max = max(t["total_credits"] for t in asap["plan"])
            note = ("(c) no measurable scheduling difference — asap's higher cap "
                    f"(18) went unused (heaviest term {asap_max}cr <= 16); the "
                    "remaining work simply fits within on_time limits")
        print(f"  difficulty={diff}: {note}")


def analyze_difficulty_effect(results):
    """For each timeline, compare easy vs hard open-requirement catalog numbers."""
    print("\n=== Difficulty effect (easy vs hard open picks, per timeline) ===")
    for tl in TIMELINES:
        easy = open_selections(results[("easy", tl)])
        hard = open_selections(results[("hard", tl)])
        cats = sorted(set(easy) | set(hard))
        print(f"  timeline={tl}:")
        easy_nums, hard_nums = [], []
        for cat in cats:
            e, h = easy.get(cat), hard.get(cat)
            en = numeric_catalog(e.split()[1]) if e else None
            hn = numeric_catalog(h.split()[1]) if h else None
            if en is not None:
                easy_nums.append(en)
            if hn is not None:
                hard_nums.append(hn)
            marker = ""
            if en is not None and hn is not None:
                marker = " (hard higher)" if hn > en else \
                         " (easy higher)" if en > hn else " (same pick)"
            short = cat.split(" - ")[-1]
            print(f"    {short:38s} easy={e or '—':10s} hard={h or '—':10s}{marker}")
        if easy_nums and hard_nums:
            ea = sum(easy_nums) / len(easy_nums)
            ha = sum(hard_nums) / len(hard_nums)
            verdict = ("hard skews higher (expected)" if ha > ea
                       else "easy skews higher (unexpected)" if ea > ha
                       else "identical — pools likely offer one viable option")
            print(f"    mean catalog#: easy={ea:.0f}, hard={ha:.0f} — {verdict}")


def main():
    parsed_apas = load_student_parse(STUDENT_ID)

    results = {}
    any_failures = False

    print("=== Soundness across 8 preference combinations ===")
    for diff in DIFFICULTIES:
        for tl in TIMELINES:
            prefs = {"difficulty": diff, "timeline": tl, "free_text": ""}
            result = optimize_plan(parsed_apas, prefs)
            results[(diff, tl)] = result
            cap = ASAP_MAX_CREDITS if tl == "asap" else DEFAULT_MAX_CREDITS
            failures, warnings, double_dips = run_checks(
                parsed_apas, result, max_credits_cap=cap)

            print(f"\n[difficulty={diff}, timeline={tl}, cap={cap}]")
            print_plan(result)
            for w in warnings:
                print(f"  WARNING: {w}")
            for code, cats in double_dips:
                short = [c.split(" - ")[-1] for c in cats]
                print(f"  DOUBLE-DIP: {code} satisfies {short}")
            if failures:
                any_failures = True
                print(f"  FAILED — {len(failures)} violation(s):")
                for rule, msg in failures:
                    print(f"    [{rule}] {msg}")
            else:
                print("  PASSED: all 9 soundness rules hold.")

    analyze_timeline_effect(results)
    analyze_difficulty_effect(results)

    print()
    if any_failures:
        print("RESULT: FAILED — at least one combination violated a soundness rule.")
        sys.exit(1)
    print("RESULT: PASSED — all 8 combinations satisfy all 9 soundness rules.")


if __name__ == "__main__":
    main()
