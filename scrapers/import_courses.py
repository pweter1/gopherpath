"""
import_courses.py
-----------------
Imports UMN course data from the CourseDog CSV export into PostgreSQL.

The CSV is downloaded from:
  umtc.catalog.prod.coursedog.com/courses → "Export all results as CSV"

This script replaces scrape_courses.py. Rather than fighting Cloudflare
and session-based APIs, we use the official public export from CourseDog's
catalog frontend — the same underlying data, just a different access method.

Run:
    python3 scrapers/import_courses.py

Idempotent — safe to run multiple times. Existing rows are updated,
not duplicated.
"""

import csv
import os
import re
import psycopg2
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
CSV_PATH = "database/courses-report.2026-06-09.csv"

# Institution constants
INSTITUTION_CODE = "UMNTC"
INSTITUTION_NAME = "University of Minnesota Twin Cities"

# ---------------------------------------------------------------------------
# Offering frequency parser
# ---------------------------------------------------------------------------

def parse_offered_terms(term_string):
    """
    Parses the 'Typically offered term(s)' field into three booleans.

    Examples of values in the CSV:
      'Every Fall'
      'Every Spring'
      'Every Fall & Spring'
      'Every Fall, Spring & Summer'
      'Periodic Fall'
      'Periodic Fall & Spring'
      'Fall Odd Years'
      'Every Summer'
      '-'  (unknown/not specified)

    We treat 'Periodic' the same as 'Every' — the course is offered in
    that semester, just not guaranteed every single year. For the optimizer,
    what matters is whether it's *ever* offered in a given semester.

    Returns: (offered_fall, offered_spring, offered_summer) as booleans.
    """
    if not term_string or term_string.strip() == '-':
        # Unknown offering frequency — default to fall and spring
        # so the optimizer doesn't incorrectly block the course
        return True, True, False

    t = term_string.lower()

    offered_fall   = 'fall' in t
    offered_spring = 'spring' in t
    offered_summer = 'summer' in t

    return offered_fall, offered_spring, offered_summer


# ---------------------------------------------------------------------------
# Credit parser
# ---------------------------------------------------------------------------

def parse_credits(credit_string):
    """
    Converts a credit string to a float, or None if unparseable.
    Handles values like '3', '1.5', '' (empty).
    """
    if not credit_string or credit_string.strip() == '':
        return None
    try:
        return float(credit_string.strip())
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Prerequisite extractor
# ---------------------------------------------------------------------------

def extract_prereq_text(description):
    """
    Extracts raw prerequisite text from a course description.

    Prerequisites appear in descriptions as sentences starting with
    'prereq:', 'Prereq:', or 'Prerequisites:'. They're typically at
    the end of the description.

    Returns the raw prereq string, or None if not found.
    """
    if not description:
        return None

    prereq_pattern = re.compile(
        r'(prereq[s]?:|prerequisites?:)(.*?)(\.|$)',
        re.IGNORECASE | re.DOTALL
    )

    match = prereq_pattern.search(description)
    if match:
        return (match.group(1) + match.group(2)).strip()

    return None


# ---------------------------------------------------------------------------
# Writing intensive flag
# ---------------------------------------------------------------------------

def extract_writing_intensive(catalog_number):
    """
    Courses with a 'W' suffix (e.g. '3211W') are Writing Intensive.
    Returns (clean_number, is_writing_intensive).
    """
    if catalog_number and catalog_number.upper().endswith('W'):
        return catalog_number[:-1], True
    return catalog_number, False


# ---------------------------------------------------------------------------
# Requirements parser
# ---------------------------------------------------------------------------

def parse_requirements(req_string):
    """
    Parses the Requirements field into a list of requirement strings.

    Examples:
      '-'                          → []
      'Social Sciences'            → ['Social Sciences']
      'Arts/Humanities, Literature' → ['Arts/Humanities', 'Literature']

    These map to Liberal Education requirements and will be stored
    in course_attributes table.
    """
    if not req_string or req_string.strip() == '-':
        return []

    return [r.strip() for r in req_string.split(',') if r.strip()]


# ---------------------------------------------------------------------------
# Academic career inference
# ---------------------------------------------------------------------------

def infer_acad_career(catalog_number):
    """
    Infers whether a course is undergraduate or graduate based on
    course number. UMN convention:
      1000-4999 = undergraduate
      5000+     = graduate

    This isn't perfect — some 5xxx courses are open to undergrads —
    but it's a reasonable default until we get better data.
    """
    if not catalog_number:
        return 'UGRD'

    # Strip any trailing letters (e.g. '3211W' → '3211')
    num_only = re.sub(r'[^0-9]', '', catalog_number)
    if not num_only:
        return 'UGRD'

    try:
        num = int(num_only)
        return 'GRAD' if num >= 5000 else 'UGRD'
    except ValueError:
        return 'UGRD'


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)


def upsert_institution(cursor):
    cursor.execute("""
        INSERT INTO institutions (code, name)
        VALUES (%s, %s)
        ON CONFLICT (code) DO NOTHING
    """, (INSTITUTION_CODE, INSTITUTION_NAME))

    cursor.execute(
        "SELECT id FROM institutions WHERE code = %s",
        (INSTITUTION_CODE,)
    )
    return cursor.fetchone()[0]


