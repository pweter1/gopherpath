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
import sys
import json
import networkx as nx
import psycopg2
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

def build_candidate_courses(parsed_apas):
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
        })

    # Process remaining requirements
    for req in parsed_apas.get("remaining_requirements", []):
        category = req["category"]
        options = req.get("options", [])
        credits_needed = req.get("credits_needed")

        # Skip meta-requirements (total credit counts)
        if category in ["University Credit and GPA Requirements",
                        "Major Requirements - Minimum Major Credits"]:
            continue

        if options:
            # Pinned: specific courses required
            # Take the first valid option for now
            # Full preference-based selection comes in a later phase
            for option in options[:1]:
                parts = option.strip().split()
                if len(parts) >= 2:
                    subject = parts[0]
                    number = " ".join(parts[1:])
                    code = f"{subject} {number}"

                    if code in in_progress:
                        continue

                    db_course = get_course_from_db(subject, number)
                    prereqs = extract_simple_prereqs(subject, number)
                    candidates.append({
                        "subject": subject,
                        "number": number,
                        "title": db_course["title"] if db_course else option,
                        "credits": db_course["credits"] if db_course else (credits_needed or 3.0),
                        "requirement_category": category,
                        "is_pinned": True,
                        "term_locked": None,
                        "offered_fall": db_course["offered_fall"] if db_course else True,
                        "offered_spring": db_course["offered_spring"] if db_course else True,
                        "prereqs": prereqs,
                    })
        else:
            # Open requirement — we need to recommend a course
            # For now, add a placeholder. Full recommendation engine
            # uses Claude API + DB query in the next iteration.
            if credits_needed and credits_needed > 0:
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
                })

    return candidates


# ---------------------------------------------------------------------------
# Simple greedy scheduler
# ---------------------------------------------------------------------------

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

    max_credits = preferences.get("max_credits_per_semester", DEFAULT_MAX_CREDITS)

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

            # Check prerequisites — satisfied if ANY prereq is met
            # Resolve known course aliases before checking
            course_prereqs = c.get("prereqs", [])
            if course_prereqs:
                resolved_prereqs = [
                    COURSE_ALIASES.get(p, p) for p in course_prereqs
                ]
                prereqs_met = any(prereq in scheduled for prereq in resolved_prereqs)
                if not prereqs_met:
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

        # Sort: pinned courses first, then by credits descending
        available.sort(key=lambda x: (not x["is_pinned"], -x["credits"]))

        # Remove courses whose prerequisites are in the same term
        # e.g. STAT 5102 can't be in the same term as STAT 5101
        this_term_codes = {f"{c['subject']} {c['number']}"
                          for c in plan[term["code"]]}
        available = [
            c for c in available
            if f"{c['subject']} {c['number']}" not in this_term_codes
        ]

        for c in available:
            if current_credits + c["credits"] <= max_credits:
                plan[term["code"]].append(c)
                scheduled.add(f"{c['subject']} {c['number']}")
                current_credits += c["credits"]

    # Identify unscheduled requirements
    unscheduled = []
    for c in candidates:
        code = f"{c['subject']} {c['number']}"
        if code not in scheduled:
            unscheduled.append(c)

    return plan, unscheduled


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

    # Build candidate courses to schedule
    candidates = build_candidate_courses(parsed_apas)

    print(f"Built {len(candidates)} candidate courses to schedule.",
          file=sys.stderr)

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