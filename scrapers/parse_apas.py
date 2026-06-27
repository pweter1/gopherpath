import sys
"""
parse_apas.py
-------------
Parses a UMN APAS report (PDF) using pdfplumber for text extraction
and the Claude API for structured data extraction.

The APAS (Academic Progress Audit System) report is a student's official
degree progress document from UMN One Stop. It contains:
  - Completed courses with grades and credits
  - In-progress courses
  - Remaining degree requirements
  - GPA, credit counts, major, expected graduation

This script does two things:
  1. Extracts raw text from the APAS PDF using pdfplumber
  2. Sends that text to Claude with a carefully engineered prompt
     that returns structured JSON

The JSON output is the input to the constraint optimizer in Phase 3.

Usage:
    python3 scrapers/parse_apas.py path/to/apas.pdf
"""

import sys
import json
import pdfplumber
import anthropic
import os
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# PDF text extraction
# ---------------------------------------------------------------------------

def extract_text_from_pdf(pdf_path):
    """
    Extracts all text from a PDF file using pdfplumber.

    pdfplumber is better than PyPDF2 for complex layouts because it
    preserves spatial relationships between text elements. APAS reports
    have a table-like structure that pdfplumber handles well.

    Returns the full text as a single string with page breaks preserved.
    """
    pages = []

    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages):
            text = page.extract_text()
            if text:
                pages.append(f"--- PAGE {i+1} ---\n{text}")

    full_text = "\n\n".join(pages)

    if not full_text.strip():
        raise ValueError("Could not extract any text from PDF. File may be scanned/image-based.")

    return full_text


# ---------------------------------------------------------------------------
# Claude API prompt engineering
# ---------------------------------------------------------------------------

# This is the system prompt for the APAS parser.
# It is one of three distinct system prompts in GopherPath:
#   1. APAS parser (this one) — extracts structured data from a document
#   2. Prerequisite parser — extracts logic from course description text
#   3. Plan explainer/chat — explains scheduling decisions to the student
#
# Key prompt engineering decisions made here:
#   - We ask for JSON only, no prose, to make parsing reliable
#   - We define the exact schema we expect, with field names and types
#   - We give explicit instructions for edge cases (withdrawn courses,
#     AP credits, transfer credits, in-progress courses)
#   - We tell Claude what to do when data is ambiguous or missing
#   - We constrain Claude to only extract what's in the document,
#     never infer or hallucinate course information

APAS_SYSTEM_PROMPT = """You are a precise academic document parser. Your job is to extract structured data from a UMN APAS (Academic Progress Audit System) report.

You must respond with valid JSON only. No prose, no explanation, no markdown code fences. Just the raw JSON object.

Extract the following information exactly as it appears in the document:

{
  "student": {
    "name": "string",
    "student_id": "string or null",
    "major": "string",
    "college": "string or null",
    "catalog_year": "string or null",
    "expected_graduation": null (ALWAYS null — see rule 9),
    "advisor": "string or null (use first advisor if multiple)"
  },
  "credits": {
    "earned": number,
    "in_progress": number,
    "needed": number,
    "total_required": number
  },
  "gpa": {
    "overall": number or null,
    "major": number or null
  },
  "completed_courses": [
    {
      "term": "string (e.g. 'F 24', 'SP25', 'SP23')",
      "subject": "string (e.g. 'CSCI')",
      "number": "string (e.g. '1133')",
      "credits": number,
      "grade": "string (e.g. 'A', 'B+', 'W', 'T', 'T4', 'T5')",
      "title": "string",
      "is_transfer": boolean,
      "is_ap": boolean,
      "is_withdrawn": boolean
    }
  ],
  "in_progress_courses": [
    {
      "term": "string",
      "subject": "string",
      "number": "string",
      "credits": number,
      "title": "string"
    }
  ],
  "remaining_requirements": [
    {
      "category": "string (e.g. 'Writing Intensive', 'Liberal Education', 'Major Requirements')",
      "description": "string (brief description of what is needed)",
      "credits_needed": number or null,
      "courses_needed": number or null,
      "options": ["string"] (list of specific course codes if given, e.g. ['STAT 5102', 'STAT 4051'])
    }
  ]
}

Rules:
1. completed_courses includes ALL courses that appear in the transcript section, including withdrawn (W grade) and failed courses. Set is_withdrawn=true for W grades.
2. AP exam credits have grades like 'T', 'T4', 'T5' — set is_ap=true for these.
3. Transfer credits have similar grade patterns — use context to determine is_transfer.
4. in_progress_courses are marked 'AF FT' (authorized future term) or 'IP' in the document.
5. For remaining_requirements, extract every requirement section that says 'Needs:' — these are incomplete requirements.
6. If a field is not present in the document, use null, not a placeholder string.
7. Course numbers may have suffixes like 'W' (writing intensive) or 'H' (honors) — preserve them exactly.
8. Do not infer or add any information not explicitly present in the document.
9. The "Expected Grad Term" field on UMN APAS reports is ALWAYS blank — it exists as a column header but is never populated. Do NOT attempt to extract it and do NOT substitute the Catalog Year, last enrollment term, or any other date. ALWAYS return null for expected_graduation. The student will be asked for their graduation date separately.
"""

