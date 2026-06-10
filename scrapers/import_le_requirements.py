"""
import_le_requirements.py
-------------------------
Imports Liberal Education requirement mappings into course_attributes.

Rather than decoding numeric requirement IDs from the CSV, we use the
pre-filtered CSVs exported from the CourseDog catalog — one per Liberal
Ed requirement. Each CSV contains exactly the courses that satisfy that
requirement, so we can tag them directly.

Run:
    python3 scrapers/import_le_requirements.py
"""

import csv
import os
import psycopg2
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

# Map Liberal Ed requirement name to its filtered CSV file
LE_FILES = {
    'Arts/Humanities':           'database/courses-report.2026-06-10.Arts.csv',
    'Biological Sciences':       'database/courses-report.2026-06-10.Bio.csv',
    'Civic Life and Ethics':     'database/courses-report.2026-06-09.Civic.csv',
    'Global Perspectives':       'database/courses-report.2026-06-10.Global.csv',
    'Historical Perspectives':   'database/courses-report.2026-06-10.Hist.csv',
    'Literature':                'database/courses-report.2026-06-10.Lit.csv',
    'Mathematical Thinking':     'database/courses-report.2026-06-10.Math.csv',
    'Physical Sciences':         'database/courses-report.2026-06-10.Phys.csv',
    'Race Power and Justice':    'database/courses-report.2026-06-10.Race.csv',
    'Social Sciences':           'database/courses-report.2026-06-10.Soc.csv',
    'The Environment':           'database/courses-report.2026-06-10.Env.csv',
    'Technology and Society':    'database/courses-report.2026-06-10.Tech.csv',
}

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)


def import_le_requirements():
    conn = get_db_connection()
    cursor = conn.cursor()

    # Clear existing CLE attributes — we're rebuilding from scratch
    cursor.execute("DELETE FROM course_attributes WHERE attribute = 'CLE'")
    conn.commit()
    print("Cleared existing CLE attributes.")

    total_tagged = 0

    for le_name, filepath in LE_FILES.items():
        if not os.path.exists(filepath):
            print(f"MISSING: {filepath}")
            continue

        tagged = 0
        not_found = 0

        with open(filepath, newline='', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            for row in reader:
                subject = row['Course subject code'].strip()
                number = row['Course number'].strip()

                if row['Campus'].strip() != 'Twin Cities':
                    continue

                # Look up the course in our database
                cursor.execute("""
                    SELECT id FROM courses
                    WHERE subject_code = %s AND catalog_number = %s
                """, (subject, number))

                result = cursor.fetchone()

                # If not found, try stripping W suffix
                if not result and number.upper().endswith('W'):
                    cursor.execute("""
                        SELECT id FROM courses
                        WHERE subject_code = %s AND catalog_number = %s
                    """, (subject, number[:-1]))
                    result = cursor.fetchone()

                if result:
                    course_id = result[0]
                    cursor.execute("""
                        INSERT INTO course_attributes (course_id, attribute, value, name)
                        VALUES (%s, 'CLE', %s, %s)
                        ON CONFLICT DO NOTHING
                    """, (course_id, le_name, le_name))
                    tagged += 1
                else:
                    not_found += 1

        conn.commit()
        total_tagged += tagged
        print(f"{le_name}: {tagged} courses tagged ({not_found} not found in DB)")

    print(f"\nTotal CLE attributes added: {total_tagged}")
    cursor.close()
    conn.close()


if __name__ == "__main__":
    import_le_requirements()