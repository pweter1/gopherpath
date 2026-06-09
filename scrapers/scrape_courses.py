from umn_subjects import UMN_SUBJECTS
"""
scrape_courses.py
-----------------
Scrapes all undergraduate courses from the UMN Schedule Builder API
and loads them into the gopherpath PostgreSQL database.

Schedule Builder API endpoints used:
  - /api.php?type=courses   → course catalog data (title, credits, description, attributes)
  - /api.php?type=sections  → used later to derive offering frequency across terms

Run:
    python3 scrapers/scrape_courses.py

The script is idempotent — running it twice will update existing rows
rather than creating duplicates, using PostgreSQL's ON CONFLICT clause.
"""

import requests
import psycopg2
import os
import time
import json
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

# Schedule Builder API base URL — all requests go through api.php
API_BASE = "https://schedulebuilder.umn.edu/api.php"

# Institution constants matching Schedule Builder's own identifiers
INSTITUTION = "UMNTC"
CAMPUS = "UMNTC"

# Fall 2026 term code. We use one term to collect course catalog data.
# Offering frequency (fall/spring/both) is derived separately in a later script.
TERM = 1269

# We only want undergraduate courses. Graduate courses are out of scope for V1.
CAREER_FILTER = "UGRD"

# Delay between API requests in seconds. Be a polite scraper —
# hammering the server with no delay risks getting your IP blocked.
REQUEST_DELAY = 0.5

# ---------------------------------------------------------------------------
# Database connection
# ---------------------------------------------------------------------------

def get_db_connection():
    """
    Opens and returns a psycopg2 database connection.
    psycopg2 is the standard PostgreSQL driver for Python.
    """
    return psycopg2.connect(DATABASE_URL)


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def fetch_subjects():
    """
    Fetches all subject codes (departments) from Schedule Builder.
    Returns a list of dicts with 'code' and 'name' keys.

    We fetch subjects first so we can iterate over them and pull
    courses one department at a time — this avoids any single
    request returning tens of thousands of courses at once.
    """
    params = {
        "type": "subjects",
        "institution": INSTITUTION,
        "campus": CAMPUS,
        "term": TERM,
    }
    response = requests.get(API_BASE, params=params, timeout=30)
    response.raise_for_status()  # raises an exception on 4xx/5xx responses
    return response.json()


def fetch_courses_for_subject(subject_code):
    """
    Fetches all courses for a single subject code from Schedule Builder.
    Returns a list of course dicts.

    We filter to UGRD here using a list comprehension rather than an API
    parameter because the API doesn't support server-side career filtering.
    """
    params = {
        "type": "courses",
        "institution": INSTITUTION,
        "campus": CAMPUS,
        "term": TERM,
        "subject": subject_code,
    }
    response = requests.get(API_BASE, params=params, timeout=30)
    response.raise_for_status()

    all_courses = response.json()

    # Filter to undergraduate courses only
    return [c for c in all_courses if c.get("acad_career") == CAREER_FILTER]


# ---------------------------------------------------------------------------
# Data processing helpers
# ---------------------------------------------------------------------------

def extract_prereq_text(description_paragraphs):
    """
    Extracts raw prerequisite text from a course description.

    Schedule Builder returns descriptions as a list of paragraph strings.
    Prerequisites are typically in the last paragraph and start with
    'Prereq:', 'prereq:', or 'Prerequisites:'.

    We store this raw text now. The Claude API will parse it into
    structured logic in a separate script (parse_prerequisites.py).

    Returns the raw prereq string, or None if no prereq text found.
    """
    if not description_paragraphs:
        return None

    prereq_keywords = ["prereq:", "prerequisites:", "pre-req:"]

    for paragraph in description_paragraphs:
        lower = paragraph.lower()
        for keyword in prereq_keywords:
            if keyword in lower:
                # Find where the keyword starts and return from there
                idx = lower.index(keyword)
                return paragraph[idx:].strip()

    return None


def build_full_description(description_paragraphs):
    """
    Joins the list of description paragraphs into a single string.
    Schedule Builder returns descriptions as a list; we store them as text.
    """
    if not description_paragraphs:
        return None
    return "\n\n".join(description_paragraphs)


# ---------------------------------------------------------------------------
# Database write helpers
# ---------------------------------------------------------------------------

def upsert_institution(cursor):
    """
    Inserts UMN as an institution if it doesn't exist yet.
    Returns the institution's database ID.

    ON CONFLICT DO NOTHING means if we run this script twice,
    we don't get a duplicate row or an error.
    """
    cursor.execute("""
        INSERT INTO institutions (code, name)
        VALUES (%s, %s)
        ON CONFLICT (code) DO NOTHING
    """, (INSTITUTION, "University of Minnesota Twin Cities"))

    cursor.execute("SELECT id FROM institutions WHERE code = %s", (INSTITUTION,))
    return cursor.fetchone()[0]


