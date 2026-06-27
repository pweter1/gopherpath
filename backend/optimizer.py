"""
optimizer.py
------------
Constraint-based course plan optimizer for GopherPath.

Takes a student's parsed APAS data and generates a valid
semester-by-semester course plan.

Architecture:
  1. PrerequisiteGraph  — builds a NetworkX DAG from course prerequisite data
  2. PlanOptimizer      — uses OR-Tools to assign courses to semesters
  3. optimize_plan()    — main entry point, returns a structured plan

Design decisions:
  - OR-Tools handles hard constraints (credit limits, prerequisites, graduation)
  - Soft preferences (difficulty, interest) become objective function weights
  - Claude API handles ambiguous cases (open requirements with many options)
  - The optimizer fails gracefully — returns best partial plan if full plan
    is impossible, with clear explanation of what couldn't be resolved
"""

import os
import re
import sys
import json
import datetime
import networkx as nx
import psycopg2
import anthropic
from dotenv import load_dotenv
from ortools.sat.python import cp_model

# Policy values live in umn_policy.json (loaded via policy.py). optimizer.py is
# imported both as `backend.optimizer` (from main.py) and as `optimizer` (from
# tests with backend/ on sys.path), so try both import paths.
try:
    from backend.policy import POLICY
except ImportError:
    from policy import POLICY

load_dotenv()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Fallback term list, used only when expected_graduation is missing or
# unparseable. Normally optimize_plan() derives terms via build_terms().
# Sequence number determines ordering (lower = earlier).
TERMS = [
    {"code": "F26",  "label": "Fall 2026",   "seq": 0, "is_fall": True,  "is_summer": False},
    {"code": "SP27", "label": "Spring 2027", "seq": 1, "is_fall": False, "is_summer": False},
    {"code": "F27",  "label": "Fall 2027",   "seq": 2, "is_fall": True,  "is_summer": False},
    {"code": "SP28", "label": "Spring 2028", "seq": 3, "is_fall": False, "is_summer": False},
]

# Seasons, ordered chronologically within a calendar year. Combined into a
# single monotonic index (year*3 + season) so terms can be stepped/compared.
_SEASON_SPRING, _SEASON_SUMMER, _SEASON_FALL = 0, 1, 2
MAX_PLAN_TERMS = POLICY["scheduling"]["max_terms"]  # cap a plan at 4 years


def _term_index(year, season):
    return year * 3 + season


def _season_for_month(month):
    """UMN-ish term calendar: Jan-May spring, Jun-Aug summer, Sep-Dec fall."""
    if month <= 5:
        return _SEASON_SPRING
    if month <= 8:
        return _SEASON_SUMMER
    return _SEASON_FALL


def _parse_graduation(expected_graduation):
    """
    Parse an APAS expected_graduation string into (year, season), or None.

    The Claude APAS parser is inconsistent: 'Spr 28', 'Spring 2028',
    'Fall 2026', 'Sum 27' all occur. Match a season word + a 2- or 4-digit
    year (2-digit assumed 20xx).
    """
    if not expected_graduation:
        return None
    m = re.search(r"([A-Za-z]+)\s*'?\s*(\d{2,4})", expected_graduation.strip())
    if not m:
        return None
    word = m.group(1).lower()
    year = int(m.group(2))
    if year < 100:
        year += 2000
    if word.startswith("sp"):       # spr, spring
        season = _SEASON_SPRING
    elif word.startswith("su"):     # sum, summer
        season = _SEASON_SUMMER
    elif word.startswith("f"):      # fall, fal, f
        season = _SEASON_FALL
    else:
        return None
    return (year, season)


def _make_term(idx, seq):
    """Build a term dict from a monotonic term index and sequence number."""
    year, season = divmod(idx, 3)
    yy = year % 100
    if season == _SEASON_FALL:
        return {"code": f"F{yy:02d}", "label": f"Fall {year}", "seq": seq,
                "is_fall": True, "is_summer": False}
    if season == _SEASON_SPRING:
        return {"code": f"SP{yy:02d}", "label": f"Spring {year}", "seq": seq,
                "is_fall": False, "is_summer": False}
    return {"code": f"SU{yy:02d}", "label": f"Summer {year}", "seq": seq,
            "is_fall": False, "is_summer": True}


def _regular_terms_from(start_idx, n):
    """n consecutive Fall/Spring terms starting at start_idx (summers skipped)."""
    terms, idx, seq = [], start_idx, 0
    while len(terms) < n:
        if idx % 3 != _SEASON_SUMMER:
            terms.append(_make_term(idx, seq))
            seq += 1
        idx += 1
    return terms


def build_terms(expected_graduation, today=None):
    """
    Derive the plannable term list from a student's expected graduation.

    Terms run from the next term after `today` up to and including the
    graduation term. Regular plannable terms are Fall and Spring; summer
    terms are skipped UNLESS summer is the graduation term itself. Capped at
    MAX_PLAN_TERMS. Falls back gracefully (with a warning) on bad input.
    """
    today = today or datetime.date.today()
    start_idx = _term_index(today.year, _season_for_month(today.month)) + 1
    # Don't start a plan in summer — advance to the following fall.
    if start_idx % 3 == _SEASON_SUMMER:
        start_idx += 1

    grad = _parse_graduation(expected_graduation)
    if grad is None:
        # UMN APAS never populates the grad term, so null is the common case
        # (the student should have been prompted on the confirm screen). Use 4
        # terms from the current term — a reasonable, year-agnostic default —
        # NOT the year-specific module-level TERMS.
        print(f"[optimizer] WARNING: missing/unparseable expected_graduation "
              f"{expected_graduation!r}; defaulting to 4 terms from the current "
              f"term. The student should set their graduation date.",
              file=sys.stderr)
        return _regular_terms_from(start_idx, 4)

    grad_idx = _term_index(*grad)
    if grad_idx < start_idx:
        print(f"[optimizer] WARNING: expected_graduation {expected_graduation!r} "
              f"is already past; using 4-term fallback from current term.",
              file=sys.stderr)
        return _regular_terms_from(start_idx, 4)

    terms, idx, seq = [], start_idx, 0
    while idx <= grad_idx and len(terms) < MAX_PLAN_TERMS:
        is_grad = (idx == grad_idx)
        if idx % 3 == _SEASON_SUMMER and not is_grad:
            idx += 1
            continue
        terms.append(_make_term(idx, seq))
        seq += 1
        idx += 1

    if idx <= grad_idx:
        print(f"[optimizer] WARNING: graduation {expected_graduation!r} is more "
              f"than {MAX_PLAN_TERMS} terms away; capping plan length.",
              file=sys.stderr)
    if not terms:
        return _regular_terms_from(start_idx, 4)
    return terms

