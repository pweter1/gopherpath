"""
main.py
-------
GopherPath FastAPI backend.

Run locally with:
    uvicorn backend.main:app --reload
"""

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional
import tempfile
import os
import sys
import hashlib
import secrets
import json
import psycopg2
import anthropic
from dotenv import load_dotenv


load_dotenv()

try:
    from backend.policy import POLICY
except ImportError:
    from policy import POLICY

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scrapers.parse_apas import parse_apas, validate_parsed_apas

class Preferences(BaseModel):
    difficulty: str = "any"          # "easy", "medium", "hard", "any"
    timeline: str = "on_time"        # "asap", "on_time"
    max_credits: Optional[int] = None
    free_text: Optional[str] = None  # raw student input


class ChatMessage(BaseModel):
    role: str   # "user" or "assistant"
    content: str


class ChatRequest(BaseModel):
    message: str = ""
    history: list[ChatMessage] = []
    is_opening: bool = False
# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(
    title="GopherPath API",
    description="AI-powered academic course planner for UMN students",
    version="0.1.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:3001"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_db_connection():
    return psycopg2.connect(os.getenv("DATABASE_URL"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def generate_session_token():
    """
    Generates a cryptographically secure random token.
    This token identifies the student's session and is shared via URL.
    It is not a login — it's a shareable plan identifier.
    secrets.token_urlsafe is the correct function for this use case:
    it uses os.urandom() under the hood and produces URL-safe base64.
    """
    return secrets.token_urlsafe(32)


def hash_student_id(student_id):
    """
    Hashes the student ID before storing it.
    We never store raw student IDs — only a SHA-256 hash.
    This means we can check if a student has uploaded before
    without storing personally identifiable information.
    """
    if not student_id:
        return None
    return hashlib.sha256(student_id.encode()).hexdigest()


def save_student_to_db(parsed_data):
    """
    Saves parsed APAS data to the students table.
    Returns the session token for this student.

    We store:
      - Key fields as typed columns (for querying)
      - The full parsed JSON in parsed_apas_json (for flexibility)

    The JSONB column means we can query into the JSON later if needed,
    e.g. SELECT * FROM students WHERE parsed_apas_json->'credits'->>'earned' > '80'
    """
    token = generate_session_token()
    student = parsed_data.get("student", {})
    credits = parsed_data.get("credits", {})
    gpa = parsed_data.get("gpa", {})

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute("""
            INSERT INTO students (
                session_token, name, student_id_hash,
                major, college, catalog_year,
                expected_graduation, advisor,
                credits_earned, credits_in_progress,
                credits_needed, credits_total_required,
                gpa_overall, gpa_major,
                parsed_apas_json
            )
            VALUES (
                %s, %s, %s,
                %s, %s, %s,
                %s, %s,
                %s, %s,
                %s, %s,
                %s, %s,
                %s
            )
            RETURNING id
        """, (
            token,
            student.get("name"),
            hash_student_id(student.get("student_id")),
            student.get("major"),
            student.get("college"),
            student.get("catalog_year"),
            student.get("expected_graduation"),
            student.get("advisor"),
            credits.get("earned"),
            credits.get("in_progress"),
            credits.get("needed"),
            credits.get("total_required"),
            gpa.get("overall"),
            gpa.get("major"),
            json.dumps(parsed_data)
        ))

        conn.commit()
        return token

    except Exception as e:
        conn.rollback()
        raise e

    finally:
        cursor.close()
        conn.close()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
def root():
    return {"status": "ok", "service": "GopherPath API"}


@app.post("/parse-apas")
async def parse_apas_endpoint(file: UploadFile = File(...)):
    """
    Accepts a UMN APAS report (PDF), parses it, saves to database,
    and returns structured JSON with a session token.

    The session token is used to identify this student's plan
    throughout the rest of the application.
    """
    if not file.filename.endswith(".pdf"):
        raise HTTPException(
            status_code=400,
            detail="Only PDF files are accepted. Please export your APAS as a PDF from One Stop."
        )

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            content = await file.read()
            tmp.write(content)
            tmp_path = tmp.name

        parsed_data = parse_apas(tmp_path)

        errors = validate_parsed_apas(parsed_data)
        if errors:
            raise HTTPException(
                status_code=400,
                detail={
                    "message": "APAS parsed but failed validation",
                    "errors": errors
                }
            )

        # Save to database and get session token
        session_token = save_student_to_db(parsed_data)

        return {
            "status": "success",
            "session_token": session_token,
            "data": parsed_data
        }

    except HTTPException:
        raise

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to parse APAS: {str(e)}"
        )

    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


@app.get("/student/{session_token}")
def get_student(session_token: str):
    """
    Returns full saved state for a session — parsed APAS, plan, explanation,
    and chat history. Used by the frontend to restore a session without
    re-uploading or re-running the optimizer.
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute("""
            SELECT name, major, expected_graduation,
                   credits_earned, gpa_overall, parsed_apas_json,
                   created_at
            FROM students
            WHERE session_token = %s
        """, (session_token,))

        row = cursor.fetchone()

        if not row:
            raise HTTPException(status_code=404, detail="Session not found.")

        parsed_apas = row[5] or {}

        return {
            "status": "success",
            "data": {
                "name": row[0],
                "major": row[1],
                "expected_graduation": row[2],
                "credits_earned": float(row[3]) if row[3] else None,
                "gpa_overall": float(row[4]) if row[4] else None,
                "parsed_apas": parsed_apas,
                "generated_plan": parsed_apas.get("generated_plan"),
                "plan_explanation": parsed_apas.get("plan_explanation"),
                "chat_history": parsed_apas.get("chat_history", []),
                "created_at": row[6].isoformat(),
            }
        }

    finally:
        cursor.close()
        conn.close()


class ExplanationSave(BaseModel):
    explanation: str


@app.post("/save-explanation/{session_token}")
def save_explanation(session_token: str, body: ExplanationSave):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            UPDATE students
            SET parsed_apas_json = jsonb_set(
                parsed_apas_json::jsonb,
                '{plan_explanation}',
                %s::jsonb
            )
            WHERE session_token = %s
        """, (json.dumps(body.explanation), session_token))
        conn.commit()
        return {"status": "ok"}
    finally:
        cursor.close()
        conn.close()


