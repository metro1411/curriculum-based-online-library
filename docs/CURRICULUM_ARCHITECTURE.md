# Curriculum, Prospectus and SOMA Integration

This document describes how Smart DIT Learning Hub uses the DIT prospectus as
its curriculum backbone, how curriculum administrators move a prospectus from
upload to publication, how DIT AI uses that curriculum, and how the hub is
prepared, but not yet connected, to integrate with SOMA.

> **Data honesty.** The repository contains **no real DIT module names,
> codes, credits, prerequisites, programmes or departments** beyond the
> original seed data, and **no SOMA endpoints, credentials or schemas**. The
> seeded Electrical Engineering modules are kept for backward compatibility
> and labelled *"not yet verified against the official DIT prospectus"*.
> Sample files and test fixtures are marked **DEMO**. Real curriculum enters
> the system only through the review workflow below.

## 1. Audit summary (before this change)

| Area | What existed | Gap closed |
| --- | --- | --- |
| Curriculum tree | Department → Programme → NtaLevel → Semester → Module, with HOD-managed `AcademicYear` editions | No link to a source prospectus, no credits, no prerequisites, no provenance |
| Roles | student, lecturer, department_head | No institution-wide curriculum administrator |
| Resources | Mapped to a module and optional topic | No learning objectives or lecturer remarks; any lecturer assigned to a module could edit another lecturer's upload |
| DIT AI | Keyword retrieval over lecturer resources, Gemini answers, optional web grounding | No awareness of the student's curriculum position; out-of-curriculum answers were not labelled |
| Student records | Self-registered programme/level/semester | No module registration, no record of where context came from, no SOMA boundary |
| Audit | `governance.record_audit` for HOD and lecturer actions | Prospectus, curriculum and context actions now audited too |

The existing tree is **reused**: publishing a prospectus creates or updates
rows in the same tables HOD tools and student navigation already use, so every
existing page keeps working.

## 2. Architecture

```
            ┌────────────────────────┐
 upload ──▶ │ ProspectusDocument     │  original file (private storage) + sha256
            │  └ ProspectusChunk     │  extracted text, for AI after publication
            └──────────┬─────────────┘
                       │ extract (heuristics) / import CSV / manual entry
            ┌──────────▼─────────────┐
            │ CurriculumVersion      │  draft → validated → approved → published → archived
            │  ├ DraftProgramme      │  staging: never visible to students
            │  └ CurriculumEntry     │  one row per module, review_status per row
            └──────────┬─────────────┘
                       │ publish (validated + approved only)
            ┌──────────▼─────────────────────────────────────────────────┐
            │ Live tree: Department → Programme → NtaLevel → Semester →  │
            │ Module (credits, provenance="prospectus",                  │
            │ curriculum_version_id) + ModulePrerequisite + AcademicYear │
            └──────────┬─────────────────────────────────────────────────┘
                       │
   ┌───────────────────┼─────────────────────┬────────────────────────┐
   ▼                   ▼                     ▼                        ▼
 Student navigation  Lecturer resources   DIT AI (curriculum_      Student academic
 and dashboard       mapped to modules    context.py + ai_engine)  context (internal / SOMA)
```

Key modules:

| File | Responsibility |
| --- | --- |
| `curriculum_service.py` | Prospectus storage and text extraction, candidate extraction, CSV import, validation, and the workflow transitions (approve, publish, archive, delete) |
| `routes/admin.py`, `templates/admin/` | Curriculum administrator workspace |
| `permissions.py` | Central role checks (`can_manage_curriculum`, `can_edit_resource`, `can_view_module_students`, …) |
| `academic_context.py` | Student academic context, module registrations, SOMA sync |
| `integrations/soma.py` | Read-only SOMA adapter boundary |
| `curriculum_context.py` | Curriculum context, curriculum and prospectus retrieval, and question classification for DIT AI |
| `recommendations.py` | Evidence-based study recommendations |

## 3. Database changes