def upsert_subject(cursor, institution_id, subject_code):
    """
    Inserts a subject row if it doesn't exist.
    We don't have subject names in the CSV, so we use the code as name.
    These can be updated later if needed.
    """
    cursor.execute("""
        INSERT INTO subjects (institution_id, code, name)
        VALUES (%s, %s, %s)
        ON CONFLICT (institution_id, code) DO NOTHING
    """, (institution_id, subject_code, subject_code))

    cursor.execute(
        "SELECT id FROM subjects WHERE institution_id = %s AND code = %s",
        (institution_id, subject_code)
    )
    return cursor.fetchone()[0]


def upsert_course(cursor, institution_id, subject_id, row):
    """
    Inserts or updates a course row from a CSV row dict.
    Returns the course's database ID.
    """
    catalog_number_raw = row['Course number'].strip()
    catalog_number, is_wi = extract_writing_intensive(catalog_number_raw)

    description = row['Course description'].strip() or None
    prereq_raw  = extract_prereq_text(description)

    min_credits = parse_credits(row['Minimum credits'])
    max_credits = parse_credits(row['Maximum credits'])

    # A course has variable credits if min != max
    credits_variable = (
        min_credits is not None and
        max_credits is not None and
        min_credits != max_credits
    )

    # Use min_credits as the standard credit value
    credits = min_credits

    is_repeatable = row['Is this course repeatable?'].strip().lower() == 'yes'

    offered_fall, offered_spring, offered_summer = parse_offered_terms(
        row['Typically offered term(s)']
    )

    acad_career = infer_acad_career(catalog_number_raw)

    subject_code = row['Course subject code'].strip()

    cursor.execute("""
        INSERT INTO courses (
            institution_id, subject_id,
            subject_code, catalog_number, title,
            description, credits, min_credits, max_credits,
            credits_variable, course_repeatable,
            acad_career, prereq_raw,
            offered_fall, offered_spring, offered_summer,
            updated_at
        )
        VALUES (
            %s, %s,
            %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s,
            %s, %s,
            %s, %s, %s,
            NOW()
        )
        ON CONFLICT (institution_id, subject_code, catalog_number)
        DO UPDATE SET
            title            = EXCLUDED.title,
            description      = EXCLUDED.description,
            credits          = EXCLUDED.credits,
            min_credits      = EXCLUDED.min_credits,
            max_credits      = EXCLUDED.max_credits,
            credits_variable = EXCLUDED.credits_variable,
            course_repeatable = EXCLUDED.course_repeatable,
            prereq_raw       = EXCLUDED.prereq_raw,
            offered_fall     = EXCLUDED.offered_fall,
            offered_spring   = EXCLUDED.offered_spring,
            offered_summer   = EXCLUDED.offered_summer,
            updated_at       = NOW()
        RETURNING id
    """, (
        institution_id, subject_id,
        subject_code, catalog_number, row['Course name'].strip(),
        description, credits, min_credits, max_credits,
        credits_variable, is_repeatable,
        acad_career, prereq_raw,
        offered_fall, offered_spring, offered_summer,
    ))

    return cursor.fetchone()[0]


def insert_course_attributes(cursor, course_id, requirements, is_writing_intensive):
    """
    Stores Liberal Ed requirements and Writing Intensive flag
    as course attributes.
    """
    cursor.execute(
        "DELETE FROM course_attributes WHERE course_id = %s",
        (course_id,)
    )

    # Writing Intensive flag
    if is_writing_intensive:
        cursor.execute("""
            INSERT INTO course_attributes (course_id, attribute, value, name)
            VALUES (%s, %s, %s, %s)
        """, (course_id, 'WI', 'WI', 'Writing Intensive'))

    # Liberal Ed requirements
    for req in requirements:
        cursor.execute("""
            INSERT INTO course_attributes (course_id, attribute, value, name)
            VALUES (%s, %s, %s, %s)
        """, (course_id, 'CLE', req, req))


# ---------------------------------------------------------------------------
# Main import loop
# ---------------------------------------------------------------------------

def import_courses():
    print(f"Connecting to database...")
    conn = get_db_connection()
    cursor = conn.cursor()

    print(f"Setting up institution...")
    institution_id = upsert_institution(cursor)
    conn.commit()

    print(f"Reading {CSV_PATH}...")

    total        = 0
    skipped      = 0
    errors       = 0
    subject_ids  = {}  # cache subject_code → subject_id to avoid repeat queries

    with open(CSV_PATH, newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)

        for row in reader:
            subject_code = row['Course subject code'].strip()

            # Skip non-Twin-Cities courses
            if row['Campus'].strip() != 'Twin Cities':
                skipped += 1
                continue

            try:
                # Get or create subject (cached)
                if subject_code not in subject_ids:
                    subject_ids[subject_code] = upsert_subject(
                        cursor, institution_id, subject_code
                    )
                subject_id = subject_ids[subject_code]

                # Insert/update course
                catalog_number_raw = row['Course number'].strip()
                _, is_wi = extract_writing_intensive(catalog_number_raw)
                requirements = parse_requirements(row['Requirements'])

                course_id = upsert_course(cursor, institution_id, subject_id, row)
                insert_course_attributes(cursor, course_id, requirements, is_wi)

                total += 1

                if total % 500 == 0:
                    conn.commit()
                    print(f"  {total} courses imported...")

            except Exception as e:
                conn.rollback()
                errors += 1
                print(f"  ERROR on {subject_code} {row.get('Course number', '?')}: {e}")

    conn.commit()

    print(f"\n{'='*50}")
    print(f"Import complete.")
    print(f"  Courses imported: {total}")
    print(f"  Skipped (non-TC): {skipped}")
    print(f"  Errors:           {errors}")

    cursor.close()
    conn.close()


if __name__ == "__main__":
    import_courses()