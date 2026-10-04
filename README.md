# Smart DIT Learning Hub

A learning workspace for DIT. The prospectus decides the curriculum, lecturers publish resources against it, and students study with DIT AI.

## Roles

| Role | Does |
| --- | --- |
| Head of Department | Uploads the prospectus, which sets every module in every department. Approves lecturers and module claims. Places students. |
| Lecturer | Claims modules, publishes topics and resources, posts announcements, answers anonymous questions. |
| Student | Browses their modules, studies resources, asks lecturers, and uses DIT AI. |

## How the prospectus works

1. The HOD uploads a text PDF, DOCX or the CSV template (`docs/samples/`) on **Prospectus**.
2. The system reads it and checks every module line.
3. A clean read goes live at once and replaces all live modules. Old modules are archived, never deleted. Resources, lecturers, topics and student placements follow the module code.
4. Any problem publishes nothing and shows the HOD what to fix.
5. The latest publish can be undone.

Details: [docs/CURRICULUM_ARCHITECTURE.md](docs/CURRICULUM_ARCHITECTURE.md).

## DIT AI

Answers come from lecturer materials first, then the live prospectus (any programme's full module list, module records and printed rules), then general knowledge. Every answer is labelled with its source. Students can attach an image or PDF (8 MB max) for DIT AI to read; attachments are not stored.

## Run locally

1. `pip install -r requirements.txt`
2. Optionally copy `.env.example` to `.env` and add a `GEMINI_API_KEY` from [Google AI Studio](https://aistudio.google.com/app/apikey).
3. `python app.py`, then open `http://127.0.0.1:5080` (set `PORT` to change it).

On Windows, `START_HERE.bat` does the same. Tests: `pip install -r requirements-dev.txt`, then `python -m pytest`.

First launch creates demo accounts:

| Role | Email | Password |
| --- | --- | --- |
| Student | `student@dit.ac.tz` | `Student@123` |
| Lecturer | `lecturer@dit.ac.tz` | `Lecturer@123` |

Create the HOD from **Create account** with a `5000…` staff number and `HOD_ACTIVATION_CODE`.

## Configuration

| Variable | Purpose |
| --- | --- |
| `SECRET_KEY`, `DATABASE_URL` | Required in production |
| `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` | Private file storage in production |
| `HOD_ACTIVATION_CODE` | Activates HOD accounts (16+ characters) |
| `GEMINI_API_KEY`, `GEMINI_MODEL` | DIT AI (default model `gemini-3.5-flash`) |
| `GEMINI_ENABLE_WEB_GROUNDING` | `1` allows labelled Google Search help when no DIT source matches |
| `SMTP_*`, `MAIL_FROM`, `PUBLIC_BASE_URL` | Optional email notifications |
| `SOMA_*` | Read-only SOMA adapter; reports "not configured" until DIT supplies the API |

Keep every secret out of version control. Schedule `flask send-study-reminders` daily to send optional reminders.

Deployment: [DEPLOY_RENDER.md](DEPLOY_RENDER.md).

## Code map

- `curriculum_service.py`: prospectus upload, reading, checks, publish and undo.
- `curriculum_context.py`, `ai_engine.py`: DIT AI retrieval, prompts and answer rendering.
- `routes/`: student, lecturer, department (HOD), AI, API, auth and notification endpoints.
- `models.py`: data model. Schema changes are idempotent start-up migrations in `app.py`.
- `academic_context.py`, `integrations/soma.py`: student placement and the SOMA boundary.
- `navigation.py`: each role's menu, shared by the sidebar, mobile drawer and command palette.
- `tests/`: pytest suite on a disposable database.
