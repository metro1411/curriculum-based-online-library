# Curriculum, Prospectus, DIT AI and SOMA

The live DIT prospectus is the source of truth for every department, programme and module. This document covers how it gets there, what happens to existing content when it changes, how DIT AI uses it, and the SOMA boundary.

> **Data honesty.** The repository contains no real DIT module names, codes, credits, prerequisites or programmes beyond the original seed data, and no SOMA endpoints or credentials. Seeded modules are labelled *not yet verified against the official DIT prospectus*. Samples and test fixtures are marked **DEMO**. Real curriculum enters only through a prospectus upload.

## 1. Prospectus upload

Only a Head of Department (`role="department_head"`) can upload, at `/department/prospectus`.

```
upload → read → check → swap (one transaction) → live
                  └─ any problem → stopped, nothing changes
```

1. **Upload.** PDF, DOCX, TXT, MD or CSV, with a title and academic year (`2026/2027`). The file type is checked against its contents. The original is stored privately with its SHA-256 (`ProspectusDocument`).
2. **Read.** CSV uses the template in `docs/samples/` (required columns: `department, programme, nta_level, semester, module_code, module_name`; optional: `year_label, module_type, credits, prerequisites, description`). PDF, DOCX and text go through `extract_prospectus`, which reads the whole book (up to 3 million characters) the way the DIT prospectus prints it:
   - `6.1 DEPARTMENT OF …` sets the department; `8.1 MWANZA CAMPUS` makes the campus its own department;
   - each award heading (`(a) BASIC TECHNICIAN CERTIFICATE (BTC) IN CIVIL ENGINEERING (NTA LEVEL 4)`, even wrapped over two lines) gives the NTA level and the award. The programme is the field after the award, so BTC, TC, OD, HD and Bachelor in Civil Engineering become one programme, *Civil Engineering*, with levels 4 to 8, and each level is labelled with its award;
   - `Semester I` to `Semester VI` (also `SEMISTER`, `Semester 2 Modules`, `Core Modules for Semester I`);
   - `FUNDAMENTAL`, `CORE` and `ELECTIVE MODULES` set the module type, as does a *Class* column;
   - `CODE Title Credits` lines, with or without an S/N column, titles wrapped over several lines, and codes printed as `GST05112` or `SLT P 06101` (stored as `GST 05112`, `SLTP 06101`);
   - page footers, totals and staff lists are ignored. A module printed twice identically in the same semester is kept once.
   Programmes printed without an NTA level (vocational NVA awards, the general course programmes) are skipped and listed as notes. Rows are staged as `CurriculumEntry` rows on a `CurriculumVersion`.
3. **Check** (`validate_version`). Any of these stops the upload:
   - no readable text, or no modules found;
   - a module missing its code, name, department, programme, NTA level or semester;
   - a bad code, a level outside 1–10, a semester outside 1–6, or credits outside 0–100;
   - a prerequisite that is unknown, refers to itself or forms a loop;
   - a CSV line that cannot be read;
   - the same file is already live;
   - more than half of the live modules would be retired, unless the HOD ticks *Confirm a large change*.
4. **Swap** (`publish_upload`). In one transaction:
   - every live module in every department is archived;
   - each staged row becomes a new live module (`provenance="prospectus"`) under the version's academic year, which becomes current in its department;
   - prerequisites are linked from the new prospectus;
   - content is reallocated (section 2);
   - departments, programmes, levels and semesters with no live modules are hidden, never deleted.
5. **Result.** `/department/prospectus/<id>` shows each module's outcome, students who need placing, hidden departments, and notes from the read: codes printed more than once in a programme with different titles, and skipped programmes. Notes never stop a publish; they point at lines DIT may want to correct in the prospectus. A stopped upload shows its problems instead.

Every live change is recorded in the version's undo journal. **Undo** (`undo_last_publish`) reverses the latest publish exactly and brings back the version it replaced. Undo is refused once lecturers have added resources, topics or claims to the new modules. All steps are audited (`prospectus.uploaded`, `prospectus.published`, `prospectus.stopped`, `prospectus.undone`).

Version statuses: `published` (live), `archived` (replaced), `failed` (stopped), `undone`.

## 2. Reallocation

Module code links an old module to its replacement. Codes are compared in upper case with spaces collapsed.

| Outcome | When | Effect |
| --- | --- | --- |
| Carried over | Code in the new prospectus, same department | Content moves to the new module |
| Moved | Code in the new prospectus, different department | Content moves; report shows from and to |
| New | Code was not live before | Nothing to move |
| Retired | Code missing from the new prospectus | Archived; its resources stay with it, still visible to its lecturers |
| Needs placing | Code appears in several programmes, none of them the old one | Resources stay on the archived module; lecturers get every match |

What moves with a module: resources (verification unchanged), lecturer assignments (status unchanged), topics and student module registrations. History (views, downloads, saved items, AI conversations) stays on the archived module.

Students are re-pointed to the matching programme (same row, or the only live programme with the same name), NTA level, semester and the new academic year. Students with no match are listed on the result page for the HOD to place at `/department/students`.

## 3. Roles

| Role | Curriculum rights |
| --- | --- |
| Head of Department | Uploads, publishes and undoes the prospectus for the whole institution. Sees the live modules of their department, places their students, approves lecturers and claims. |
| Lecturer | Publishes resources and topics in approved modules only. Edits only their own uploads. |
| Student | Sees published modules of their placement or registrations, and verified resources. |

