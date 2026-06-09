-- =============================================================================
-- GopherPath Database Schema
-- =============================================================================
-- Design principles:
--   1. Generic naming ("institution", "course") not hardcoded to UMN.
--      UMN is the first institution we populate, but the schema works for any.
--   2. All UMN-specific data lives in the data, not the structure.
--   3. Prerequisites are stored as structured logic, not raw text.
--      Raw text is preserved separately for auditing.
-- =============================================================================


-- -----------------------------------------------------------------------------
-- INSTITUTIONS
-- One row per university. Every other table references this.
-- -----------------------------------------------------------------------------
CREATE TABLE institutions (
    id              SERIAL PRIMARY KEY,
    code            VARCHAR(20) UNIQUE NOT NULL,  -- e.g. 'UMNTC'
    name            VARCHAR(255) NOT NULL,         -- e.g. 'University of Minnesota Twin Cities'
    created_at      TIMESTAMP DEFAULT NOW()
);


-- -----------------------------------------------------------------------------
-- SUBJECTS
-- Department/subject codes, e.g. CSCI, MATH, HIST
-- -----------------------------------------------------------------------------
CREATE TABLE subjects (
    id              SERIAL PRIMARY KEY,
    institution_id  INTEGER REFERENCES institutions(id) ON DELETE CASCADE,
    code            VARCHAR(20) NOT NULL,   -- e.g. 'CSCI'
    name            VARCHAR(255) NOT NULL,  -- e.g. 'Computer Science'
    UNIQUE(institution_id, code)
);


-- -----------------------------------------------------------------------------
-- COURSES
-- One row per unique course. Not per section, not per semester.
-- credits_variable: true means the course can be taken for a range of credits.
-- source_id: the internal ID from the source system (Schedule Builder crse_id).
--   Stored so we can re-sync data without creating duplicates.
-- -----------------------------------------------------------------------------
CREATE TABLE courses (
    id                  SERIAL PRIMARY KEY,
    institution_id      INTEGER REFERENCES institutions(id) ON DELETE CASCADE,
    subject_id          INTEGER REFERENCES subjects(id) ON DELETE CASCADE,
    source_id           INTEGER,                   -- crse_id from Schedule Builder
    subject_code        VARCHAR(20) NOT NULL,      -- e.g. 'CSCI'
    catalog_number      VARCHAR(20) NOT NULL,      -- e.g. '1133'
    title               VARCHAR(500) NOT NULL,
    description         TEXT,
    credits             NUMERIC(4,2),              -- standard credit value
    min_credits         NUMERIC(4,2),
    max_credits         NUMERIC(4,2),
    credits_variable    BOOLEAN DEFAULT FALSE,
    course_repeatable   BOOLEAN DEFAULT FALSE,
    acad_career         VARCHAR(20),               -- 'UGRD' or 'GRAD'
    -- Offering frequency derived by querying multiple terms
    offered_fall        BOOLEAN DEFAULT FALSE,
    offered_spring      BOOLEAN DEFAULT FALSE,
    offered_summer      BOOLEAN DEFAULT FALSE,
    -- Raw prerequisite text extracted from description, before parsing
    prereq_raw          TEXT,
    created_at          TIMESTAMP DEFAULT NOW(),
    updated_at          TIMESTAMP DEFAULT NOW(),
    UNIQUE(institution_id, subject_code, catalog_number)
);

-- NOTE: Liberal Ed requirement mapping pending.
-- IDs from CourseDog CSV do not directly map to LE requirement names.
-- Solution: filter catalog by each LE requirement, export CSV, match IDs.
-- The 11 LE requirements are documented in scrapers/import_courses.py.

-- -----------------------------------------------------------------------------
-- COURSE ATTRIBUTES
-- Liberal Education requirements and other flags (CEL, WI, etc.)
-- Stored as key-value pairs so we can handle any attribute without schema changes.
-- -----------------------------------------------------------------------------
CREATE TABLE course_attributes (
    id          SERIAL PRIMARY KEY,
    course_id   INTEGER REFERENCES courses(id) ON DELETE CASCADE,
    attribute   VARCHAR(50) NOT NULL,   -- e.g. 'CLE', 'WI', 'CEL'
    value       VARCHAR(100),           -- e.g. 'AH', 'ENV', 'FIELD STDY'
    name        VARCHAR(255)            -- human-readable: 'Artistic Expression'
);