def upsert_subject(cursor, institution_id, subject_code, subject_name):
    """
    Inserts a subject (department) row if it doesn't exist.
    Returns the subject's database ID.
    """
    cursor.execute("""
        INSERT INTO subjects (institution_id, code, name)
        VALUES (%s, %s, %s)
        ON CONFLICT (institution_id, code) DO UPDATE SET name = EXCLUDED.name
    """, (institution_id, subject_code, subject_name))

    cursor.execute(
        "SELECT id FROM subjects WHERE institution_id = %s AND code = %s",
        (institution_id, subject_code)
    )
    return cursor.fetchone()[0]


def upsert_course(cursor, institution_id, subject_id, course_data):
    """
    Inserts or updates a course row.

    ON CONFLICT (institution_id, subject_code, catalog_number) DO UPDATE
    means if the course already exists (from a previous run), we update
    its fields with the latest data rather than failing or duplicating.

    Returns the course's database ID.
    """
    description_paragraphs = course_data.get("description", [])
    full_description = build_full_description(description_paragraphs)
    prereq_raw = extract_prereq_text(description_paragraphs)

    cursor.execute("""
        INSERT INTO courses (
            institution_id, subject_id, source_id,
            subject_code, catalog_number, title,
            description, credits, min_credits, max_credits,
            credits_variable, course_repeatable,
            acad_career, prereq_raw, updated_at
        )
        VALUES (
            %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s,
            %s, %s, NOW()
        )
        ON CONFLICT (institution_id, subject_code, catalog_number)
        DO UPDATE SET
            title           = EXCLUDED.title,
            description     = EXCLUDED.description,
            credits         = EXCLUDED.credits,
            min_credits     = EXCLUDED.min_credits,
            max_credits     = EXCLUDED.max_credits,
            credits_variable = EXCLUDED.credits_variable,
            course_repeatable = EXCLUDED.course_repeatable,
            prereq_raw      = EXCLUDED.prereq_raw,
            updated_at      = NOW()
        RETURNING id
    """, (
        institution_id,
        subject_id,
        course_data.get("id"),                          # source_id = crse_id
        course_data.get("subject"),
        course_data.get("catalog_nbr"),
        course_data.get("title"),
        full_description,
        course_data.get("credits"),
        course_data.get("min_credits"),
        course_data.get("max_credits"),
        course_data.get("credits_variable", False),
        course_data.get("course_repeatable", False),
        course_data.get("acad_career"),
        prereq_raw,
    ))

    return cursor.fetchone()[0]


def insert_course_attributes(cursor, course_id, attributes):
    """
    Inserts Liberal Ed and other attribute flags for a course.

    We delete existing attributes first and re-insert them on each run.
    This is simpler than diffing and is safe because attributes rarely
    change — and when they do, we want the latest version.
    """
    # Clear existing attributes for this course before re-inserting
    cursor.execute("DELETE FROM course_attributes WHERE course_id = %s", (course_id,))

    for attr in attributes:
        cursor.execute("""
            INSERT INTO course_attributes (course_id, attribute, value, name)
            VALUES (%s, %s, %s, %s)
        """, (
            course_id,
            attr.get("attribute"),
            attr.get("attribute_value"),
            attr.get("name"),
        ))


# ---------------------------------------------------------------------------
# Main scraping loop
# ---------------------------------------------------------------------------

def scrape_all_courses():
    """
    Main entry point. Fetches all subjects, then iterates over each subject
    to fetch and store its courses.

    Progress is printed to stdout so you can watch the scrape run.
    Errors on individual subjects are caught and logged without stopping
    the entire job — if BIOL fails, CHEM still runs.
    """
    print(f"Connecting to database...")
    conn = get_db_connection()
    cursor = conn.cursor()

    subjects = UMN_SUBJECTS
    print(f"Using {len(subjects)} UMN subject codes.\n")

    # Ensure UMN institution row exists
    institution_id = upsert_institution(cursor)
    conn.commit()

    total_courses = 0
    failed_subjects = []

    for i, subject in enumerate(subjects):
        subject_code, subject_name = subject

        print(f"[{i+1}/{len(subjects)}] {subject_code} — {subject_name}", end=" ... ")

        try:
            courses = fetch_courses_for_subject(subject_code)

            if not courses:
                print(f"0 undergraduate courses, skipping.")
                time.sleep(REQUEST_DELAY)
                continue

            # Ensure subject row exists
            subject_id = upsert_subject(cursor, institution_id, subject_code, subject_name)

            courses_inserted = 0
            for course in courses:
                course_id = upsert_course(cursor, institution_id, subject_id, course)
                insert_course_attributes(cursor, course_id, course.get("attributes", []))
                courses_inserted += 1

            conn.commit()
            total_courses += courses_inserted
            print(f"{courses_inserted} courses.")

        except Exception as e:
            conn.rollback()
            failed_subjects.append(subject_code)
            print(f"ERROR: {e}")

        time.sleep(REQUEST_DELAY)

    # Final summary
    print(f"\n{'='*50}")
    print(f"Scrape complete.")
    print(f"Total undergraduate courses loaded: {total_courses}")
    if failed_subjects:
        print(f"Failed subjects ({len(failed_subjects)}): {', '.join(failed_subjects)}")
    else:
        print(f"No failures.")

    cursor.close()
    conn.close()


if __name__ == "__main__":
    scrape_all_courses()