All changes are additive. There is no Alembic in this project, so
`app._apply_schema_migrations` runs `create_all()` and then idempotent
`ALTER TABLE … ADD COLUMN` statements on start-up (SQLite and Postgres).
Upgrading a database created by the previous release was verified: existing
modules are back-filled as `legacy_seed` (seed data) or `hod_manual` (HOD-created).

New columns:

| Table | Column | Purpose |
| --- | --- | --- |
| `modules` | `credits` (numeric 6,2, nullable) | Credit value from an approved prospectus; empty when unknown |
| `modules` | `curriculum_version_id` | The published version that created/last updated the module |
| `modules` | `provenance` | `legacy_seed`, `hod_manual` or `prospectus` |
| `nta_levels` | `year_label` | Optional label such as the prospectus's year name |
| `resources` | `learning_objectives`, `lecturer_remarks` | Lecturer-supplied mapping detail |
| `ai_messages` | `context_label` | Which scope label the answer carried |

New tables:

| Table | Purpose |
| --- | --- |
| `prospectus_documents` | Uploaded prospectus: stored file, sha256, extraction status and text |
| `prospectus_chunks` | Extracted text chunks (retrieved by AI only for published versions) |
| `curriculum_versions` | A prospectus edition moving through the workflow; `is_demo` marks training data |
| `draft_programmes` | Programme structure inside a version (levels, semesters per level, year labels) |
| `curriculum_entries` | Staged module rows with origin, source reference and review status |
| `module_prerequisites` | Live prerequisite links (unique, never self-referencing) |
| `student_academic_contexts` | Where a student's context came from: `self_registration`, `internal_admin` or `soma` |
| `student_module_registrations` | Modules a student is registered for (admin or SOMA) |
| `integration_sync_logs` | Every SOMA sync attempt and its outcome |

## 4. Prospectus workflow

Roles: only a **Curriculum Administrator** (`role="admin"`) can upload,
validate, approve, publish or archive. HODs can read versions through the API.
Students see published curriculum only.

1. **Upload** (`/admin/prospectus`). PDF, DOCX, TXT, MD or CSV. The file type is
   checked against its contents, stored privately, hashed and audited
   (`prospectus.uploaded`). If text extraction fails, the document is kept and
   the version can still be filled in by CSV or by hand.
2. **Extract.** `extract_candidates` applies conservative patterns to the text
   (department, programme, NTA level, semester and `CODE Name` module lines).
   Every candidate is stored as `origin="extracted"`, `review_status="pending"`,
   with its source line, so a person must confirm it. Prospectus layouts vary,
   so an empty or partial extraction is expected and the CSV route is the
   reliable one.
3. **Import CSV** (optional). Use `docs/samples/prospectus_import_template.csv`.
   Required columns: `department, programme, nta_level, semester, module_code,
   module_name`. Optional: `year_label, module_type (core|general_studies),
   credits, prerequisites (codes separated by , or ;), description`.
   `docs/samples/DEMO_prospectus_import.csv` shows the format with DEMO rows.
4. **Review.** On the version page, the administrator edits or deletes each
   entry and marks it *reviewed*. Any edit to a validated or approved version
   returns it to *draft*.
5. **Validate.** Errors block approval: missing fields, invalid code format,
   NTA level not in the programme, a semester outside the programme's range,
   duplicate module codes or names within a programme, unknown or
   self-referencing prerequisites, prerequisite cycles, unreviewed entries,
   credits outside 0–100, and a code already used by a different live module.
   Warnings (for example, missing credits) are shown but don't block.
6. **Approve.** Needs a fresh, passing validation. Records who approved it and when.
7. **Publish.** Creates or updates departments, programmes, levels, semesters,
   the `AcademicYear` and modules (`provenance="prospectus"`), then links
   prerequisites. *Make current* switches the department's current academic
   year. Live modules are matched by code, so publishing never deletes them.
8. **Archive.** A published version can be archived. Its modules stay
   available, because students and resources depend on them.

Safeguards: published and archived versions cannot be edited or deleted. Only
draft, validated or approved versions can be deleted. Every transition is
audited (`curriculum.version_created`, `…validated`, `…approved`,
`…published`, `…archived`, `…deleted`, plus entry and programme changes).

## 5. Resources mapped to modules