-- -----------------------------------------------------------------------------
-- PREREQUISITES
-- Structured prerequisite logic after Claude API parsing.
--
-- Prerequisites form a tree. Each row is one node in that tree.
-- operator: 'AND', 'OR', or NULL (leaf node = an actual course requirement)
-- parent_id: NULL means this is the root node for a course's prereq tree.
--
-- Example: CSCI 4041 requires (CSCI 2011 OR CSCI 2021) AND MATH 2243
-- Stored as:
--   row 1: course=4041, operator='AND', parent=NULL          <- root
--   row 2: course=4041, operator='OR',  parent=1             <- left branch
--   row 3: course=4041, req_subject='MATH', req_number='2243', parent=1  <- right leaf
--   row 4: course=4041, req_subject='CSCI', req_number='2011', parent=2  <- leaf
--   row 5: course=4041, req_subject='CSCI', req_number='2021', parent=2  <- leaf
-- -----------------------------------------------------------------------------
CREATE TABLE prerequisites (
    id              SERIAL PRIMARY KEY,
    course_id       INTEGER REFERENCES courses(id) ON DELETE CASCADE,
    parent_id       INTEGER REFERENCES prerequisites(id) ON DELETE CASCADE,
    operator        VARCHAR(10),        -- 'AND', 'OR', or NULL for leaf nodes
    req_subject     VARCHAR(20),        -- subject code of required course (leaf only)
    req_number      VARCHAR(20),        -- catalog number of required course (leaf only)
    min_grade       VARCHAR(5),         -- e.g. 'C-' if a minimum grade is specified
    note            TEXT                -- e.g. 'instructor consent' for non-course prereqs
);


-- -----------------------------------------------------------------------------
-- GRADE DISTRIBUTIONS
-- One row per course per term per instructor.
-- Will be populated later from Gopher Grades data or UMN data request.
-- Keeping the table in schema now so foreign keys are consistent.
-- -----------------------------------------------------------------------------
CREATE TABLE grade_distributions (
    id              SERIAL PRIMARY KEY,
    course_id       INTEGER REFERENCES courses(id) ON DELETE CASCADE,
    term            INTEGER NOT NULL,   -- e.g. 1269 (Schedule Builder term code)
    term_label      VARCHAR(50),        -- e.g. 'Fall 2026'
    instructor      VARCHAR(255),
    enrollment      INTEGER,
    grade_a         NUMERIC(5,2),       -- percentage of A grades
    grade_b         NUMERIC(5,2),
    grade_c         NUMERIC(5,2),
    grade_d         NUMERIC(5,2),
    grade_f         NUMERIC(5,2),
    grade_s         NUMERIC(5,2),       -- satisfactory
    grade_n         NUMERIC(5,2),       -- non-satisfactory
    grade_w         NUMERIC(5,2),       -- withdrawal
    median_gpa      NUMERIC(3,2),
    created_at      TIMESTAMP DEFAULT NOW()
);


-- -----------------------------------------------------------------------------
-- INDEXES
-- Added on every foreign key and any column we'll filter on frequently.
-- Without these, queries slow down dramatically once we have 10k+ courses.
-- -----------------------------------------------------------------------------
CREATE INDEX idx_courses_institution    ON courses(institution_id);
CREATE INDEX idx_courses_subject        ON courses(subject_code);
CREATE INDEX idx_courses_source_id      ON courses(source_id);
CREATE INDEX idx_course_attrs_course    ON course_attributes(course_id);
CREATE INDEX idx_course_attrs_attr      ON course_attributes(attribute);
CREATE INDEX idx_prereqs_course         ON prerequisites(course_id);
CREATE INDEX idx_prereqs_parent         ON prerequisites(parent_id);
CREATE INDEX idx_grade_dist_course      ON grade_distributions(course_id);
CREATE INDEX idx_grade_dist_term        ON grade_distributions(term);