The former Curriculum Administrator role is retired. Start-up migration turns existing `admin` accounts into heads of department, and deactivates any without a department.

## 4. DIT AI

`curriculum_context.build_context` gathers the student's department, programme, level, semester, academic year and modules. Retrieval priority:

1. Lecturer-approved resources for the selected or current module.
2. Live curriculum records: the student's modules in full, any other live module by name or code, and the module outline of any programme the question names (`retrieve_programmes`), narrowed to the award it names ("ordinary diploma", "bachelor") when it names one.
3. Live prospectus text, including printed rules and regulations (`retrieve_prospectus`, top 4 passages). Stopped and replaced uploads are never used. `prospectus_chunks` splits the book into passages that each start with their place, for example `[Chapter Five: Examination Regulations › 9.0 Absence from Examination]`. Rare words score higher than common ones, words in that heading count double, and a few everyday words are widened to the regulation's own terms (*miss* → *absence*, *fail* → *supplementary*, *repeat*).
4. Other approved resources in the student's current modules.
5. General knowledge, optionally web-grounded, always labelled.

The system prompt treats the live prospectus as the official source for modules and DIT rules, and tells the model to say so when the prospectus does not cover a question.

Each answer is labelled and the label is stored on `AIMessage`:

| Label | Shown text |
| --- | --- |
| `lecturer_content` | Lecturer-approved DIT learning content |
| `dit_curriculum` | DIT curriculum information |
| `general_academic` | General academic knowledge — related to your modules but not found in DIT materials |
| `outside_curriculum` | General information — not identified as part of your current DIT curriculum. |

**Attachments.** `/ai/ask` accepts one image (PNG, JPEG, WebP) or PDF up to 8 MB as base64. `ai_engine.read_attachment` checks the type against the file's first bytes; Gemini reads it inline with the question. Attachments are never stored; the saved message records only the file name. Web grounding is off for questions with an attachment.

## 5. Student placement and SOMA

Until SOMA is connected, the HOD sets a student's programme, level, semester, academic year and registered modules at `/department/students/<id>` (audited as `student_context.updated`).

SOMA stays the authoritative, **read-only** record system. `integrations/soma.py` defines:

- `UnconfiguredSomaAdapter`: the default; reports "not configured".
- `HttpSomaAdapter`: transport only. It refuses to run until DIT's official specification supplies the base URL, credentials, `SOMA_STUDENT_RECORD_PATH`, `AUTH_HEADERS` and `RESPONSE_MAPPER`.
- `MockSomaAdapter`: DEMO only, reads `SOMA_MOCK_DATA_FILE`, refused in production.

`academic_context.sync_from_soma` matches records against published curriculum and never creates programmes or modules. On any failure the student's context is unchanged and an `IntegrationSyncLog` row is written.

To connect SOMA: set `SOMA_ADAPTER=http` with its URL, credentials and record path; implement `AUTH_HEADERS` and `RESPONSE_MAPPER` from the official specification; add contract tests from DIT's sample responses.

| Variable | Default | Notes |
| --- | --- | --- |
| `SOMA_ADAPTER` | `none` | `none`, `http` or `mock` (DEMO) |
| `SOMA_API_BASE_URL` | empty | From DIT |
| `SOMA_API_KEY`, `SOMA_CLIENT_ID`, `SOMA_CLIENT_SECRET` | empty | Secrets; never commit |
| `SOMA_STUDENT_RECORD_PATH` | empty | Path template containing `{student_id}` |
| `SOMA_TIMEOUT_SECONDS` | `10` | HTTP timeout |
| `SOMA_MOCK_DATA_FILE` | empty | DEMO JSON keyed by registration number |

## 6. API

All endpoints require sign-in and keep role scoping.

| Method and path | Who | Returns |
| --- | --- | --- |
| `GET /api/v1/modules` | all | Includes `credits`, `prerequisites`, `curriculum_version`, `provenance` |
| `GET /api/v1/resources[?module_id=]`, `/resources/<id>` | all | Includes `learning_objectives`, `lecturer_remarks`, `topic`, curriculum path |
| `POST /api/v1/ai/ask` | student | Includes `context_label`, `context_label_text`, `curriculum_context` |
| `GET /api/v1/me/academic-context` | student | Placement, modules, source, missing fields |
| `GET /api/v1/me/recommendations` | student | Recommendations with their evidence |
| `GET /api/v1/curriculum/versions` | all | Students: live and replaced. HOD: all, including stopped |
| `GET /api/v1/curriculum/versions/<id>` | all | Detail; staged entries for HODs; 404 for students on stopped uploads |

## 7. Data model notes

Schema changes are additive and applied by `app._apply_schema_migrations` on start-up (SQLite and Postgres, no Alembic). `curriculum_versions` gained `allocation_report_json`, `undo_journal_json`, `undone_by_id` and `undone_at`.

## 8. Tests

`python -m pytest` runs on a disposable database. Prospectus tests undo every publish they make, so the seed curriculum is unchanged afterwards.

- `tests/test_prospectus_workflow.py`: publish, reallocation, undo, every stop condition, parsing and validation.
- `tests/test_rag_curriculum.py`: retrieval priority, programme outlines, prospectus rules, labels, attachments.
- `tests/test_permissions.py`: HOD-only prospectus routes, lecturer ownership, student data isolation, API visibility.
- `tests/test_soma_integration.py`: adapter boundary and HOD placement.