Lecturers upload resources to a module they are approved to teach (the existing
wizard). The form shows the full curriculum path and accepts **learning
objectives** and **lecturer remarks**, which appear on the student resource
page and in the API, and are indexed for DIT AI. `resource.created` audit rows
include programme, NTA level, semester and module code, and verifying a
resource records `resource.approved`.

**Ownership:** `permissions.can_edit_resource` allows editing and deleting only by
the uploader, and only while they still teach that module. Other lecturers on
the module see "By <name>" without edit controls. Unverified resources are
never shown to students.

## 6. Curriculum-aware DIT AI

`curriculum_context.build_context` gathers the student's department, programme,
level, semester, academic year and current modules (registered modules first).
Retrieval priority:

1. Lecturer-approved resources for the selected or current module
2. Structured curriculum data: module record, topics, credits, prerequisites
3. Published DIT prospectus text (never draft versions)
4. Other approved resources in the student's current modules (weighted 0.85 when a module is selected)
5. General knowledge, optionally web-grounded, always labelled

Each answer is classified and labelled. The label is stored on `AIMessage` and
returned by `/ai/ask` and `/api/v1/ai/ask`:

| Label | Shown text |
| --- | --- |
| `lecturer_content` | Lecturer-approved DIT learning content |
| `dit_curriculum` | DIT curriculum information |
| `general_academic` | General academic knowledge — related to your modules but not found in DIT materials |
| `outside_curriculum` | General information — not identified as part of your current DIT curriculum. |

Questions outside the curriculum are **answered, not refused**. The goal is
context, not restriction. If retrieval fails, the assistant degrades to
general guidance and reports `retrieval_error` rather than failing the request.
Modules not yet verified against a prospectus are flagged as such in the prompt.

## 7. Student academic context and SOMA

### Internal mechanism (works today)

`/admin/students/<id>/context` lets a curriculum administrator set a student's
programme, level, semester, academic year and registered modules. Choices are
validated against the live curriculum and audited (`student_context.updated`).
Students see their context, its source and any missing fields on the dashboard
and at `GET /api/v1/me/academic-context`.

### SOMA adapter boundary (not connected)

SOMA remains the authoritative system for official student records. The hub
treats it as **read-only** and never writes to it. `integrations/soma.py` defines:

- `SomaAdapter`: the interface (`get_student_record`, `health_check`).
- `UnconfiguredSomaAdapter`: the default. Reports "not configured".
- `HttpSomaAdapter`: transport and error handling only. It **refuses to run**
  until all of the following come from DIT's official API specification:
  base URL, credentials, the student-record path (`SOMA_STUDENT_RECORD_PATH`),
  the authentication header scheme (`AUTH_HEADERS`) and the response mapping
  (`RESPONSE_MAPPER`). `AUTH_HEADERS` and `RESPONSE_MAPPER` are deliberately
  `None`: implementing them needs the real contract, which this project does
  not have.
- `MockSomaAdapter`: **DEMO/testing only**. It reads records from
  `SOMA_MOCK_DATA_FILE` and is refused when `APP_ENV=production`.

`academic_context.sync_from_soma` matches the SOMA record against **published**
curriculum by name and code, and never creates programmes or modules. On any
failure (not configured, unauthorised, unavailable, not found, unexpected
response or unmatched programme) the student's existing context is left
unchanged, a staff-safe message is shown, and an `IntegrationSyncLog` row is
written. Successful syncs are audited (`student_context.soma_synced`).

**To connect SOMA**, obtain DIT's official API documentation and an authorised
service account, then:

1. Set `SOMA_ADAPTER=http`, `SOMA_API_BASE_URL`, the credentials and `SOMA_STUDENT_RECORD_PATH`.
2. Implement `AUTH_HEADERS(adapter) -> dict` exactly as the specification describes.
3. Implement `RESPONSE_MAPPER(payload) -> SomaStudentRecord` for the documented response.
4. Add contract tests using sample responses supplied by DIT.

## 8. Study recommendations

