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
import networkx as nx
import psycopg2
import anthropic
from dotenv import load_dotenv
from ortools.sat.python import cp_model

load_dotenv()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Term codes map to human-readable labels and sequence numbers
# Sequence number determines ordering (lower = earlier)
TERMS = [
    {"code": "F26",  "label": "Fall 2026",   "seq": 0, "is_fall": True},
    {"code": "SP27", "label": "Spring 2027", "seq": 1, "is_fall": False},
    {"code": "F27",  "label": "Fall 2027",   "seq": 2, "is_fall": True},
    {"code": "SP28", "label": "Spring 2028", "seq": 3, "is_fall": False},
]

# Course aliases — some courses appear under different numbers in prereq
# text vs what students actually take. This mapping handles known cases.
# Key: number as it appears in prereq text
# Value: number as it appears in transcripts/our database
COURSE_ALIASES = {
    "CSCI 3081W": "CSCI 3061",
    "CSCI 3081":  "CSCI 3061",
    "CSCI 4061":  "CSCI 3061",  # old number for Computer Systems
}

DEFAULT_MAX_CREDITS = 16
DEFAULT_MIN_CREDITS = 12
# timeline="asap" raises the per-semester cap so more courses fit each term
# (mirrors the value main.py's /optimize endpoint passes for asap)
ASAP_MAX_CREDITS = 18

# Meta-requirements (total credit counts, GPA requirements) — these are
# degree-level checks, not schedulable courses
SKIP_META_KEYWORDS = [
    "Minimum Degree Credits",
    "Minimum Major Credits",
    "Major GPA",
    "University Credit",
    "GPA Requirements",
    "S Grade Credit",
    "Credits Completed",
    "Degree-Applicable",
    "Elective Credits",
]


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

# Map APAS requirement category names to our CLE attribute values
LE_MAPPING = {
    'Diversified Core - Biological & Physical Sciences': [
        'Biological Sciences', 'Physical Sciences'
    ],
    'Diversified Core - Historical Perspectives & Social Sciences': [
        'Historical Perspectives', 'Social Sciences'
    ],
    'Diversified Core - Arts/Humanities & Literature': [
        'Arts/Humanities', 'Literature'
    ],
    'Designated Themes - Civic Life and Ethics': [
        'Civic Life and Ethics'
    ],
    'Designated Themes - Global Perspectives': [
        'Global Perspectives'
    ],
    'Designated Themes - Technology and Society': [
        'Technology and Society'
    ],
    'Designated Themes - The Environment': [
        'The Environment'
    ],
    'Diversified Core - Mathematical Thinking': [
        'Mathematical Thinking'
    ],
    'Designated Themes - Race, Power, and Justice': [
        'Race Power and Justice'
    ],
    'Writing Intensive': [None],  # handled separately via WI attribute
}

# UMN Liberal Education guidelines (curricularhub.umn.edu): a course must
# "be at least 3 credits (or at least 4 credits for biological or physical
# sciences, which must include a lab or field experience component)" to
# carry an LE designation. Recommending below-threshold courses would tell
# students a requirement is satisfied when it isn't.
MIN_LE_COURSE_CREDITS = 3.0
MIN_BIO_PHYS_COURSE_CREDITS = 4.0
BIO_PHYS_CLE_VALUES = {"Biological Sciences", "Physical Sciences"}


def min_credits_for_cle_value(cle_value):
    """Per-course credit minimum for a CLE attribute value."""
    if cle_value in BIO_PHYS_CLE_VALUES:
        return MIN_BIO_PHYS_COURSE_CREDITS
    return MIN_LE_COURSE_CREDITS


# UMN writing requirement (onestop.umn.edu): four WI courses total, two of
# them upper-division (3xxx+), one of those within the major. The APAS lists
# the in-major upper-division WI as its own sub-requirement with explicit
# course options; the bare parent "Writing Intensive" row carries no credit
# count, so we schedule this many additional upper-division WI courses for it.
WI_ADDITIONAL_COURSES = 2


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
    prefix = category + " - "
    return any(
        other.get("category", "").startswith(prefix)
        for other in all_requirements
        if other is not req
    )