def parse_apas_with_claude(apas_text):
    """
    Sends extracted APAS text to Claude API and returns structured JSON.

    We use claude-sonnet-4-20250514 — it's the right balance of capability
    and cost for document parsing. The APAS text is ~3,000-5,000 tokens,
    and we expect ~1,000-2,000 tokens back.

    We set temperature=0 because this is an extraction task, not a
    creative one. We want deterministic, consistent output.
    """
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8096,
        temperature=0,
        system=APAS_SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": f"Parse this APAS report and return the structured JSON:\n\n{apas_text}"
            }
        ]
    )

    raw_response = message.content[0].text

    # Strip markdown code fences if Claude added them despite instructions
    # This is a defensive measure — the prompt says not to, but we handle it anyway
    if raw_response.startswith("```"):
        lines = raw_response.split("\n")
        raw_response = "\n".join(lines[1:-1])

    try:
        return json.loads(raw_response)
    except json.JSONDecodeError as e:
        raise ValueError(f"Claude returned invalid JSON: {e}\n\nRaw response:\n{raw_response}")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_parsed_apas(data):
    """
    Basic validation of the parsed APAS data.

    We check that required fields are present and values are sensible.
    This catches prompt failures before bad data reaches the database.

    Returns a list of validation errors. Empty list means valid.
    """
    errors = []

    # Required top-level keys
    for key in ["student", "credits", "gpa", "completed_courses", "remaining_requirements"]:
        if key not in data:
            errors.append(f"Missing required field: {key}")

    if "student" in data:
        if not data["student"].get("major"):
            errors.append("Missing student major")
        # expected_graduation is ALWAYS blank on UMN APAS reports (the column
        # exists but is never populated), so a missing value is expected, not
        # an error — the student is prompted to enter it on the confirm screen.
        if not data["student"].get("expected_graduation"):
            print("[parse_apas] WARNING: Expected graduation term not found in "
                  "APAS — student will be prompted to enter it manually.",
                  file=sys.stderr)

    if "credits" in data:
        earned = data["credits"].get("earned", 0)
        if earned < 0 or earned > 300:
            errors.append(f"Suspicious earned credits value: {earned}")

    if "completed_courses" in data:
        if not isinstance(data["completed_courses"], list):
            errors.append("completed_courses must be a list")
        elif len(data["completed_courses"]) == 0:
            errors.append("No completed courses found — parser may have failed")

    return errors


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_apas(pdf_path):
    """
    Full pipeline: PDF → text → Claude → validated JSON.
    Returns the parsed data dict.
    """
    print(f"Extracting text from {pdf_path}...", file=sys.stderr)
    apas_text = extract_text_from_pdf(pdf_path)
    print(f"Extracted {len(apas_text)} characters across PDF pages.", file=sys.stderr)

    print(f"Sending to Claude API for parsing...", file=sys.stderr)
    parsed = parse_apas_with_claude(apas_text)

    print(f"Validating parsed data...", file=sys.stderr)
    errors = validate_parsed_apas(parsed)

    if errors:
        print(f"VALIDATION WARNINGS:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
    else:
        print(f"Validation passed.", file=sys.stderr)

    return parsed


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 scrapers/parse_apas.py path/to/apas.pdf")
        sys.exit(1)

    pdf_path = sys.argv[1]
    result = parse_apas(pdf_path)

    print(json.dumps(result, indent=2))