`recommendations.curriculum_recommendations` compares a student's recorded
study activity (learning events and resource views over 14 days) across their
current modules. For example: *"You have low recent activity in X compared
with your other current modules (0 vs an average of 4.5 study actions in 14
days)."* Each recommendation carries its evidence (actions, average, questions
asked, verified resources, basis). The UI shows this under **Why this?**.
Recommendations never claim to measure marks, ability or assessment results,
because the hub does not hold that data.

## 9. API additions

All endpoints require sign-in and keep existing scoping (students see only
their programme or registered modules and verified resources).

| Method and path | Who | Returns |
| --- | --- | --- |
| `GET /api/v1/modules` | all | Now includes `credits`, `prerequisites`, `curriculum_version`, `provenance` |
| `GET /api/v1/resources[?module_id=]`, `/resources/<id>` | all | Now includes `learning_objectives`, `lecturer_remarks`, `topic`, `curriculum` path |
| `POST /api/v1/ai/ask` | student | Now includes `context_label`, `context_label_text`, `curriculum_context` |
| `GET /api/v1/me/academic-context` | student | Department, programme, level, semester, year, modules, source, missing fields |
| `GET /api/v1/me/recommendations` | student | Recommendations with evidence and the evidence note |
| `GET /api/v1/curriculum/versions` | all | Students: published only. Admin and HOD: all statuses |
| `GET /api/v1/curriculum/versions/<id>` | all | Version detail; entries for admin and HOD; 404 for students on unpublished versions |

## 10. Environment variables

| Variable | Default | Notes |
| --- | --- | --- |
| `SOMA_ADAPTER` | `none` | `none`, `http` (needs the official contract) or `mock` (DEMO, disabled in production) |
| `SOMA_API_BASE_URL` | empty | Placeholder until DIT provides it |
| `SOMA_API_KEY` | empty | Secret. Never commit |
| `SOMA_CLIENT_ID` / `SOMA_CLIENT_SECRET` | empty | Secret. Only if DIT's scheme uses them |
| `SOMA_STUDENT_RECORD_PATH` | empty | Documented path template, e.g. containing `{student_id}` |
| `SOMA_TIMEOUT_SECONDS` | `10` | HTTP timeout |
| `SOMA_MOCK_DATA_FILE` | empty | DEMO JSON `{ "<registration number>": {programme, nta_level, semester, academic_year, registered_module_codes} }` |

## 11. Deployment

1. Deploy as usual. Schema changes apply automatically on start-up.
2. Create the first curriculum administrator from a shell on the server:

   ```bash
   flask --app app create-curriculum-admin --email admin@example.org --name "Curriculum Office" --password '<strong password>'
   ```

   The password must be at least 12 characters. Running the command for an
   existing account changes its role to `admin` (audited as `role.changed`).
3. Sign in, open **Prospectus**, upload the official prospectus and create a version.
4. Fill the version by extraction, CSV or manual entry, then review, validate, approve and publish.
5. Ask lecturers to add learning objectives and remarks to key resources.

## 12. Future integration

- **SOMA:** implement the auth and response mapper from the official specification (section 7) and schedule a nightly sync.
- **Assessment data:** recommendations could use marks only if DIT provides an authorised, documented source. Until then they stay activity-based.
- **Prospectus parsing:** extraction is deliberately conservative. A layout-aware parser for the official prospectus format can replace `extract_candidates` without changing the review workflow.
- **Semantic retrieval:** keyword TF-IDF can be swapped for embeddings behind `ai_engine._retrieve_resources` and `curriculum_context.retrieve_*`.

## 13. Tests

`python -m pytest` runs the whole suite on a disposable database. The new suites are:

- `tests/test_prospectus_workflow.py`: upload, extraction, CSV, validation, approve, publish and archive safeguards
- `tests/test_rag_curriculum.py`: retrieval priority, labels, prompt context, draft prospectus exclusion, graceful failure
- `tests/test_permissions.py`: admin-only routes, lecturer ownership, student data isolation, API visibility
- `tests/test_soma_integration.py`: unconfigured behaviour, HTTP error mapping, mock adapter, failures leave context unchanged
- `tests/test_resource_mapping.py`: objectives and remarks, audit details, student visibility, recommendations
