# Smart DIT Learning Hub

Smart DIT Learning Hub is a role-based learning workspace for organising module resources, supporting independent study, and giving lecturers a clear view of learning engagement.

## Start here

After extracting the release ZIP, double-click `START_HERE.bat`. It provides the correct order: configure `.env`, run the automated test, start locally, then open the Render guide. See `START_HERE.md` for the short explanation.

## What it includes

- Student curriculum navigation by academic year, programme, NTA level and semester; topic-based resource albums; private lecturer questions; learning goals, streaks and visual personal insights.
- A private DIT AI workspace with natural explanations, real mathematical symbols, interactive teal flashcards, labelled code blocks, revision sheets, quizzes, source references and feedback controls.
- Lecturer workspaces for claiming HOD-approved modules, publishing topics and categorised resources, and answering anonymous student questions through the `New`, `Reviewing`, `Answered`, `Will Address in Class` and `Closed` workflow.
- Module announcements: lecturers message every enrolled student from **Announcements** (or the module workspace). Students get an in-app alert with an unread badge, see updates on their dashboard and module page, and can reply privately with one tap. Posting is limited to approved lecturers, de-duplicated, rate-limited (10 per hour) and audited; email delivery is optional per announcement and respects each student's email preference.
- Built for phones on slow or patchy connections: gzip-compressed pages and assets, content-hashed CSS/JS cached for a year, an offline page instead of the browser error screen (service worker; signed-in pages are never cached on the device), double-tap and offline protection on forms, request timeouts with clear messages, 16px inputs that avoid iOS zoom, and large touch targets. The app can be added to a phone's home screen.
- Prospectus-driven curriculum: a Curriculum Administrator uploads the DIT prospectus, then extracts, imports (CSV) or enters modules, reviews, validates, approves and publishes a curriculum version. Unverified data is never published automatically, published curriculum can't be deleted, and every step is audited. Modules carry credits, prerequisites and their provenance.
- Curriculum-aware DIT AI: answers use the student's programme, level, semester and modules, rank lecturer resources first, and label every answer (for example *General information — not identified as part of your current DIT curriculum.*). Lecturers add learning objectives and remarks to resources, and students get study recommendations that show their evidence.
- A read-only SOMA integration boundary that reports "not configured" until DIT's official API contract is supplied, plus an internal way for administrators to set a student's academic context. See [docs/CURRICULUM_ARCHITECTURE.md](docs/CURRICULUM_ARCHITECTURE.md).
- HOD curriculum versioning, module code and type (`Core` or `General Studies`), lecturer verification/deactivation, claim approval, module archiving and a searchable audit log.
- Email and in-app notifications with student-controlled optional reminder frequency. Direct question replies and account notices remain available even when optional reminders are disabled.
- Local SQLite development storage plus managed-Postgres and private-cloud-storage support for public deployment; secure password hashing, CSRF protection, strict access scoping, security headers, file validation and sensitive-action audit trails.

## Run locally

1. Create and activate a virtual environment.
2. Install packages with `pip install -r requirements.txt`.
3. Optionally set `GEMINI_API_KEY` to enable generated AI answers. The assistant uses lecturer resources first; when a module has no confident match, it can use Google Search for supplementary academic context and labels those links in the answer. Google Search grounding requires a Gemini project/key entitled for that paid capability; ordinary Gemini chat continues to work without it.
4. Start the application with `python app.py`.
5. Open `http://127.0.0.1:5080`.

For the complete public deployment and upgrade checklist, see [DEPLOY_RENDER.md](DEPLOY_RENDER.md).

The default address is `127.0.0.1:5080`. If it is unavailable, choose another local port without changing source code, for example: `PORT=5090 python app.py` (PowerShell: `$env:PORT=5090; python app.py`).

On Windows, double-click `run_local.bat` for the same setup-and-start process. To run the isolated test suite before starting the app, double-click `test_app.bat` (or run `pip install -r requirements-dev.txt` then `python -m pytest`); it checks login, curriculum versioning, module claims, anonymous questions, notifications, ten-minute streak enforcement, deactivation, audit logs, resource management, API access, AI formatting, security headers and the health endpoint without changing your seeded data.

On a local first launch, the application creates the curriculum, learning resources, and access accounts:

| Role | Email | Password |
| --- | --- | --- |
| Student | `student@dit.ac.tz` | `Student@123` |
| Lecturer | `lecturer@dit.ac.tz` | `Lecturer@123` |

Create the first Curriculum Administrator with `flask --app app create-curriculum-admin --email you@example.org --name "Curriculum Office"` (you will be prompted for a password of at least 12 characters), then open **Prospectus** after signing in. `docs/samples/` holds a blank CSV import template and a file of DEMO rows that shows the format.

### Gemini AI setup