class ChatHistoryUpdate(BaseModel):
    messages: list[dict]


@app.patch("/chat-history/{session_token}")
def save_chat_history(session_token: str, body: ChatHistoryUpdate):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            UPDATE students
            SET parsed_apas_json = jsonb_set(
                parsed_apas_json::jsonb,
                '{chat_history}',
                %s::jsonb
            )
            WHERE session_token = %s
        """, (json.dumps(body.messages), session_token))
        conn.commit()
        return {"status": "ok"}
    finally:
        cursor.close()
        conn.close()

# ---------------------------------------------------------------------------
# AI Advisor helpers
# ---------------------------------------------------------------------------

def _load_student_context(cursor, session_token):
    cursor.execute("""
        SELECT name, major, expected_graduation,
               credits_earned, credits_total_required, gpa_overall,
               parsed_apas_json
        FROM students WHERE session_token = %s
    """, (session_token,))
    row = cursor.fetchone()
    if not row:
        return None, None, None, None
    student = {
        "name": row[0],
        "major": row[1],
        "expected_graduation": row[2],
        "credits_earned": float(row[3]) if row[3] else 0,
        "credits_total_required": float(row[4]) if row[4] else 120,
        "gpa_overall": float(row[5]) if row[5] else None,
    }
    parsed_apas = row[6] or {}
    return student, parsed_apas, parsed_apas.get("generated_plan"), parsed_apas.get("preferences")


def _build_system_prompt(student, parsed_apas, generated_plan, preferences, candidate_courses=""):
    name = student["name"] or "Student"
    completed = [c for c in parsed_apas.get("completed_courses", []) if not c.get("is_withdrawn")]
    completed_lines = "\n".join(
        f"  - {c['subject']} {c['number']}: {c.get('title', '')} (Grade: {c.get('grade', 'N/A')})"
        for c in completed
    ) or "  (none)"

    remaining = parsed_apas.get("remaining_requirements", [])
    remaining_lines = []
    for req in remaining:
        opts = req.get("options", [])
        if opts:
            preview = ", ".join(opts[:3]) + ("..." if len(opts) > 3 else "")
            remaining_lines.append(f"  - {req['category']}: {preview}")
        else:
            remaining_lines.append(f"  - {req['category']}: {req.get('credits_needed', '?')} credits needed")
    remaining_block = "\n".join(remaining_lines) or "  (none)"

    plan_lines = []
    if generated_plan:
        for term in generated_plan.get("plan", []):
            plan_lines.append(f"\n  {term['term_label']} ({term['total_credits']} credits):")
            for c in term["courses"]:
                plan_lines.append(
                    f"    - {c['subject']} {c['number']}: {c['title']} "
                    f"({c['credits']}cr) → satisfies: {c['requirement_category']}"
                )
        unscheduled = generated_plan.get("unscheduled", [])
        if unscheduled:
            plan_lines.append("\n  COULD NOT SCHEDULE:")
            for c in unscheduled:
                plan_lines.append(f"    - {c['subject']} {c['number']}: {c.get('title','')} ({c['requirement_category']})")
    plan_block = "".join(plan_lines) or "  (no plan generated yet)"

    prefs = preferences or {}
    pref_lines = []
    if prefs.get("difficulty"):
        pref_lines.append(f"  - Difficulty preference: {prefs['difficulty']}")
    if prefs.get("timeline"):
        pref_lines.append(f"  - Timeline: {prefs['timeline']}")
    if prefs.get("free_text"):
        pref_lines.append(f"  - Student's note: \"{prefs['free_text']}\"")
    pref_block = "\n".join(pref_lines) or "  (none specified)"

    return f"""You are an AI academic advisor embedded in GopherPath, an academic planning tool for University of Minnesota students.