# Course aliases — some courses appear under different numbers in prereq
# text vs what students actually take. This mapping handles known cases.
# Key: number as it appears in prereq text
# Value: number as it appears in transcripts/our database
COURSE_ALIASES = {
    "CSCI 3081W": "CSCI 3061",
    "CSCI 3081":  "CSCI 3061",
    "CSCI 4061":  "CSCI 3061",  # old number for Computer Systems
}

DEFAULT_MAX_CREDITS = POLICY["scheduling"]["default_max_credits"]
DEFAULT_MIN_CREDITS = POLICY["scheduling"]["default_min_credits"]
# timeline="asap" raises the per-semester cap so more courses fit each term
# (mirrors the value main.py's /optimize endpoint passes for asap)
ASAP_MAX_CREDITS = POLICY["scheduling"]["asap_max_credits"]

# Largest course credit value eligible for recommendation
MAX_RECOMMENDED_CREDITS = POLICY["course_filters"]["max_recommended_credits"]

# Meta-requirements (total credit counts, GPA requirements) — these are
# degree-level checks, not schedulable courses
SKIP_META_KEYWORDS = POLICY["meta_requirement_keywords"]


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db_connection():
    return psycopg2.connect(os.getenv("DATABASE_URL"))


def get_course_from_db(subject, number):
    """
    Looks up a course in the database by subject and catalog number.
    Returns a dict with course data, or None if not found.

    Handles the W suffix mismatch: APAS lists courses as '3562W' but
    our database stores them as '3562'. We try exact match first,
    then strip the W suffix and try again.
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    def query(subj, num):
        cursor.execute("""
            SELECT id, subject_code, catalog_number, title,
                   credits, min_credits, max_credits,
                   offered_fall, offered_spring, offered_summer,
                   prereq_raw
            FROM courses
            WHERE subject_code = %s AND catalog_number = %s
        """, (subj.upper(), num))
        return cursor.fetchone()

    try:
        # Try exact match first
        row = query(subject, number)

        # If not found and number ends in W, try without W
        if not row and number.upper().endswith('W'):
            row = query(subject, number[:-1])

        # If not found and number ends in H, try without H
        if not row and number.upper().endswith('H'):
            row = query(subject, number[:-1])

        if not row:
            return None

        return {
            "id": row[0],
            "subject": row[1],
            "number": row[2],
            "title": row[3],
            "credits": float(row[4]) if row[4] else 3.0,
            "min_credits": float(row[5]) if row[5] else 3.0,
            "max_credits": float(row[6]) if row[6] else 3.0,
            "offered_fall": row[7],
            "offered_spring": row[8],
            "offered_summer": row[9],
            "prereq_raw": row[10],
        }

    finally:
        cursor.close()
        conn.close()

def extract_simple_prereqs(subject, number):
    """
    Extracts simple prerequisite course codes from prereq_raw text.

    Handles the most common pattern: a single course number reference
    like "prereq: 5101" or "prereq: CSCI 1133".

    This is a lightweight parser for the optimizer. The full Claude-based
    prerequisite parser (parse_prerequisites.py) will handle complex
    AND/OR logic in a later phase.

    Returns a list of course codes, e.g. ['STAT 5101', 'MATH 5651']
    """
    db_course = get_course_from_db(subject, number)
    if not db_course or not db_course.get("prereq_raw"):
        return []

    prereq_raw = db_course["prereq_raw"].lower()
    prereqs = []

    import re

    # Pattern 1: "prereq: SUBJ 1234" — explicit subject + number
    explicit = re.findall(
        r'\b([A-Z]{2,5})\s+(\d{4}[A-Z]?)\b',
        db_course["prereq_raw"]
    )
    for subj, num in explicit:
        prereqs.append(f"{subj} {num}")

    # Pattern 2: "prereq: 5101" — bare number, same subject assumed
    bare = re.findall(r'prereq[s]?:\s*(\d{4})', prereq_raw)
    for num in bare:
        prereqs.append(f"{subject.upper()} {num}")

    # Deduplicate while preserving order
    seen = set()
    unique = []
    for p in prereqs:
        if p not in seen:
            seen.add(p)
            unique.append(p)

    return unique

# Map APAS requirement category names to our CLE attribute values.
# 'Writing Intensive' maps to [None] (JSON null) — handled separately via the
# WI attribute. Insertion order is preserved by json.load and matters for the
# suffix-matching in match_le_category().
LE_MAPPING = POLICY["liberal_education"]["requirement_mapping"]

# UMN Liberal Education guidelines (curricularhub.umn.edu): a course must
# "be at least 3 credits (or at least 4 credits for biological or physical
# sciences, which must include a lab or field experience component)" to
# carry an LE designation. Recommending below-threshold courses would tell
# students a requirement is satisfied when it isn't.
MIN_LE_COURSE_CREDITS = POLICY["liberal_education"]["default_min_credits"]
MIN_BIO_PHYS_COURSE_CREDITS = (
    POLICY["liberal_education"]["categories"]["Biological Sciences"]["min_credits"]
)
# CLE values with a category-specific credit minimum (e.g. bio/phys = 4cr)
BIO_PHYS_CLE_VALUES = set(POLICY["liberal_education"]["categories"].keys())


def min_credits_for_cle_value(cle_value):
    """Per-course credit minimum for a CLE attribute value."""
    category = POLICY["liberal_education"]["categories"].get(cle_value)
    if category:
        return category["min_credits"]
    return MIN_LE_COURSE_CREDITS


# Catalog-number suffixes marking enrollment-restricted sections: H = Honors,
# V = Honors variant (honors + writing intensive). We don't track Honors
# program membership, so candidate-selection queries never recommend these.
# (W = writing intensive is NOT restricted and stays eligible.)
RESTRICTED_SECTION_SUFFIXES = tuple(POLICY["course_filters"]["restricted_section_suffixes"])
# SQL regex form, used with !~* (case-insensitive)
RESTRICTED_SUFFIX_PATTERN = POLICY["course_filters"]["restricted_section_sql_pattern"]


def is_restricted_section(catalog_number):
    """True for Honors/honors-variant catalog numbers (H or V suffix)."""
    return catalog_number.upper().endswith(RESTRICTED_SECTION_SUFFIXES)


# Directed/independent study courses require individual instructor
# arrangement and permission — they aren't generally enrollable, so they are
# never recommended (same spirit as RESTRICTED_SECTION_SUFFIXES). Filtered by
# title because catalog-number conventions vary by department (x793, x794,
# x993, x994, 4094, ...). SQL regex form, used with !~* (case-insensitive).
DIRECTED_STUDY_TITLE_PATTERN = POLICY["course_filters"]["directed_study_title_pattern"]

# Quality floor for recommended Writing Intensive courses. UMN publishes no
# per-course WI credit minimum (unlike the Liberal Ed 3/4cr rule), but the
# norm is clear: 453 of 491 upper-division WI courses are 3+ credits, and the
# sub-3cr ones are mostly directed studies/seminars. Norm-based assumption —
# revisit if a real policy minimum surfaces.
MIN_WI_CREDITS = POLICY["writing_intensive"]["min_course_credits"]
# WI upper-division catalog-number threshold (3xxx and above)
WI_UPPER_DIVISION_THRESHOLD = POLICY["writing_intensive"]["upper_division_threshold"]


# UMN writing requirement (onestop.umn.edu): four WI courses total, two of
# them upper-division (3xxx+), one of those within the major. The APAS lists
# the in-major upper-division WI as its own sub-requirement with explicit
# course options; the bare parent "Writing Intensive" row carries no credit
# count, so we schedule this many additional upper-division WI courses for it.
WI_ADDITIONAL_COURSES = POLICY["writing_intensive"]["additional_courses_to_schedule"]

# Courses UMN designates as counting double toward the WI total.
# Source: UMN APAS audit — "WRIT 3562W counts as 2 upper-division WI courses."
DOUBLE_COUNT_WI_COURSES = set(POLICY["writing_intensive"]["double_count_courses"].keys())


def _normalize_le_category(category):
    """
    Normalize APAS parser variants to match LE_MAPPING keys.
    Handles: colon separators, 'Liberal Education - ' prefix.
    e.g. 'Liberal Education - Diversified Core: Historical Perspectives'
         → 'Diversified Core - Historical Perspectives'
    """
    normalized = category.strip()
    # Strip known parser-added prefixes
    for prefix in ("Liberal Education - ", "Liberal Ed - "):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):]
    # Normalize colon separators to dash separators
    normalized = normalized.replace(": ", " - ")
    return normalized


def is_wi_parent_requirement(category):
    """
    True for the bare overall Writing Intensive requirement (with any
    parser-added prefix), as opposed to named WI sub-requirements like
    'Writing Intensive - Upper-Division Writing Intensive within the Major'.
    """
    return category.strip().endswith("Writing Intensive")


def is_parent_header_requirement(req, all_requirements):
    """
    True for grouping rows like 'Liberal Education - Designated Themes' that
    carry no credit count and exist only to group child requirements the APAS
    parser tracks as separate rows (categories extending this one with
    ' - ...'). Scheduling such a row would double-count its children.

    The bare Writing Intensive parent also has a tracked child but represents
    real additional courses (see WI_ADDITIONAL_COURSES), so it is excluded.
    A credits_needed=None row with NO tracked children is not a header — it
    is a real requirement and must not be skipped under this rule.
    """
    if req.get("credits_needed") is not None:
        return False
    category = req.get("category", "")
    if is_wi_parent_requirement(category):
        return False
    # Normalize both sides so colon-style children (e.g.
    # 'Designated Themes: Civic Life and Ethics') are recognized as children
    # of 'Designated Themes'.
    prefix = _normalize_le_category(category) + " - "
    return any(
        _normalize_le_category(other.get("category", "")).startswith(prefix)
        for other in all_requirements
        if other is not req
    )


def match_le_category(requirement_category):
    """
    Maps an APAS requirement category to its CLE attribute values.

    The Claude-based APAS parser is not consistent about category names:
      - parent-group prefixes vary ('Liberal Education - Diversified Core -...'
        vs 'Diversified Core -...')
      - separators vary (': ' vs ' - ')
      - names can be truncated ('Diversified Core - Historical Perspectives'
        for '...Historical Perspectives & Social Sciences') or carry a
        redundant tail ('...Arts/Humanities & Literature - Literature')
    So we normalize first (see _normalize_le_category), then try, in order:
    exact, suffix (absorbs a prefix), prefix (absorbs a truncated tail), and
    reverse-prefix (absorbs a redundant tail).

    Returns the CLE value list, or None if the category isn't a Liberal Ed
    requirement we can query.
    """
    requirement_category = _normalize_le_category(requirement_category)
    values = LE_MAPPING.get(requirement_category)
    if values:
        return values
    for key, vals in LE_MAPPING.items():
        if requirement_category.endswith(key):
            return vals
    # Prefix match: requirement is a truncation of a known key (e.g.
    # 'Diversified Core - Historical Perspectives' -> '...& Social Sciences').
    # Guard against the WI sentinel key (empty-ish) matching everything.
    for key, vals in LE_MAPPING.items():
        if vals != [None] and key.startswith(requirement_category):
            return vals
    # Reverse-prefix: requirement carries a redundant tail after a known key
    # (e.g. 'Diversified Core - Arts/Humanities & Literature - Literature').
    for key, vals in LE_MAPPING.items():
        if vals != [None] and requirement_category.startswith(key):
            return vals
    return None


def _candidate_order_clause(preferences):
    """
    Deterministic ORDER BY for candidate queries, derived from preferences.

    difficulty="hard" → higher catalog numbers first (4xxx/5xxx);
    anything else → lower numbers first (1xxx/2xxx).
    timeline="asap" → prefer fewer-credit courses so more fit per semester.
    Tie-breakers (subject_code, title) make the ordering total — catalog
    numbers repeat across subjects, and Postgres returns tied rows in
    arbitrary order otherwise.
    """
    pref = preferences or {}
    difficulty = pref.get("difficulty", "any")
    timeline = pref.get("timeline", "on_time")
    # safe: catalog_dir is only ever "ASC" or "DESC"
    catalog_dir = "DESC" if difficulty == "hard" else "ASC"
    if timeline == "asap":
        return f"c.credits ASC, c.catalog_number {catalog_dir}, c.subject_code ASC, c.title ASC"
    return f"c.catalog_number {catalog_dir}, c.subject_code ASC, c.title ASC"


def recommend_multi_tag_courses(open_le_reqs, completed_courses, preferences=None, scheduled_codes=None):
    """
    Proactive "double-dip" pass: finds single courses whose CLE tags cover
    TWO of the student's open Liberal Ed requirements at once, so one
    course's credits satisfy two requirements.

    UMN policy (curricularhub.umn.edu): a course may be approved to meet
    "one Core or one Theme or both a Core and a Theme" — never two Cores or
    two Themes — so only Core+Theme pairs are eligible.

    open_le_reqs: ordered list of (requirement_category, cle_values) for the
    student's open CLE-backed requirements.

    Returns candidate dicts with satisfies_categories set to the two covered
    requirement categories. Restricted to prereq-free courses for the same
    reason as the WI recommendations: the scheduler doesn't know the prereq
    chains of recommended courses.
    """
    if len(open_le_reqs) < 2:
        return []

    value_to_category = {}
    for category, values in open_le_reqs:
        for v in values:
            value_to_category[v] = category

    completed_codes = {
        f"{c['subject']} {c['number']}"
        for c in completed_courses
        if not c.get('is_withdrawn')
    }
    already_scheduled = scheduled_codes or set()
    order_clause = _candidate_order_clause(preferences)

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(f"""
            SELECT c.subject_code, c.catalog_number, c.title, c.credits,
                   c.offered_fall, c.offered_spring,
                   array_agg(DISTINCT ca.value) AS matched_values
            FROM courses c
            JOIN course_attributes ca ON c.id = ca.course_id
            WHERE ca.attribute = 'CLE'
              AND ca.value = ANY(%s)
              AND c.acad_career = 'UGRD'
              AND c.credits IS NOT NULL
              AND c.credits <= {MAX_RECOMMENDED_CREDITS}
              AND c.catalog_number !~* '{RESTRICTED_SUFFIX_PATTERN}'
              AND c.title !~* '{DIRECTED_STUDY_TITLE_PATTERN}'
              AND (c.prereq_raw IS NULL OR c.prereq_raw = '')
            GROUP BY c.id, c.subject_code, c.catalog_number, c.title,
                     c.credits, c.offered_fall, c.offered_spring
            HAVING COUNT(DISTINCT ca.value) >= 2
            ORDER BY {order_clause}
        """, (list(value_to_category.keys()),))

        results = []
        for subject, number, title, credits, off_fall, off_spring, matched in cursor.fetchall():
            code = f"{subject} {number}"
            if code in completed_codes or code in already_scheduled:
                continue
            credits = float(credits)

            # Categories this course can legitimately cover: the tag must map
            # to an open requirement AND the course must meet that
            # requirement's per-course credit minimum (4cr for bio/phys).
            covered = []
            for category, values in open_le_reqs:
                if category in covered:
                    continue
                if any(v in matched and credits >= min_credits_for_cle_value(v)
                       for v in values):
                    covered.append(category)

            cores = [cat for cat in covered if "Diversified Core" in cat]
            themes = [cat for cat in covered if "Designated Themes" in cat]
            if not cores or not themes:
                continue
            # Policy allows at most one Core + one Theme; pick the first of
            # each in requirement order for determinism.
            pair = [cores[0], themes[0]]

            results.append({
                "subject": subject,
                "number": number,
                "title": title,
                "credits": credits,
                "requirement_category": pair[0],
                "satisfies_categories": pair,
                "is_pinned": False,
                "term_locked": None,
                "offered_fall": off_fall,
                "offered_spring": off_spring,
                "prereqs": [],
                "prereq_raw": "",
                "cle_value": None,
            })
        return results
    finally:
        cursor.close()
        conn.close()


def recommend_courses_for_requirement(requirement_category, completed_courses, max_results=3, preferences=None, scheduled_codes=None):
    """
    Recommends real courses from the database for an open Liberal Ed
    or other requirement.

    Strategy:
      1. Map the requirement category to a CLE attribute value
      2. Query courses with that CLE attribute, ordered by difficulty preference
      3. Filter out completed and already-scheduled courses
      4. For timeline=asap, prefer 3-credit courses (fit more in a semester)
      5. Return up to max_results candidates

    Returns a list of course dicts ready to be added as candidates.
    """
    # Build set of completed course codes for filtering
    completed_codes = {
        f"{c['subject']} {c['number']}"
        for c in completed_courses
        if not c.get('is_withdrawn')
    }

    already_scheduled = scheduled_codes or set()

    order_clause = _candidate_order_clause(preferences)

    # Find which CLE values apply to this requirement
    cle_values = match_le_category(requirement_category)
    if not cle_values:
        return []

    conn = get_db_connection()
    cursor = conn.cursor()

    def to_candidate(row, cle_value):
        subject, number, title, credits, offered_fall, offered_spring = row
        return {
            "subject": subject,
            "number": number,
            "title": title,
            "credits": float(credits),
            "requirement_category": requirement_category,
            "is_pinned": False,
            "term_locked": None,
            "offered_fall": offered_fall,
            "offered_spring": offered_spring,
            "prereqs": [],
            "prereq_raw": "",
            "cle_value": cle_value,
        }

    try:
        # [None] is the Writing Intensive sentinel: WI is an attribute, not a
        # CLE value. Recommend upper-division (3xxx+) WI courses, restricted
        # to courses with no prerequisites so the recommendation is takeable
        # exactly as scheduled (the greedy scheduler doesn't know the prereq
        # chains of recommended courses).
        if cle_values == [None]:
            cursor.execute(f"""
                SELECT DISTINCT c.subject_code, c.catalog_number, c.title,
                       c.credits, c.offered_fall, c.offered_spring
                FROM courses c
                JOIN course_attributes ca ON c.id = ca.course_id
                WHERE ca.attribute = 'WI'
                  AND c.acad_career = 'UGRD'
                  AND c.credits IS NOT NULL
                  AND c.credits >= %s
                  AND c.credits <= {MAX_RECOMMENDED_CREDITS}
                  AND c.catalog_number >= '{WI_UPPER_DIVISION_THRESHOLD}'
                  AND c.catalog_number !~* '{RESTRICTED_SUFFIX_PATTERN}'
                  AND c.title !~* '{DIRECTED_STUDY_TITLE_PATTERN}'
                  AND (c.prereq_raw IS NULL OR c.prereq_raw = '')
                ORDER BY {order_clause}
                LIMIT 100
            """, (MIN_WI_CREDITS,))
            results = [
                to_candidate(row, None)
                for row in cursor.fetchall()
                if f"{row[0]} {row[1]}" not in completed_codes
                and f"{row[0]} {row[1]}" not in already_scheduled
            ]
            return results[:max_results]

        results = []
        for cle_value in cle_values:
            cursor.execute(f"""
                SELECT DISTINCT c.subject_code, c.catalog_number, c.title,
                       c.credits, c.offered_fall, c.offered_spring
                FROM courses c
                JOIN course_attributes ca ON c.id = ca.course_id
                WHERE ca.attribute = 'CLE'
                  AND ca.value = %s
                  AND c.acad_career = 'UGRD'
                  AND c.credits IS NOT NULL
                  AND c.credits >= %s
                  AND c.credits <= {MAX_RECOMMENDED_CREDITS}
                  AND c.catalog_number !~* '{RESTRICTED_SUFFIX_PATTERN}'
                  AND c.title !~* '{DIRECTED_STUDY_TITLE_PATTERN}'
                ORDER BY {order_clause}
                LIMIT 100
            """, (cle_value, min_credits_for_cle_value(cle_value)))

            for row in cursor.fetchall():
                code = f"{row[0]} {row[1]}"
                if code in completed_codes or code in already_scheduled:
                    continue
                results.append(to_candidate(row, cle_value))

            if results:
                break  # Found courses for first matching CLE value

        return results[:max_results]

    finally:
        cursor.close()
        conn.close()
# ---------------------------------------------------------------------------
# Prerequisite graph
# ---------------------------------------------------------------------------

class PrerequisiteGraph:
    """
    Builds a directed acyclic graph (DAG) of course prerequisites.

    Nodes are course codes (e.g. 'CSCI 4041').
    Edges point FROM prerequisite TO dependent course.
    Edge meaning: "you must complete the source before taking the target."

    Example:
        CSCI 1133 → CSCI 2081 → CSCI 3041 → CSCI 4041

    We use NetworkX's DiGraph. Topological sort gives us a valid
    ordering — any schedule that respects topological order is valid
    from a prerequisite standpoint.
    """

    def __init__(self):
        self.graph = nx.DiGraph()
        self._completed = set()

    def add_completed_courses(self, completed_courses):
        """
        Marks courses as completed. Completed courses are added as nodes
        but won't be scheduled — they satisfy prerequisites for future courses.
        """
        for course in completed_courses:
            if not course.get("is_withdrawn"):
                code = f"{course['subject']} {course['number']}"
                self.graph.add_node(code, completed=True,
                                   credits=course.get("credits", 0))
                self._completed.add(code)

    def add_course(self, subject, number, prereqs=None):
        """
        Adds a course to the graph with optional prerequisite edges.

        prereqs is a list of course codes that must be completed first.
        For now we use a simplified prereq model — full parsing via
        Claude API will be added in a later phase.
        """
        code = f"{subject} {number}"
        self.graph.add_node(code, completed=False)

        if prereqs:
            for prereq in prereqs:
                if prereq in self.graph.nodes:
                    self.graph.add_edge(prereq, code)

    def is_completed(self, subject, number):
        return f"{subject} {number}" in self._completed

    def get_available_courses(self, scheduled_so_far):
        """
        Returns courses whose prerequisites are all either completed
        or already scheduled in earlier semesters.

        scheduled_so_far: set of course codes already placed in the plan
        """
        satisfied = self._completed | scheduled_so_far
        available = []

        for node in self.graph.nodes:
            if node in satisfied:
                continue
            # Check all predecessors are satisfied
            predecessors = set(self.graph.predecessors(node))
            if predecessors.issubset(satisfied):
                available.append(node)

        return available


# ---------------------------------------------------------------------------
# Course candidate builder
# ---------------------------------------------------------------------------

def build_candidate_courses(parsed_apas, preferences=None, terms=None):
    """
    Builds the list of courses the optimizer needs to schedule.

    Categories:
      1. Pinned courses — specific courses required (from APAS options lists)
      2. In-progress courses — already scheduled for current term
      3. Open requirements — need course recommendations from DB

    Returns a list of candidate dicts, each with:
      - subject, number, title, credits
      - requirement_category (what requirement it satisfies)
      - is_pinned (True if a specific course is required)
      - term_locked (term code if already scheduled, else None)
      - prereq_raw (raw prereq text, used for AND/OR logic in greedy_schedule)
    """
    if terms is None:
        terms = TERMS
    first_term_code = terms[0]["code"]

    candidates = []
    candidate_by_code = {}  # code -> candidate dict (for crediting double-dips)
    in_progress = {f"{c['subject']} {c['number']}"
                   for c in parsed_apas.get("in_progress_courses", [])}
    completed_codes = {
        f"{c['subject']} {c['number']}"
        for c in parsed_apas.get("completed_courses", [])
        if not c.get("is_withdrawn")
    }

    # Add in-progress courses as locked to the first plannable term.
    # Dedup by code: the APAS parser can list the same course twice (student
    # 22's MATH 2263), which would otherwise double-count its credits.
    for course in parsed_apas.get("in_progress_courses", []):
        code = f"{course['subject']} {course['number']}"
        if code in candidate_by_code:
            continue
        db_course = get_course_from_db(course["subject"], course["number"])
        cand = {
            "subject": course["subject"],
            "number": course["number"],
            "title": course.get("title", ""),
            "credits": course.get("credits", 3.0),
            "requirement_category": "In Progress",
            "satisfies_categories": ["In Progress"],
            "is_pinned": True,
            "term_locked": first_term_code,
            "offered_fall": True,
            "offered_spring": True,
            "prereqs": [],
            "prereq_raw": db_course["prereq_raw"] if db_course else "",
        }
        candidates.append(cand)
        candidate_by_code[code] = cand

    # Phase 1: pinned requirements (explicit options lists). Open
    # requirements are collected in order and handled in phases 2-3 below,
    # so the double-dip pass can see the full pinned candidate pool.
    open_requirements = []
    for req in parsed_apas.get("remaining_requirements", []):
        category = req["category"]
        options = req.get("options", [])
        credits_needed = req.get("credits_needed")

        if any(keyword.lower() in category.lower() for keyword in SKIP_META_KEYWORDS):
            continue

        # Skip parent/group header rows explicitly — their children are
        # scheduled as separate requirements (see is_parent_header_requirement)
        if is_parent_header_requirement(req, parsed_apas.get("remaining_requirements", [])):
            continue

        if not options:
            open_requirements.append(req)
            continue

        # Add enough courses from the options list to cover credits_needed.
        # For single-course requirements (e.g. STAT 5102, credits_needed=4)
        # this adds exactly 1 course and stops. For multi-credit requirements
        # (e.g. Technical Electives, credits_needed=14) it adds ~4-5 courses.
        credits_to_fill = float(credits_needed) if credits_needed else 0.0
        added_this_req: set = set()

        for option in options:
            # Stop once credits are covered (always add at least one course)
            if credits_to_fill <= 0 and added_this_req:
                break

            parts = option.strip().split()
            if len(parts) < 2:
                continue
            subject = parts[0]
            number = " ".join(parts[1:])
            code = f"{subject} {number}"

            if is_restricted_section(number):
                continue  # skip Honors/honors-variant sections in requirements options lists

            if code in added_this_req:
                continue

            # Already enrolled in this option: don't re-add it. Credit this
            # requirement to the in-progress course (it will be scheduled in
            # the first term) so coverage reflects that it's satisfied.
            if code in in_progress:
                ip = candidate_by_code.get(code)
                if ip and category not in ip["satisfies_categories"]:
                    ip["satisfies_categories"].append(category)
                added_this_req.add(code)
                continue

            # Already completed this option — the requirement is satisfied by
            # prior coursework; nothing to schedule.
            if code in completed_codes:
                added_this_req.add(code)
                continue

            db_course = get_course_from_db(subject, number)
            if db_course is None:
                # Major-specific course not in our DB — fall back to APAS data
                # so it still appears in the plan/unscheduled, never dropped.
                print(f"[optimizer] Course {subject} {number} not found in "
                      f"database — using APAS data as fallback", file=sys.stderr)
            prereqs = extract_simple_prereqs(subject, number)
            course_credits = db_course["credits"] if db_course else 3.0

            cand = {
                "subject": subject,
                "number": number,
                "title": db_course["title"] if db_course else option,
                "credits": course_credits,
                "requirement_category": category,
                "is_pinned": True,
                "term_locked": None,
                "offered_fall": db_course["offered_fall"] if db_course else True,
                "offered_spring": db_course["offered_spring"] if db_course else True,
                "offered_summer": db_course["offered_summer"] if db_course else True,
                "prereqs": prereqs,
                "prereq_raw": db_course["prereq_raw"] if db_course else "",
            }
            candidates.append(cand)
            candidate_by_code.setdefault(code, cand)
            added_this_req.add(code)
            credits_to_fill -= course_credits

    # Phase 2: proactive double-dip across open Liberal Ed requirements.
    # One course tagged for both an open Core and an open Theme satisfies
    # two requirements with one course's credits, freeing schedule room.
    completed = parsed_apas.get("completed_courses", [])
    covered_by_multi = set()
    cle_open = []
    for req in open_requirements:
        vals = match_le_category(req["category"])
        if vals and vals != [None]:
            cle_open.append((req["category"], vals))

    if len(cle_open) >= 2:
        already_added = {f"{c['subject']} {c['number']}" for c in candidates}
        for cand in recommend_multi_tag_courses(
            cle_open, completed, preferences=preferences,
            scheduled_codes=already_added,
        ):
            # Accept only if it covers two still-uncovered requirements
            new_cats = [cat for cat in cand["satisfies_categories"]
                        if cat not in covered_by_multi]
            if len(new_cats) < 2:
                continue
            candidates.append(cand)
            covered_by_multi.update(new_cats)
            already_added.add(f"{cand['subject']} {cand['number']}")

    # Phase 3: remaining open requirements — recommend single courses,
    # respecting difficulty/timeline preferences and avoiding courses
    # already chosen for other requirements
    for req in open_requirements:
        category = req["category"]
        credits_needed = req.get("credits_needed")
        if category in covered_by_multi:
            continue

        already_added = {
            f"{c['subject']} {c['number']}" for c in candidates
        }
        recommended = recommend_courses_for_requirement(
            category,
            completed,
            preferences=preferences,
            scheduled_codes=already_added,
        )

        if recommended:
            # Most open requirements need a single course; the bare
            # Writing Intensive parent requirement covers multiple
            # remaining WI courses (see WI_ADDITIONAL_COURSES). But a
            # double-counting WI course already in the plan (e.g.
            # WRIT 3562W counts as 2) reduces the additional courses needed.
            if is_wi_parent_requirement(category):
                added_codes = {f"{c['subject']} {c['number']}" for c in candidates}
                n_needed = (
                    1
                    if added_codes & DOUBLE_COUNT_WI_COURSES
                    else WI_ADDITIONAL_COURSES
                )
            else:
                n_needed = 1
            candidates.extend(recommended[:n_needed])
        elif credits_needed and credits_needed > 0:
            # Fallback to placeholder if no recommendation found
            candidates.append({
                "subject": "TBD",
                "number": "0000",
                "title": f"Course for: {category}",
                "credits": credits_needed,
                "requirement_category": category,
                "is_pinned": False,
                "term_locked": None,
                "offered_fall": True,
                "offered_spring": True,
                "prereqs": [],
            })

    return candidates


# ---------------------------------------------------------------------------
# Simple greedy scheduler
# ---------------------------------------------------------------------------

def prereqs_satisfied(prereqs, prereq_raw, satisfied):
    """
    Returns True if a course's prerequisites are met by the `satisfied` set
    of course codes (completed + scheduled in earlier terms).

    extract_simple_prereqs flattens all course codes from the raw text
    into one list, losing the nested (A or B) and (C or D) structure.
    all() is safe only when the prereq is a flat AND with no OR groups
    (e.g. "prereq: STAT 5101 and STAT 5102"). As soon as "or" appears
    the flattened list can never satisfy all() correctly, so fall back
    to any() — the student is expected to have met at least one prereq
    path, and the disclaimer covers advisor verification.
    """
    if not prereqs:
        return True
    resolved = [COURSE_ALIASES.get(p, p) for p in prereqs]
    raw = (prereq_raw or "").lower()
    has_and = " and " in raw
    has_or = " or " in raw
    if len(resolved) > 1 and has_and and not has_or:
        return all(prereq in satisfied for prereq in resolved)
    return any(prereq in satisfied for prereq in resolved)


def greedy_schedule(candidates, completed_courses, preferences=None, terms=None):
    """
    Assigns courses to semesters using a greedy algorithm.

    We use greedy here rather than OR-Tools for the first iteration
    because it's easier to debug and reason about. OR-Tools will replace
    this for the full optimizer once the data model is solid.

    Algorithm:
      1. Lock in-progress courses to the first term
      2. For each remaining term, fill up to max_credits with
         courses that are available (prereqs satisfied) and
         offered in that term
      3. Prefer pinned courses over open requirements
      4. Stop when all requirements are scheduled or we run out of terms

    Returns a dict mapping term_code → list of scheduled courses.
    """
    if preferences is None:
        preferences = {}
    if terms is None:
        terms = TERMS
    first_term_code = terms[0]["code"]

    difficulty = preferences.get("difficulty", "any")
    timeline = preferences.get("timeline", "on_time")
    # Explicit max_credits_per_semester wins; otherwise derive the cap from
    # timeline so the preference has an effect when optimize_plan is called
    # directly (asap=18, on_time=16).
    max_credits = preferences.get("max_credits_per_semester")
    if max_credits is None:
        max_credits = ASAP_MAX_CREDITS if timeline == "asap" else DEFAULT_MAX_CREDITS

    # Build prerequisite graph
    graph = PrerequisiteGraph()
    graph.add_completed_courses(completed_courses)

    # Add all candidates to graph
    for c in candidates:
        if c["subject"] != "TBD":
            graph.add_course(c["subject"], c["number"])

    # Build lookup by code
    candidate_map = {}
    for c in candidates:
        code = f"{c['subject']} {c['number']}"
        candidate_map[code] = c

    plan = {term["code"]: [] for term in terms}
    # Initialize scheduled with all completed courses
    # so prereq checks correctly recognize already-finished coursework
    scheduled = set()
    for course in completed_courses:
        if not course.get("is_withdrawn"):
            code = f"{course['subject']} {course['number']}"
            scheduled.add(code)

    # First pass: lock in-progress courses to the first term.
    # Note: in-progress courses are added to the plan but NOT to scheduled yet.
    # This prevents other courses from treating them as completed prerequisites
    # in the same term. They get added to scheduled after the first term is done.
    in_progress_codes = set()
    for c in candidates:
        if c.get("term_locked") == first_term_code:
            plan[first_term_code].append(c)
            in_progress_codes.add(f"{c['subject']} {c['number']}")

    # Second pass: schedule remaining courses greedily
    for term in terms:
        if term["code"] == first_term_code:
            # Already handled in-progress, but can add more
            current_credits = sum(c["credits"] for c in plan[first_term_code])
            scheduled.update(in_progress_codes)
        else:
            current_credits = 0

        # Get unscheduled candidates available this term
        available = []
        for c in candidates:
            code = f"{c['subject']} {c['number']}"
            if code in scheduled:
                continue
            if c.get("term_locked") and c["term_locked"] != term["code"]:
                continue

            # Check offering frequency for this term's season. Default True
            # (allow when unknown), consistent with fall/spring — the
            # disclaimer covers advisor verification.
            if term.get("is_summer"):
                if not c.get("offered_summer", True):
                    continue
            elif term["is_fall"]:
                if not c.get("offered_fall", True):
                    continue
            else:
                if not c.get("offered_spring", True):
                    continue

            # Check prerequisites (see prereqs_satisfied for AND/OR semantics)
            if not prereqs_satisfied(c.get("prereqs", []), c.get("prereq_raw"), scheduled):
                continue
            # Don't schedule a course in the first term if its prereq is also
            # in progress in the first term
            if term["code"] == first_term_code:
                prereqs_in_progress = any(
                    prereq in in_progress_codes
                    for prereq in c.get("prereqs", [])
                )
                if prereqs_in_progress:
                    continue

            available.append(c)

        # Sort based on preferences
        # Difficulty: easy = prefer lower course numbers (1xxx/2xxx)
        # Hard = prefer higher course numbers (4xxx/5xxx)
        def sort_key(c):
            is_pinned = not c["is_pinned"]
            # Multi-tag (double-dip) candidates satisfy more requirements
            # per credit — schedule them ahead of single-tag picks
            coverage = -len(c.get("satisfies_categories")
                            or [c["requirement_category"]])
            num = ''.join(filter(str.isdigit, c["number"]))
            level = int(num[0]) if num else 3

            if difficulty == "easy":
                level_score = level       # lower level = better
            elif difficulty == "hard":
                level_score = -level      # higher level = better
            else:
                level_score = 0

            return (is_pinned, coverage, level_score, -c["credits"])

        available.sort(key=sort_key)

        # Remove courses whose prerequisites are in the same term
        # e.g. STAT 5102 can't be in the same term as STAT 5101
        this_term_codes = {f"{c['subject']} {c['number']}"
                          for c in plan[term["code"]]}
        available = [
            c for c in available
            if f"{c['subject']} {c['number']}" not in this_term_codes
        ]

        for c in available:
            code = f"{c['subject']} {c['number']}"
            # Dedup guard: a course that satisfies multiple requirements has
            # one candidate dict per requirement (a legitimate "double-dip").
            # Schedule it only once; coverage for the other requirements is
            # tracked via satisfies_categories below.
            if code in scheduled:
                continue
            if current_credits + c["credits"] <= max_credits:
                plan[term["code"]].append(c)
                scheduled.add(code)
                current_credits += c["credits"]

    # Record every requirement category each course code can satisfy, so a
    # single scheduled course reflects all requirements it double-dips for.
    # Candidates from the proactive multi-tag pass already carry their own
    # satisfies_categories; merge those in too.
    code_to_categories = {}
    for c in candidates:
        code = f"{c['subject']} {c['number']}"
        cats = code_to_categories.setdefault(code, [])
        for cat in c.get("satisfies_categories") or [c["requirement_category"]]:
            if cat not in cats:
                cats.append(cat)
    for term_courses in plan.values():
        for c in term_courses:
            code = f"{c['subject']} {c['number']}"
            c["satisfies_categories"] = code_to_categories.get(
                code, [c["requirement_category"]]
            )

    # Identify unscheduled requirements (one entry per code; double-dipped
    # codes are scheduled, so they never appear here)
    unscheduled = []
    seen_unscheduled = set()
    for c in candidates:
        code = f"{c['subject']} {c['number']}"
        if code not in scheduled and code not in seen_unscheduled:
            seen_unscheduled.add(code)
            unscheduled.append(c)

    return plan, unscheduled


# ---------------------------------------------------------------------------
# Claude-powered free-text preference reordering
# ---------------------------------------------------------------------------

def _reorder_candidates_by_free_text(candidates, free_text):
    """
    Asks Claude to reorder open-requirement candidates to best match the
    student's free-text preference.

    Only open (non-pinned) candidates are reordered; pinned courses stay in
    place.  Fails gracefully — any exception returns the original list.
    """
    open_candidates = [c for c in candidates if not c.get("is_pinned")]
    pinned_candidates = [c for c in candidates if c.get("is_pinned")]

    if not open_candidates or not (free_text or "").strip():
        return candidates

    course_lines = "\n".join(
        f"- {c['subject']} {c['number']}: {c['title']} "
        f"({c['requirement_category']}, {c['credits']}cr)"
        for c in open_candidates
    )

    try:
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": (
                    f'A University of Minnesota student said: "{free_text}"\n\n'
                    f"Available elective/requirement courses to schedule:\n{course_lines}\n\n"
                    "Return ONLY a JSON array of course codes in your recommended order "
                    "(most aligned with the student's preference first), "
                    'e.g. ["PSYC 1001", "MATH 1271"]. '
                    "Include every code from the list above. No explanation."
                ),
            }],
        )

        text_block = next(
            (b.text for b in response.content if b.type == "text"), None
        )
        if not text_block:
            return candidates

        match = re.search(r'\[.*?\]', text_block, re.DOTALL)
        if not match:
            return candidates

        preferred_codes = json.loads(match.group())

        code_to_open = {f"{c['subject']} {c['number']}": c for c in open_candidates}
        reordered_open = []
        seen = set()

        for code in preferred_codes:
            if code in code_to_open and code not in seen:
                reordered_open.append(code_to_open[code])
                seen.add(code)

        # Append any open candidates Claude didn't mention (preserve original order)
        for c in open_candidates:
            code = f"{c['subject']} {c['number']}"
            if code not in seen:
                reordered_open.append(c)

        return pinned_candidates + reordered_open

    except Exception as exc:
        print(f"[optimizer] Claude free_text reordering failed: {exc}", file=sys.stderr)
        return candidates


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def optimize_plan(parsed_apas, preferences=None):
    """
    Main optimizer entry point.

    Takes parsed APAS JSON and optional preferences dict.
    Returns a structured plan dict.

    This is what the FastAPI endpoint will call.
    """
    if preferences is None:
        preferences = {}

    # Derive the plannable terms from the student's expected graduation date,
    # so the plan ends at the right term for any student (not a fixed F26-SP28).
    terms = build_terms(parsed_apas.get("student", {}).get("expected_graduation"))

    completed_courses = [c for c in parsed_apas.get("completed_courses", [])
                        if not c.get("is_withdrawn")]

    # Build candidate courses to schedule, respecting difficulty/timeline prefs
    candidates = build_candidate_courses(parsed_apas, preferences=preferences,
                                         terms=terms)

    print(f"Built {len(candidates)} candidate courses to schedule.",
          file=sys.stderr)

    # Reorder open-requirement candidates using Claude if the student left a
    # free-text note.  Fails gracefully — plan still generates on any error.
    free_text = preferences.get("free_text", "").strip()
    if free_text:
        candidates = _reorder_candidates_by_free_text(candidates, free_text)

    # Run greedy scheduler
    plan, unscheduled = greedy_schedule(candidates, completed_courses,
                                        preferences, terms=terms)

    # Format output
    formatted_plan = []
    total_scheduled_credits = 0

    for term in terms:
        term_courses = plan[term["code"]]
        term_credits = sum(c["credits"] for c in term_courses)
        total_scheduled_credits += term_credits

        formatted_plan.append({
            "term_code": term["code"],
            "term_label": term["label"],
            "courses": [
                {
                    "subject": c["subject"],
                    "number": c["number"],
                    "title": c["title"],
                    "credits": c["credits"],
                    "requirement_category": c["requirement_category"],
                    "satisfies_categories": c.get(
                        "satisfies_categories", [c["requirement_category"]]
                    ),
                    "is_pinned": c["is_pinned"],
                }
                for c in term_courses
            ],
            "total_credits": term_credits,
        })

    return {
        "status": "complete" if not unscheduled else "partial",
        "plan": formatted_plan,
        "total_scheduled_credits": total_scheduled_credits,
        "unscheduled": [
            {
                "subject": c["subject"],
                "number": c["number"],
                "title": c["title"],
                "requirement_category": c["requirement_category"],
                "satisfies_categories": c.get(
                    "satisfies_categories", [c["requirement_category"]]
                ),
            }
            for c in unscheduled
        ],
        "message": (
            "Complete plan generated."
            if not unscheduled
            else f"{len(unscheduled)} requirements could not be scheduled. "
                 f"Consider adjusting credit limits or graduation date."
        )
    }


# ---------------------------------------------------------------------------
# CLI test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json

    print("Loading parsed APAS...", file=sys.stderr)
    data = json.load(open("parsed_apas.json"))

    print("Running optimizer...", file=sys.stderr)
    result = optimize_plan(data)

    print(json.dumps(result, indent=2))