Copy `.env.example` to `.env` in the same folder as `app.py`, then insert a Gemini API key created in [Google AI Studio](https://aistudio.google.com/app/apikey). A Google OAuth client secret, access token, or credential for another provider will not work. Fully stop the Python process and start the application again after changing `.env`. Start with `GEMINI_ENABLE_WEB_GROUNDING=0`; enable it only after regular chat is working and the project is entitled for Google Search grounding.

### Academic years and email notifications

Set `CURRENT_ACADEMIC_YEAR` when seeding a new installation. HOD-created modules are stored against their academic year, so starting a new semester or year does not overwrite historical curriculum records.

To deliver email notifications, configure `PUBLIC_BASE_URL`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_USE_TLS` or `SMTP_USE_SSL`, and `MAIL_FROM`. Without SMTP values, all notifications remain available inside the application. Run `flask send-study-reminders` from a daily scheduler; each student's daily, weekly or disabled preference is respected.

## Public deployment for free

The included `render.yaml` makes this project ready to deploy as a public Flask web service. For durable data and uploaded learning materials on a free host, use **Render** for the application and **Supabase** for Postgres plus private file storage.

1. Create a GitHub repository, then commit and push this project. The `.gitignore` already excludes local data, uploads, archives, and secrets.
2. Create a free Supabase project. In **Storage**, create a **private** bucket named `resources` and set its maximum file size to 30 MB or more.
3. In Supabase, click **Connect** and copy the **Session Pooler** connection string (port 5432). Use this rather than the direct connection because it works from IPv4-only application hosts. In **Project Settings -> API**, copy the Project URL and a server-side Secret Key (or legacy `service_role` key). Never expose that key in browser code.
4. In Render, select **New -> Blueprint**, connect the GitHub repository, and accept the included `render.yaml`. Select the free plan when prompted.
5. Enter these secret values in the Blueprint form:

   | Variable | Value |
   | --- | --- |
   | `DATABASE_URL` | Supabase Session Pooler connection string |
   | `SUPABASE_URL` | Supabase Project URL |
   | `SUPABASE_SERVICE_ROLE_KEY` | Supabase server-side Secret Key / legacy `service_role` key |
   | `INITIAL_STUDENT_PASSWORD` | A new strong password of at least 12 characters |
| `INITIAL_LECTURER_PASSWORD` | A different strong password of at least 12 characters |
| `HOD_ACTIVATION_CODE` | A private random value of at least 16 characters, used to activate the first HOD account |
| `GEMINI_API_KEY` | Optional, but required for generated Gemini answers |
| `GEMINI_ENABLE_WEB_GROUNDING` | Leave as `1` to permit supplementary Google Search only when no confident lecturer-resource match exists; set to `0` to keep AI answers archive-only. Search grounding needs an eligible Gemini paid-tier/auth key. |

6. Deploy. Render provides a public `https://...onrender.com` address with HTTPS; the `/health` route is used to check that the service and database are available.
7. Open **Create account** to register real student, lecturer, and HOD accounts. The protected HOD account needs `HOD_ACTIVATION_CODE`; lecturer requests need HOD approval before they can sign in.

Free services are ideal for a competition demonstration and small pilot. Render spins an idle free web service down after 15 minutes, so the first visit after idle can take about a minute. Supabase Free includes 500 MB of database space and 1 GB of storage, but can pause a low-activity project after a week. Keep a backup of the database and resource bucket before moving to a wider institutional rollout.

## Configuration

Optionally create a local `.env` file and set values appropriate to your environment. Keep `SECRET_KEY`, `HOD_ACTIVATION_CODE`, `GEMINI_API_KEY`, `SMTP_PASSWORD`, `DATABASE_URL`, and `SUPABASE_SERVICE_ROLE_KEY` out of version control. `HOST` and `PORT` can be set for local hosting. `GEMINI_MODEL` defaults to `gemini-3.5-flash`; `GEMINI_MAX_OUTPUT_TOKENS` defaults to `4096`. The service continues to provide source-grounded guidance when an AI provider is not configured.

## Project structure

- `routes/` — student, lecturer, HOD, notification, authentication, API and AI endpoints.
- `announcements.py` — lecturer-to-student announcement rules, audience and delivery.
- `performance.py` — compression and long-lived caching of versioned static assets.
- `navigation.py` — each role's sidebar menu, defined once and shared by the sidebar, mobile drawer and Ctrl+K command palette.
- `templates/` — pages extend `base.html`, which renders the sidebar app shell for signed-in users and a public header otherwise.
- `static/css/` — `tokens.css` (light/dark design tokens), `base.css`, `layout.css` (shell, sidebar, palette), `components.css`, then page styles: `public.css`, `learning.css`, `teaching.css`, `ai.css`.
- `static/js/` — `theme-init.js` applies the saved theme before first paint; `main.js` holds all interaction behaviour.
- `tests/` — pytest suite using a disposable database (`python -m pytest`).
- `models.py` — versioned curriculum, resources, private questions, notifications, audit, conversations and analytics models.
- `learning.py` — purposeful learning-event aggregation and strict streak calculation.
- `ai_engine.py` — private retrieval, natural answer formatting, flashcards and code presentation.
- `notifications.py` — in-app and SMTP notification delivery with optional-email controls.
- `governance.py` — audit logging for sensitive administrative and teaching actions.
- `seed.py` — idempotent curriculum and starter learning data.
- `curriculum_service.py`, `routes/admin.py` — prospectus workflow and the Curriculum Administrator workspace.
- `curriculum_context.py` — curriculum context, retrieval and answer labels for DIT AI.
- `academic_context.py`, `integrations/soma.py` — student academic context and the SOMA adapter boundary.
- `permissions.py`, `recommendations.py` — central role checks and evidence-based study recommendations.