def match_le_category(requirement_category):
    """
    Maps an APAS requirement category to its CLE attribute values.

    The Claude-based APAS parser is not consistent about parent-group
    prefixes: the same requirement can parse as
    'Diversified Core - Biological & Physical Sciences' in one upload and
    'Liberal Education - Diversified Core - Biological & Physical Sciences'
    in the next. Exact lookup first, then suffix match to absorb any prefix.

    Returns the CLE value list, or None if the category isn't a Liberal Ed
    requirement we can query.
    """
    values = LE_MAPPING.get(requirement_category)
    if values:
        return values
    for key, vals in LE_MAPPING.items():
        if requirement_category.endswith(key):
            return vals
    return None


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

    # Determine ORDER BY from preferences
    pref = preferences or {}
    difficulty = pref.get("difficulty", "any")
    timeline = pref.get("timeline", "on_time")

    # difficulty="hard" → higher catalog numbers first (4xxx/5xxx)
    # difficulty="easy" or any other → lower numbers first (1xxx/2xxx)
    catalog_dir = "DESC" if difficulty == "hard" else "ASC"
    # timeline="asap" → prefer 3-credit courses so more fit per semester
    # Tie-breakers (subject_code, title) make the ordering total — catalog
    # numbers repeat across subjects, and Postgres returns tied rows in
    # arbitrary order otherwise.
    # safe: catalog_dir is only ever "ASC" or "DESC"
    if timeline == "asap":
        order_clause = f"c.credits ASC, c.catalog_number {catalog_dir}, c.subject_code ASC, c.title ASC"
    else:
        order_clause = f"c.catalog_number {catalog_dir}, c.subject_code ASC, c.title ASC"

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
                  AND c.credits <= 4
                  AND c.catalog_number >= '3000'
                  AND c.catalog_number !~* 'H$'
                  AND (c.prereq_raw IS NULL OR c.prereq_raw = '')
                ORDER BY {order_clause}
                LIMIT 100
            """)
            results = [
                to_candidate(row, None)
                for row in cursor.fetchall()
                if f"{row[0]} {row[1]}" not in completed_codes
                and f"{row[0]} {row[1]}" not in already_scheduled
            ]
            return results[:max_results]

        results = []
        for cle_value in cle_values:
            # catalog_number ending in H = Honors-restricted section; we don't
            # track Honors program membership, so never recommend these.
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
                  AND c.credits <= 4
                  AND c.catalog_number !~* 'H$'
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

def build_candidate_courses(parsed_apas, preferences=None):
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
    candidates = []
    in_progress = {f"{c['subject']} {c['number']}"
                   for c in parsed_apas.get("in_progress_courses", [])}

    # Add in-progress courses as locked to F26
    for course in parsed_apas.get("in_progress_courses", []):
        db_course = get_course_from_db(course["subject"], course["number"])
        candidates.append({
            "subject": course["subject"],
            "number": course["number"],
            "title": course.get("title", ""),
            "credits": course.get("credits", 3.0),
            "requirement_category": "In Progress",
            "is_pinned": True,
            "term_locked": "F26",
            "offered_fall": True,
            "offered_spring": True,
            "prereqs": [],
            "prereq_raw": db_course["prereq_raw"] if db_course else "",
        })

    # Process remaining requirements
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

        if options:
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

                if code in in_progress or code in added_this_req:
                    continue

                db_course = get_course_from_db(subject, number)
                prereqs = extract_simple_prereqs(subject, number)
                course_credits = db_course["credits"] if db_course else 3.0

                candidates.append({
                    "subject": subject,
                    "number": number,
                    "title": db_course["title"] if db_course else option,
                    "credits": course_credits,
                    "requirement_category": category,
                    "is_pinned": True,
                    "term_locked": None,
                    "offered_fall": db_course["offered_fall"] if db_course else True,
                    "offered_spring": db_course["offered_spring"] if db_course else True,
                    "prereqs": prereqs,
                    "prereq_raw": db_course["prereq_raw"] if db_course else "",
                })
                added_this_req.add(code)
                credits_to_fill -= course_credits
        else:
            # Open requirement — recommend real courses from database,
            # respecting difficulty/timeline preferences and avoiding
            # courses already chosen for earlier requirements
            already_added = {
                f"{c['subject']} {c['number']}" for c in candidates
            }
            recommended = recommend_courses_for_requirement(
                category,
                parsed_apas.get("completed_courses", []),
                preferences=preferences,
                scheduled_codes=already_added,
            )

            if recommended:
                # Most open requirements need a single course; the bare
                # Writing Intensive parent requirement covers multiple
                # remaining WI courses (see WI_ADDITIONAL_COURSES)
                n_needed = (
                    WI_ADDITIONAL_COURSES
                    if is_wi_parent_requirement(category)
                    else 1
                )
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


def greedy_schedule(candidates, completed_courses, preferences=None):
    """
    Assigns courses to semesters using a greedy algorithm.

    We use greedy here rather than OR-Tools for the first iteration
    because it's easier to debug and reason about. OR-Tools will replace
    this for the full optimizer once the data model is solid.

    Algorithm:
      1. Lock in-progress courses to F26
      2. For each remaining term, fill up to max_credits with
         courses that are available (prereqs satisfied) and
         offered in that term
      3. Prefer pinned courses over open requirements
      4. Stop when all requirements are scheduled or we run out of terms

    Returns a dict mapping term_code → list of scheduled courses.
    """
    if preferences is None:
        preferences = {}

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

    plan = {term["code"]: [] for term in TERMS}
    # Initialize scheduled with all completed courses
    # so prereq checks correctly recognize already-finished coursework
    scheduled = set()
    for course in completed_courses:
        if not course.get("is_withdrawn"):
            code = f"{course['subject']} {course['number']}"
            scheduled.add(code)

    # First pass: lock in-progress courses to F26
    # Note: in-progress courses are added to the plan but NOT to scheduled yet.
    # This prevents other courses from treating them as completed prerequisites
    # in the same term. They get added to scheduled after F26 is processed.
    in_progress_codes = set()
    for c in candidates:
        if c.get("term_locked") == "F26":
            plan["F26"].append(c)
            in_progress_codes.add(f"{c['subject']} {c['number']}")

    # Second pass: schedule remaining courses greedily
    for term in TERMS:
        if term["code"] == "F26":
            # Already handled in-progress, but can add more
            current_credits = sum(c["credits"] for c in plan["F26"])
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

            # Check offering frequency
            if term["is_fall"] and not c.get("offered_fall", True):
                continue
            if not term["is_fall"] and not c.get("offered_spring", True):
                continue

            # Check prerequisites (see prereqs_satisfied for AND/OR semantics)
            if not prereqs_satisfied(c.get("prereqs", []), c.get("prereq_raw"), scheduled):
                continue
            # Don't schedule a course in F26 if its prereq is also in F26
            if term["code"] == "F26":
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
            num = ''.join(filter(str.isdigit, c["number"]))
            level = int(num[0]) if num else 3

            if difficulty == "easy":
                level_score = level       # lower level = better
            elif difficulty == "hard":
                level_score = -level      # higher level = better
            else:
                level_score = 0

            return (is_pinned, level_score, -c["credits"])

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
    code_to_categories = {}
    for c in candidates:
        code = f"{c['subject']} {c['number']}"
        cats = code_to_categories.setdefault(code, [])
        if c["requirement_category"] not in cats:
            cats.append(c["requirement_category"])
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
            model="claude-opus-4-8",
            max_tokens=512,
            thinking={"type": "adaptive"},
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

    completed_courses = [c for c in parsed_apas.get("completed_courses", [])
                        if not c.get("is_withdrawn")]

    # Build candidate courses to schedule, respecting difficulty/timeline prefs
    candidates = build_candidate_courses(parsed_apas, preferences=preferences)

    print(f"Built {len(candidates)} candidate courses to schedule.",
          file=sys.stderr)

    # Reorder open-requirement candidates using Claude if the student left a
    # free-text note.  Fails gracefully — plan still generates on any error.
    free_text = preferences.get("free_text", "").strip()
    if free_text:
        candidates = _reorder_candidates_by_free_text(candidates, free_text)

    # Run greedy scheduler
    plan, unscheduled = greedy_schedule(candidates, completed_courses,
                                        preferences)

    # Format output
    formatted_plan = []
    total_scheduled_credits = 0

    for term in TERMS:
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