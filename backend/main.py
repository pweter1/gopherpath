"""
main.py
-------
GopherPath FastAPI backend.

This is the main entry point for the GopherPath API server.
It handles file uploads, APAS parsing, and will eventually
serve the constraint optimizer and chat interface.

Run locally with:
    uvicorn backend.main:app --reload

The --reload flag restarts the server automatically when you
save changes to any file — essential for development.
"""

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import tempfile
import os
import sys

# Add project root to path so we can import from scrapers/
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

# CORS middleware allows the React frontend (running on localhost:3000)
# to make requests to this backend (running on localhost:8000).
# In production this will be locked down to the actual frontend domain.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:3001"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/")
def root():
    """
    Health check endpoint.
    Returns a simple status message to confirm the server is running.
    """
    return {"status": "ok", "service": "GopherPath API"}


# ---------------------------------------------------------------------------
# APAS parsing endpoint
# ---------------------------------------------------------------------------

@app.post("/parse-apas")
async def parse_apas_endpoint(file: UploadFile = File(...)):
    """
    Accepts a UMN APAS report (PDF) and returns structured JSON.

    The client uploads a PDF file. This endpoint:
      1. Validates the file is a PDF
      2. Saves it to a temporary file (pdfplumber needs a file path)
      3. Runs the APAS parser (text extraction + Claude API)
      4. Validates the parsed output
      5. Returns the structured JSON to the client

    The temporary file is deleted after parsing regardless of success
    or failure — we never store the raw PDF on the server.

    Returns:
        200: Parsed APAS data as JSON
        400: Invalid file type or validation failure
        500: Parser error
    """

    # Validate file type
    if not file.filename.endswith(".pdf"):
        raise HTTPException(
            status_code=400,
            detail="Only PDF files are accepted. Please export your APAS as a PDF from One Stop."
        )

    # Save uploaded file to a temp file
    # We use a temp file because pdfplumber requires a file path, not a stream
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            content = await file.read()
            tmp.write(content)
            tmp_path = tmp.name

        # Run the parser
        parsed_data = parse_apas(tmp_path)

        # Validate the output
        errors = validate_parsed_apas(parsed_data)
        if errors:
            raise HTTPException(
                status_code=400,
                detail={
                    "message": "APAS parsed but failed validation",
                    "errors": errors
                }
            )

        return {
            "status": "success",
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
        # Always clean up the temp file
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)