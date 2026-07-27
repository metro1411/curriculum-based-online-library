# Smart DIT Learning Hub

Smart DIT Learning Hub is a role-based learning workspace for organising module resources, supporting independent study, and giving lecturers a clear view of learning engagement.

## Start here

After extracting the release ZIP, double-click `START_HERE.bat`. It provides the correct order: configure `.env`, run the automated test, start locally, then open the Render guide. See `START_HERE.md` for the short explanation.

## What it includes

- Student curriculum navigation, verified learning resources, quick search, saved items, download history, module learning paths, and visual personal learning insights.
- A context-aware DIT AI assistant with conversation history, structured explanations, source references, mathematical notation, worked solutions, revision sheets, flashcards, quizzes, code blocks, follow-up prompts, and feedback controls.
- Lecturer workspaces for publishing resources, managing topics and announcements, and reviewing class engagement, popular resources, topic coverage, study time, and AI support needs.
- Private multi-user registration with ID-based Student (`2403...`), Lecturer (`1403...`) and protected Head of Department (`5000...`) roles; HOD lecturer approvals, module assignments, personal goals, streaks, achievements and Data Saver mode.
- Local SQLite development storage plus managed-Postgres and private-cloud-storage support for public deployment; secure password hashing, CSRF protection, file validation, and scoped access control.

## Run locally

1. Create and activate a virtual environment.
2. Install packages with `pip install -r requirements.txt`.
3. Optionally set `GEMINI_API_KEY` to enable generated AI answers. The assistant uses lecturer resources first; when a module has no confident match, it can use Google Search for supplementary academic context and labels those links in the answer. Google Search grounding requires a Gemini project/key entitled for that paid capability; ordinary Gemini chat continues to work without it.
4. Start the application with `python app.py`.
5. Open `http://127.0.0.1:5055`.

For the complete public deployment and upgrade checklist, see [DEPLOY_RENDER.md](DEPLOY_RENDER.md).

The default address is `127.0.0.1:5055`. If it is unavailable, choose another local port without changing source code, for example: `PORT=5060 python app.py` (PowerShell: `$env:PORT=5060; python app.py`).

On Windows, double-click `run_local.bat` for the same setup-and-start process. To run the isolated functional verification before starting the app, double-click `test_app.bat`; it checks login, learning navigation, analytics, resource upload/edit/delete, privacy rules, API responses, AI answer formatting, and the health endpoint without changing your seeded data.

On a local first launch, the application creates the curriculum, learning resources, and access accounts:

| Role | Email | Password |
| --- | --- | --- |
| Student | `student@dit.ac.tz` | `Student@123` |
| Lecturer | `lecturer@dit.ac.tz` | `Lecturer@123` |

### Gemini AI setup

Copy `.env.example` to `.env` in the same folder as `app.py`, then insert a Gemini API key created in [Google AI Studio](https://aistudio.google.com/app/apikey). A Google OAuth client secret, access token, or credential for another provider will not work. Fully stop the Python process and start the application again after changing `.env`. Start with `GEMINI_ENABLE_WEB_GROUNDING=0`; enable it only after regular chat is working and the project is entitled for Google Search grounding.

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

Optionally create a local `.env` file and set values appropriate to your environment. Keep `SECRET_KEY`, `HOD_ACTIVATION_CODE`, `GEMINI_API_KEY`, `DATABASE_URL`, and `SUPABASE_SERVICE_ROLE_KEY` out of version control. `HOST` and `PORT` can be set for local hosting. `GEMINI_MODEL` defaults to `gemini-3.5-flash`; `GEMINI_MAX_OUTPUT_TOKENS` defaults to `4096`. The service continues to provide source-grounded guidance when an AI provider is not configured.

## Project structure

- `routes/` — student, lecturer, authentication, API, and AI endpoints.
- `templates/` and `static/` — responsive interface, accessible controls, and interaction behaviour.
- `models.py` — curriculum, resource, conversation, and learning analytics models.
- `learning.py` — purposeful learning-event aggregation.
- `ai_engine.py` — retrieval, answer formatting, and mathematical rendering.
- `seed.py` — idempotent curriculum and starter learning data.
