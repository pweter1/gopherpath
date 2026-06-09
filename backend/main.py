"""
main.py
-------
GopherPath FastAPI backend.

Run locally with:
    uvicorn backend.main:app --reload
"""

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import tempfile
import os
import sys
import hashlib
import secrets
import json
import psycopg2
from dotenv import load_dotenv

load_dotenv()

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scrapers.parse_apas import parse_apas, validate_parsed_apas

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
    Retrieves a previously parsed APAS by session token.
    This allows students to return to their plan without re-uploading.
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

        return {
            "status": "success",
            "data": {
                "name": row[0],
                "major": row[1],
                "expected_graduation": row[2],
                "credits_earned": float(row[3]) if row[3] else None,
                "gpa_overall": float(row[4]) if row[4] else None,
                "parsed_apas": row[5],
                "created_at": row[6].isoformat()
            }
        }

    finally:
        cursor.close()
        conn.close()