STUDENT PROFILE:
- Name: {name}
- Major: {student['major'] or 'Unknown'}
- Overall GPA: {student['gpa_overall'] or 'N/A'}
- Credits earned: {student['credits_earned']} / {student['credits_total_required']}
- Expected graduation: {student['expected_graduation'] or 'Unknown'}

COMPLETED COURSES ({len(completed)} courses):
{completed_lines}

REMAINING REQUIREMENTS ({len(remaining)} items):
{remaining_block}

GENERATED PLAN:
{plan_block}

STUDENT PREFERENCES:
{pref_block}

{candidate_courses}
YOUR RULES:
1. Be warm, specific, and advisor-like — like a knowledgeable friend who knows UMN inside and out.
2. NEVER invent course numbers, titles, or requirement fulfillments. Only use courses from CANDIDATE COURSES above or already named in the plan/completed courses.
3. Keep responses concise and direct. Students don't want paragraphs when a sentence will do.
4. For major decisions (adding a minor, changing graduation date), remind the student to confirm with their real advisor.
5. If asked about what-if scenarios, explain what would change conceptually — tell the student they can ask to regenerate the plan.
6. Speak in first person as an advisor ("I'd recommend...", "Looking at your plan...").
"""


_SKIP_KEYWORDS = POLICY["meta_requirement_keywords"]


def _get_candidate_courses_for_chat(cursor, parsed_apas, generated_plan):
    """
    Returns a formatted block of real course options for each open requirement,
    to be injected into the chat system prompt.
    """
    completed_codes = {
        f"{c['subject']} {c['number']}"
        for c in parsed_apas.get("completed_courses", [])
        if not c.get("is_withdrawn")
    }
    planned_codes = set()
    if generated_plan:
        for term in generated_plan.get("plan", []):
            for c in term["courses"]:
                if c["subject"] != "TBD":
                    planned_codes.add(f"{c['subject']} {c['number']}")
    exclude = completed_codes | planned_codes

    def fmt_row(row):
        semesters = (["Fall"] if row[4] else []) + (["Spring"] if row[5] else [])
        sem = "/".join(semesters) or "check schedule"
        return f"  - {row[0]} {row[1]}: {row[2]} ({float(row[3])}cr, {sem})"

    sections = []
    for req in parsed_apas.get("remaining_requirements", []):
        category = req.get("category", "")
        if any(kw.lower() in category.lower() for kw in _SKIP_KEYWORDS):
            continue

        # Writing Intensive — query by WI attribute (same quality rules as
        # the optimizer: no directed studies, 3cr+ norm floor)
        if "Writing Intensive" in category:
            cursor.execute(f"""
                SELECT DISTINCT c.subject_code, c.catalog_number, c.title,
                       c.credits, c.offered_fall, c.offered_spring
                FROM courses c
                JOIN course_attributes ca ON c.id = ca.course_id
                WHERE ca.attribute = 'WI' AND c.acad_career = 'UGRD'
                  AND c.credits IS NOT NULL
                  AND c.credits >= %s
                  AND c.catalog_number !~* '{RESTRICTED_SUFFIX_PATTERN}'
                  AND c.title !~* '{DIRECTED_STUDY_TITLE_PATTERN}'
                ORDER BY c.catalog_number ASC, c.subject_code ASC, c.title ASC
                LIMIT 200
            """, (MIN_WI_CREDITS,))
            lines = [fmt_row(r) for r in cursor.fetchall()
                     if f"{r[0]} {r[1]}" not in exclude][:8]
            if lines:
                sections.append(f"[{category}]\n" + "\n".join(lines))
            continue

        cle_values = match_le_category(category)
        if not cle_values or cle_values == [None]:
            continue

        lines = []
        for cle_value in cle_values:
            # Same eligibility rules as the optimizer: UMN LE credit minimums
            # (3cr, or 4cr for bio/physical sciences) and no restricted
            # (H/V-suffix Honors) sections — the chat must never suggest a
            # course that can't actually fulfill the requirement.
            cursor.execute(f"""
                SELECT DISTINCT c.subject_code, c.catalog_number, c.title,
                       c.credits, c.offered_fall, c.offered_spring
                FROM courses c
                JOIN course_attributes ca ON c.id = ca.course_id
                WHERE ca.attribute = 'CLE' AND ca.value = %s
                  AND c.acad_career = 'UGRD'
                  AND c.credits IS NOT NULL
                  AND c.credits >= %s
                  AND c.credits <= {POLICY["course_filters"]["max_recommended_credits"]}
                  AND c.catalog_number !~* '{RESTRICTED_SUFFIX_PATTERN}'
                  AND c.title !~* '{DIRECTED_STUDY_TITLE_PATTERN}'
                ORDER BY c.catalog_number ASC, c.subject_code ASC, c.title ASC
                LIMIT 200
            """, (cle_value, min_credits_for_cle_value(cle_value)))
            for row in cursor.fetchall():
                if f"{row[0]} {row[1]}" not in exclude:
                    lines.append(fmt_row(row))
                if len(lines) >= 8:
                    break
            if len(lines) >= 8:
                break
        if lines:
            sections.append(f"[{category}]\n" + "\n".join(lines))

    if not sections:
        return ""
    return (
        "CANDIDATE COURSES FOR OPEN REQUIREMENTS:\n"
        "(ONLY recommend courses from this list when the student asks about options. "
        "Never invent course codes or titles not listed here. "
        "If a student asks about a requirement not covered here, "
        "say you can look it up rather than guessing.)\n\n"
        + "\n\n".join(sections)
    )


def _stream_claude(system_prompt, messages):
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    def generate():
        try:
            with client.messages.stream(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                system=system_prompt,
                messages=messages,
            ) as stream:
                for text in stream.text_stream:
                    yield f"data: {json.dumps({'text': text})}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.post("/chat/{session_token}")
def chat_endpoint(session_token: str, body: ChatRequest):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        student, parsed_apas, generated_plan, preferences = _load_student_context(cursor, session_token)
        if not student:
            raise HTTPException(status_code=404, detail="Session not found.")
        candidate_courses = _get_candidate_courses_for_chat(cursor, parsed_apas, generated_plan)
    finally:
        cursor.close()
        conn.close()

    system_prompt = _build_system_prompt(student, parsed_apas, generated_plan, preferences, candidate_courses)

    messages = [{"role": m.role, "content": m.content} for m in body.history]

    if body.is_opening:
        first_name = (student["name"] or "there").split()[0]
        messages.append({
            "role": "user",
            "content": (
                f"Please greet me as my academic advisor. My name is {first_name}. "
                f"In 2-4 sentences: say hi, summarize the plan (semesters and total credits to schedule), "
                f"and proactively flag 1-2 things I should pay attention to — "
                f"such as heavy semesters, tight prerequisite chains, or any requirements that couldn't be scheduled. "
                f"Be warm and specific. Do not use bullet points."
            ),
        })
    else:
        messages.append({"role": "user", "content": body.message})

    return _stream_claude(system_prompt, messages)


@app.get("/plan-explanation/{session_token}")
def plan_explanation_endpoint(session_token: str):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        student, parsed_apas, generated_plan, preferences = _load_student_context(cursor, session_token)
        if not student:
            raise HTTPException(status_code=404, detail="Session not found.")
    finally:
        cursor.close()
        conn.close()

    if not generated_plan:
        raise HTTPException(status_code=400, detail="No plan generated yet.")

    system_prompt = _build_system_prompt(student, parsed_apas, generated_plan, preferences)
    first_name = (student["name"] or "this student").split()[0]
    semesters_with_courses = [t for t in generated_plan.get("plan", []) if t["courses"]]
    unscheduled_count = len(generated_plan.get("unscheduled", []))

    user_msg = (
        f"Write a plan explanation for {first_name}. 3-4 paragraphs, flowing prose only — no headers or bullets. "
        f"Cover: (1) an overview spanning {len(semesters_with_courses)} semesters, "
        f"(2) why key courses are placed when they are — name specific courses, "
        f"(3) any prerequisite chains the student should know about, "
        f"(4) anything to watch out for (heavy semesters, tight scheduling"
        + (f", {unscheduled_count} requirements that couldn't be scheduled" if unscheduled_count else "")
        + f"). Warm, advisor-like tone."
    )

    return _stream_claude(system_prompt, [{"role": "user", "content": user_msg}])


from backend.optimizer import (
    optimize_plan,
    match_le_category,
    min_credits_for_cle_value,
    RESTRICTED_SUFFIX_PATTERN,
    DIRECTED_STUDY_TITLE_PATTERN,
    MIN_WI_CREDITS,
)

@app.post("/optimize/{session_token}")
def optimize_plan_endpoint(session_token: str, preferences: Preferences):
    """
    Generates a semester-by-semester course plan for a student.
    Accepts preferences to customize the plan.
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute(
            "SELECT parsed_apas_json FROM students WHERE session_token = %s",
            (session_token,)
        )
        row = cursor.fetchone()

        if not row:
            raise HTTPException(status_code=404, detail="Session not found.")

        parsed_apas = row[0]

        # Convert preferences to optimizer format
        prefs = {
            "difficulty": preferences.difficulty,
            "timeline": preferences.timeline,
            "max_credits_per_semester": preferences.max_credits or (
                POLICY["scheduling"]["asap_max_credits"]
                if preferences.timeline == "asap"
                else POLICY["scheduling"]["default_max_credits"]
            ),
            "free_text": preferences.free_text,
        }

        plan = optimize_plan(parsed_apas, prefs)

        # Save plan and preferences for chat context.
        # Drop chat_history and plan_explanation: they describe the previous
        # plan, and the frontend re-runs the advisor init sequence (explanation,
        # requirements card, follow-up) when no saved messages exist.
        cursor.execute("""
            UPDATE students
            SET parsed_apas_json = jsonb_set(
                jsonb_set(
                    parsed_apas_json::jsonb - 'chat_history' - 'plan_explanation',
                    '{generated_plan}',
                    %s::jsonb
                ),
                '{preferences}',
                %s::jsonb
            )
            WHERE session_token = %s
        """, (json.dumps(plan), json.dumps(prefs), session_token))
        conn.commit()

        return {"status": "success", "plan": plan}

    finally:
        cursor.close()
        conn.close()