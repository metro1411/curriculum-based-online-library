"""End-to-end smoke coverage for the public Smart DIT workflows.

Run with a disposable SQLite database so the seeded demonstration data and
uploaded resources in a packaged copy are never modified:

    $env:DATABASE_URL = "sqlite:///C:/path/to/smart_dit_qa.db"
    python tests/smoke_test.py
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

# These values must be set before importing the application configuration.
# Never inherit DATABASE_URL: a smoke test must not touch a developer's local
# data or a deployed Supabase/Postgres database.
database_file = Path(tempfile.gettempdir()) / "smart_dit_smoke.db"
database_file.unlink(missing_ok=True)
os.environ["DATABASE_URL"] = f"sqlite:///{database_file.as_posix()}"
os.environ.setdefault("FLASK_DEBUG", "0")
os.environ.setdefault("SECRET_KEY", "smoke-test-key-not-for-production")
# Always test the application-owned fallback and local storage path. This
# keeps the test deterministic and never consumes a developer's real Gemini
# quota or writes to a configured Supabase project.
os.environ["APP_ENV"] = "development"
os.environ["GEMINI_API_KEY"] = ""
os.environ["SUPABASE_URL"] = ""
os.environ["SUPABASE_SERVICE_ROLE_KEY"] = ""

import config  # noqa: E402
import ai_engine  # noqa: E402

# The seed routine creates starter resources on first launch. Point its
# import-time path at an isolated folder before the app factory is imported so
# a QA run cannot add random files to a packaged project's uploads directory.
TEST_UPLOADS = tempfile.TemporaryDirectory(prefix="smart-dit-seed-")
config.RESOURCE_UPLOAD_DIR = TEST_UPLOADS.name

import storage_backend  # noqa: E402
from app import app  # noqa: E402
from extensions import db  # noqa: E402
from models import LecturerAssignment, Resource  # noqa: E402


def expect(response, status, label):
    if response.status_code != status:
        raise AssertionError(f"{label}: expected {status}, got {response.status_code}")


def login(client, role):
    credentials = {
        "student": ("/student/login", "student@dit.ac.tz", "Student@123"),
        "lecturer": ("/lecturer/login", "lecturer@dit.ac.tz", "Lecturer@123"),
    }
    path, identifier, password = credentials[role]
    response = client.post(path, data={"identifier": identifier, "password": password}, follow_redirects=False)
    expect(response, 302, f"{role} login")


def main():
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    credential_message = ai_engine._provider_error_message(Exception("401 UNAUTHENTICATED"))
    if "credentials" not in credential_message.lower():
        raise AssertionError("Gemini authentication failures must return an actionable safe message")
    anonymous = app.test_client()
    expect(anonymous.get("/"), 200, "landing page")
    health = anonymous.get("/health")
    expect(health, 200, "health check")
    if health.get_json() != {"status": "ok"}:
        raise AssertionError("health check did not return an ok payload")
    expect(anonymous.get("/dashboard"), 302, "student login guard")
    expect(anonymous.get("/lecturer/dashboard"), 302, "lecturer login guard")

    student = app.test_client()
    login(student, "student")
    expect(student.get("/dashboard"), 200, "student dashboard")
    expect(student.get("/archive"), 200, "curriculum navigation")
    expect(student.get("/search?q=control"), 200, "global search")
    expect(student.get("/learning-insights"), 200, "learning insights")
    expect(student.get("/ai"), 200, "AI workspace")
    curriculum = student.get("/api/v1/curriculum")
    expect(curriculum, 200, "curriculum API")
    if not curriculum.get_json().get("departments"):
        raise AssertionError("curriculum API returned no seeded departments")

    with app.app_context():
        resource = Resource.query.first()
        if resource is None:
            raise AssertionError("seed data contains no resource")
        resource_id = resource.id
        module_id = resource.module_id

    expect(student.get(f"/module/{module_id}"), 200, "module overview")
    expect(student.get(f"/resource/{resource_id}"), 200, "resource preview")
    expect(student.post(f"/resource/{resource_id}/save"), 302, "bookmark toggle")

    # The AI endpoint remains safe and explicit when no provider key exists.
    unavailable = student.post("/ai/ask", json={"message": "Explain feedback control.", "module_id": module_id})
    expect(unavailable, 503, "AI provider unavailable response")

    # Exercise the complete chat formatting/persistence path without calling an
    # external paid provider. The provider client is covered by configuration
    # checks at startup and is intentionally not exercised without a real key.
    generated = {
        "ok": True,
        "answer_text": "## Worked solution\n\nFor $V = IR$, substitute the known values.",
        "answer_html": "<h2>Worked solution</h2><p>For <span class=\"math-inline\">V = IR</span>, substitute the known values.</p>",
        "sources": [],
        "web_sources": [],
        "general_guidance": False,
        "web_grounded": False,
        "followups": ["Show a numerical example"],
    }
    with patch.object(ai_engine, "is_available", return_value=True), patch.object(ai_engine, "ask", return_value=generated):
        answer = student.post("/ai/ask", json={
            "message": "Solve V = IR step by step.", "module_id": module_id,
            "mode": "solve", "response_style": "detailed",
        })
    expect(answer, 200, "AI answer workflow")
    answer_data = answer.get_json()
    if not answer_data.get("ok") or "math-inline" not in answer_data.get("answer_html", ""):
        raise AssertionError("AI answer payload was not formatted as expected")

    student.get("/logout")
    lecturer = app.test_client()
    login(lecturer, "lecturer")
    expect(lecturer.get("/lecturer/workspace"), 200, "lecturer workspace")
    expect(lecturer.get("/lecturer/resources"), 200, "resource management")

    with app.app_context():
        assignment = LecturerAssignment.query.first()
        if assignment is None:
            raise AssertionError("seed data contains no lecturer-module assignment")
        upload_module_id = assignment.module_id

    expect(lecturer.post("/lecturer/workspace", data={"module_id": upload_module_id}), 302, "lecturer workspace selection")
    expect(lecturer.get("/lecturer/dashboard"), 200, "lecturer dashboard")
    expect(lecturer.get(f"/lecturer/upload/module/{upload_module_id}"), 200, "upload form")
    upload = lecturer.post(
        f"/lecturer/upload/module/{upload_module_id}",
        data={
            "title": "Smoke-test lecture note", "description": "Verified upload workflow test.",
            "resource_type": "notes", "verified": "on",
            "file": (io.BytesIO(b"Feedback control keeps a system stable."), "smoke-note.txt"),
        },
        content_type="multipart/form-data", follow_redirects=False,
    )
    expect(upload, 302, "lecturer file upload")

    with app.app_context():
        uploaded = Resource.query.filter_by(title="Smoke-test lecture note").one_or_none()
        if uploaded is None or not uploaded.stored_filename or uploaded.text_extraction_status != "success":
            raise AssertionError("uploaded resource was not saved and indexed")
        uploaded_id = uploaded.id

        # Pending material belongs to the lecturer workspace only. Verify that
        # direct URLs and the JSON integration boundary cannot reveal it.
        uploaded.verification_status = "pending"
        db.session.commit()
    student_again = app.test_client()
    login(student_again, "student")
    expect(student_again.get(f"/resource/{uploaded_id}"), 404, "pending resource privacy")
    expect(student_again.get(f"/api/v1/resources/{uploaded_id}"), 404, "pending resource API privacy")
    with app.app_context():
        uploaded = db.session.get(Resource, uploaded_id)
        uploaded.verification_status = "verified"
        db.session.commit()
    served = student_again.get(f"/resource/{uploaded_id}/file")
    expect(served, 200, "verified resource file")
    if served.headers.get("X-Content-Type-Options") != "nosniff":
        raise AssertionError("resource file response is missing nosniff protection")

    expect(lecturer.get(f"/lecturer/resources/{uploaded_id}/edit"), 200, "resource edit form")
    expect(lecturer.post(
        f"/lecturer/resources/{uploaded_id}/edit",
        data={"title": "Smoke-test lecture note", "external_url": "not-a-url"},
        follow_redirects=False,
    ), 302, "external-link validation")
    update = lecturer.post(
        f"/lecturer/resources/{uploaded_id}/edit",
        data={"title": "Smoke-test lecture note (edited)", "description": "Updated safely.", "resource_type": "notes", "verified": "on"},
        follow_redirects=False,
    )
    expect(update, 302, "resource update")
    expect(lecturer.post(f"/lecturer/resources/{uploaded_id}/delete", follow_redirects=False), 302, "resource delete")
    with app.app_context():
        if db.session.get(Resource, uploaded_id) is not None:
            raise AssertionError("resource delete did not complete")

    TEST_UPLOADS.cleanup()
    print("PASS: 29 core student, lecturer, AI, API, upload and health checks")


if __name__ == "__main__":
    main()
