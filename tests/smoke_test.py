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
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

# These values must be set before importing the application configuration.
# Never inherit DATABASE_URL: a smoke test must not touch a developer's local
# data or a deployed Supabase/Postgres database.
database_file = Path(tempfile.gettempdir()) / f"smart_dit_smoke_{os.getpid()}.db"
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
os.environ["HOD_ACTIVATION_CODE"] = "isolated-test-hod-code"
os.environ["SMTP_HOST"] = ""
os.environ["SMTP_USERNAME"] = ""
os.environ["SMTP_PASSWORD"] = ""
os.environ["MAIL_FROM"] = ""

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
from models import (  # noqa: E402
    AcademicQuestion, AcademicYear, AIAnswerFeedback, AuditLog,
    LearningEvent, LecturerAssignment, LecturerRequest, Module, Notification, Resource, ResourceStudySession,
    StudentPreference, Topic, User, utcnow,
)


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
    if "frame-ancestors 'none'" not in health.headers.get("Content-Security-Policy", ""):
        raise AssertionError("security headers were not applied")
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
    answer_message_id = answer_data.get("message_id")
    expect(student.post("/ai/feedback", json={
        "message_id": answer_message_id, "rating": 3,
    }), 200, "AI answer rating")
    expect(student.post("/ai/feedback", json={
        "message_id": answer_message_id, "rating": 1, "is_unclear": True,
        "note": "The final equation sign should be checked.",
    }), 200, "AI incorrect-answer report")
    with app.app_context():
        feedback_rows = AIAnswerFeedback.query.filter_by(message_id=answer_message_id).all()
        if len(feedback_rows) != 1:
            raise AssertionError("AI feedback updates created duplicate records")
        if not feedback_rows[0].is_unclear or "equation sign" not in (feedback_rows[0].note or ""):
            raise AssertionError("AI incorrect-answer report did not persist")
    with app.app_context():
        api_event_count = LearningEvent.query.filter_by(
            event_type="academic_ai_question", qualifies_for_streak=True
        ).count()
    with patch.object(ai_engine, "is_available", return_value=True), patch.object(ai_engine, "ask", return_value=generated):
        api_answer = student.post("/api/v1/ai/ask", json={
            "question": "Explain how feedback control improves system stability.",
            "module_id": module_id,
            "mode": "explain",
        })
    expect(api_answer, 200, "academic AI API workflow")
    with app.app_context():
        events = LearningEvent.query.filter_by(
            event_type="academic_ai_question", qualifies_for_streak=True
        ).all()
        if len(events) != api_event_count + 1:
            raise AssertionError("genuine academic API question did not qualify the streak")
        if any("feedback control improves" in (event.detail or "").lower() for event in events):
            raise AssertionError("a private AI question leaked into learning analytics")

    student.get("/logout")
    lecturer = app.test_client()
    login(lecturer, "lecturer")
    lecturer_workspace = lecturer.get("/lecturer/workspace")
    expect(lecturer_workspace, 200, "lecturer workspace")
    if b"Select &amp; manage topics" not in lecturer_workspace.data:
        raise AssertionError("lecturer workspace did not expose the selected-module topic action")
    expect(lecturer.get("/lecturer/resources"), 200, "resource management")

    with app.app_context():
        assignment = LecturerAssignment.query.first()
        if assignment is None:
            raise AssertionError("seed data contains no lecturer-module assignment")
        upload_module_id = assignment.module_id
        lecturer_user = User.query.filter_by(email="lecturer@dit.ac.tz").one()
        assigned_ids = {
            item.module_id for item in LecturerAssignment.query.filter_by(
                lecturer_id=lecturer_user.id, status="approved"
            ).all()
        }
        unassigned_module = Module.query.filter(
            Module.id.notin_(assigned_ids or [-1])
        ).first()
        unassigned_module_id = unassigned_module.id if unassigned_module else None

    if unassigned_module_id:
        expect(
            lecturer.post(f"/lecturer/module/{unassigned_module_id}/content", data={
                "action": "topic", "title": "Must not be created",
            }),
            403,
            "unapproved lecturer topic guard",
        )

    topic_destination = lecturer.post(
        "/lecturer/workspace",
        data={"module_id": upload_module_id, "destination": "topics"},
        follow_redirects=False,
    )
    expect(topic_destination, 302, "lecturer module-to-topic selection")
    if not topic_destination.headers["Location"].endswith(
        f"/lecturer/module/{upload_module_id}/content"
    ):
        raise AssertionError("selected lecturer module did not open its topic manager")
    dashboard_destination = lecturer.post(
        "/lecturer/workspace",
        data={"module_id": upload_module_id, "destination": "dashboard"},
        follow_redirects=False,
    )
    expect(dashboard_destination, 302, "lecturer module-to-dashboard selection")
    if not dashboard_destination.headers["Location"].endswith("/lecturer/dashboard"):
        raise AssertionError("lecturer analytics dashboard option stopped working")
    expect(lecturer.get("/lecturer/dashboard"), 200, "lecturer dashboard")
    topic_create = lecturer.post(
        f"/lecturer/module/{upload_module_id}/content",
        data={
            "action": "topic",
            "unit_label": "Lab 1",
            "category": "practical",
            "status": "planned",
            "title": "Category Workflow Verification",
            "learning_outcome": "Apply the concept during a guided laboratory activity.",
            "description": "A practical introduction to the category workflow.",
            "is_published": "on",
        },
        follow_redirects=False,
    )
    expect(topic_create, 302, "lecturer topic category creation")
    with app.app_context():
        categorised_topic = Topic.query.filter_by(
            module_id=upload_module_id, title="Category Workflow Verification"
        ).one()
        categorised_topic_id = categorised_topic.id
        if categorised_topic.category != "practical" or categorised_topic.category_label != "Practical / Lab":
            raise AssertionError("topic category did not persist")
        if not categorised_topic.description or not categorised_topic.learning_outcome:
            raise AssertionError("topic description and learning objective did not persist")
        if unassigned_module_id and Topic.query.filter_by(
            module_id=unassigned_module_id, title="Category Workflow Verification"
        ).count():
            raise AssertionError("topic leaked into a module the lecturer did not select")
        topic_count = Topic.query.filter_by(module_id=upload_module_id).count()
    if topic_count > 1:
        expect(lecturer.post(
            f"/lecturer/module/{upload_module_id}/topics/{categorised_topic_id}/move",
            data={"direction": "up"},
            follow_redirects=False,
        ), 302, "lecturer topic reorder")
        with app.app_context():
            reordered = Topic.query.filter_by(module_id=upload_module_id).order_by(
                Topic.display_order, Topic.id
            ).all()
            if reordered[-1].id == categorised_topic_id:
                raise AssertionError("topic reorder did not change the learning-path position")
    topic_workspace = lecturer.get(f"/lecturer/module/{upload_module_id}/content")
    expect(topic_workspace, 200, "lecturer topic category workspace")
    if b"Practical / Lab" not in topic_workspace.data:
        raise AssertionError("topic category was not shown in the lecturer workspace")
    expect(lecturer.post(
        f"/lecturer/module/{upload_module_id}/topics/{categorised_topic_id}/update",
        data={
            "unit_label": "Tutorial 1",
            "category": "tutorial",
            "status": "currently_teaching",
            "title": "Category Workflow Verification",
            "learning_outcome": "Apply the concept in a guided tutorial.",
            "description": "A revised guided tutorial description.",
            "is_published": "on",
        },
        follow_redirects=False,
    ), 302, "lecturer topic category update")
    with app.app_context():
        updated_topic = db.session.get(Topic, categorised_topic_id)
        if updated_topic.category != "tutorial":
            raise AssertionError("updated topic category did not persist")
        if updated_topic.description != "A revised guided tutorial description.":
            raise AssertionError("updated topic description did not persist")
    expect(lecturer.get(f"/lecturer/upload/module/{upload_module_id}"), 200, "upload form")
    upload = lecturer.post(
        f"/lecturer/upload/module/{upload_module_id}",
        data={
            "title": "Smoke-test lecture note", "description": "Verified upload workflow test.",
            "resource_type": "notes", "topic_id": str(categorised_topic_id), "verified": "on",
            "file": (io.BytesIO(b"Feedback control keeps a system stable."), "smoke-note.txt"),
        },
        content_type="multipart/form-data", follow_redirects=False,
    )
    expect(upload, 302, "lecturer file upload")

    with app.app_context():
        uploaded = Resource.query.filter_by(title="Smoke-test lecture note").one_or_none()
        if uploaded is None or not uploaded.stored_filename or uploaded.text_extraction_status != "success":
            raise AssertionError("uploaded resource was not saved and indexed")
        if uploaded.topic_id != categorised_topic_id:
            raise AssertionError("uploaded resource was not placed under its selected topic")
        uploaded_id = uploaded.id

        # Pending material belongs to the lecturer workspace only. Verify that
        # direct URLs and the JSON integration boundary cannot reveal it.
        uploaded.verification_status = "pending"
        db.session.commit()
    student_again = app.test_client()
    login(student_again, "student")
    categorised_module = student_again.get(f"/module/{upload_module_id}")
    expect(categorised_module, 200, "student categorised topic view")
    if b"Tutorial" not in categorised_module.data or b"Category Workflow Verification" not in categorised_module.data:
        raise AssertionError("students could not see the topic category and title")
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

        department = User.query.filter_by(email="student@dit.ac.tz").one().department
        programme = User.query.filter_by(email="student@dit.ac.tz").one().programme
        level = User.query.filter_by(email="student@dit.ac.tz").one().nta_level
        semester = User.query.filter_by(email="student@dit.ac.tz").one().semester

    expect(lecturer.post(
        f"/lecturer/module/{upload_module_id}/topics/{categorised_topic_id}/delete",
        follow_redirects=False,
    ), 302, "safe topic archive")
    archived_student_view = student_again.get(f"/module/{upload_module_id}")
    if b"Category Workflow Verification" in archived_student_view.data:
        raise AssertionError("archived topic remained visible to students")
    archived_insights = student_again.get(f"/learning-insights?module_id={upload_module_id}")
    if b"Category Workflow Verification" in archived_insights.data:
        raise AssertionError("archived topic remained in student progress metrics")
    expect(lecturer.post(
        f"/lecturer/module/{upload_module_id}/topics/{categorised_topic_id}/restore",
        follow_redirects=False,
    ), 302, "topic restore and publish")
    if b"Category Workflow Verification" not in student_again.get(f"/module/{upload_module_id}").data:
        raise AssertionError("restored topic was not republished to students")

    # Registration turns the agreed 2403/1403/5000 ID rules into real account
    # states. These requests use a disposable database only.
    registration = app.test_client()
    registration_page = registration.get("/register")
    expect(registration_page, 200, "registration form")
    if b"<script>" in registration_page.data:
        raise AssertionError("registration form contains CSP-blocked inline JavaScript")
    if registration_page.data.count(b'name="department_id"') != 1:
        raise AssertionError("registration must present one common department selector")
    new_student = registration.post("/register", data={
        "registration_number": "24030002", "full_name": "Student Two",
        "email": "student.two@example.test", "password": "StudentTwo@123",
        "password_confirm": "StudentTwo@123", "department_id": department.id,
        "programme_id": programme.id, "nta_level_id": level.id, "semester_id": semester.id,
    }, follow_redirects=False)
    expect(new_student, 302, "student registration")
    with app.app_context():
        second_student = User.query.filter_by(registration_number="24030002").one()
        if not second_student.is_student or not second_student.is_active_account:
            raise AssertionError("student registration did not create an active student account")

    new_student_client = app.test_client()
    sign_in = new_student_client.post("/login", data={
        "identifier": "24030002", "password": "StudentTwo@123"
    }, follow_redirects=False)
    expect(sign_in, 302, "ID-based student sign in")
    expect(new_student_client.get("/profile"), 200, "student profile page")
    profile_update = new_student_client.post("/profile", data={
        "action": "profile", "full_name": "Student Two", "learning_goal": "Master control systems",
        "weekly_goal_minutes": 180, "data_saver": "on",
    }, follow_redirects=False)
    expect(profile_update, 302, "student profile update")
    with app.app_context():
        pref = StudentPreference.query.filter_by(student_id=second_student.id).one()
        if not pref.data_saver or pref.learning_goal != "Master control systems":
            raise AssertionError("profile learning preferences did not persist")

    lecturer_registration = registration.post("/register", data={
        "registration_number": "14030002", "full_name": "Lecturer Two",
        "email": "lecturer.two@example.test", "password": "LecturerTwo@123",
        "password_confirm": "LecturerTwo@123", "department_id": department.id,
        "teaching_interest": "Control Engineering",
    }, follow_redirects=False)
    expect(lecturer_registration, 302, "lecturer registration")
    with app.app_context():
        pending_lecturer = User.query.filter_by(registration_number="14030002").one()
        if pending_lecturer.account_status != "pending" or pending_lecturer.is_active_account:
            raise AssertionError("lecturer registration should remain pending")
        if LecturerRequest.query.filter_by(user_id=pending_lecturer.id, status="pending").count() != 1:
            raise AssertionError("lecturer request was not recorded")

    hod_registration = registration.post("/register", data={
        "registration_number": "50000001", "full_name": "Electrical Head",
        "email": "hod@example.test", "password": "DepartmentHead@123",
        "password_confirm": "DepartmentHead@123", "department_id": department.id,
        "hod_activation_code": "isolated-test-hod-code",
    }, follow_redirects=False)
    expect(hod_registration, 302, "HOD activation")
    hod = app.test_client()
    expect(hod.post("/login", data={"identifier": "50000001", "password": "DepartmentHead@123"}, follow_redirects=False), 302, "HOD sign in")
    expect(hod.get("/department"), 200, "HOD dashboard")
    expect(hod.get("/department/lecturer-requests"), 200, "HOD lecturer request list")
    expect(hod.get("/department/curriculum"), 200, "HOD curriculum workspace")
    expect(hod.get("/department/lecturers"), 200, "HOD lecturer directory")
    expect(hod.get("/department/module-claims"), 200, "HOD module claim list")
    expect(hod.get("/department/audit-log"), 200, "HOD audit log")
    approval = hod.post(f"/department/lecturer-requests/{pending_lecturer.id}/approve", data={
        "module_ids": str(upload_module_id),
    }, follow_redirects=False)
    expect(approval, 302, "HOD lecturer approval")
    with app.app_context():
        approved = db.session.get(User, pending_lecturer.id)
        if not approved.is_active_account or approved.account_status != "active":
            raise AssertionError("HOD approval did not activate lecturer")
        if not LecturerAssignment.query.filter_by(lecturer_id=approved.id, module_id=upload_module_id).first():
            raise AssertionError("HOD approval did not assign selected module")

        current_year = AcademicYear.query.filter_by(
            department_id=department.id, is_current=True
        ).one()
        academic_year_id = current_year.id

    # The HOD publishes a fully identified module without changing historical
    # semester records.
    module_create = hod.post("/department/curriculum", data={
        "action": "module",
        "academic_year_id": academic_year_id,
        "programme_id": programme.id,
        "nta_level_id": level.id,
        "semester_id": semester.id,
        "code": "EE-TEST-01",
        "name": "Academic Workflow Verification",
        "module_type": "general_studies",
        "cohort_label": "EE5-QA",
        "description": "Isolated smoke-test curriculum module.",
    }, follow_redirects=False)
    expect(module_create, 302, "HOD module creation")
    with app.app_context():
        claimed_module = Module.query.filter_by(code="EE-TEST-01").one()
        claimed_module_id = claimed_module.id
        if claimed_module.module_type != "general_studies" or claimed_module.academic_year_id != academic_year_id:
            raise AssertionError("module code, type or academic-year version did not persist")
    expect(
        hod.get(f"/department/curriculum/modules/{claimed_module_id}/edit"),
        200,
        "HOD module edit form",
    )
    expect(hod.post(
        f"/department/curriculum/modules/{claimed_module_id}/edit",
        data={
            "code": "EE-TEST-01",
            "name": "Academic Workflow Verification Updated",
            "module_type": "general_studies",
            "publication_status": "published",
            "cohort_label": "EE5-QA",
            "description": "Updated without replacing its academic-year record.",
        },
        follow_redirects=False,
    ), 302, "HOD module update")
    with app.app_context():
        updated_module = db.session.get(Module, claimed_module_id)
        if updated_module.name != "Academic Workflow Verification Updated" or not updated_module.is_published:
            raise AssertionError("HOD module update did not preserve a published curriculum record")

    approved_lecturer = app.test_client()
    expect(approved_lecturer.post("/login", data={
        "identifier": "14030002", "password": "LecturerTwo@123"
    }, follow_redirects=False), 302, "approved lecturer sign in")
    claim_submit = approved_lecturer.post("/lecturer/workspace", data={
        "action": "claim", "module_id": claimed_module_id,
    }, follow_redirects=False)
    expect(claim_submit, 302, "lecturer module claim")
    with app.app_context():
        claim = LecturerAssignment.query.filter_by(
            lecturer_id=pending_lecturer.id, module_id=claimed_module_id
        ).one()
        if claim.status != "pending":
            raise AssertionError("new lecturer claim was not left pending for HOD approval")
        claim_id = claim.id
    pending_api_modules = approved_lecturer.get("/api/v1/modules")
    expect(pending_api_modules, 200, "pending lecturer API scope")
    if claimed_module_id in {
        item["id"] for item in pending_api_modules.get_json()["modules"]
    }:
        raise AssertionError("pending module claim granted lecturer API access")
    expect(hod.post(
        f"/department/module-claims/{claim_id}/approve", follow_redirects=False
    ), 302, "HOD module claim approval")
    with app.app_context():
        if db.session.get(LecturerAssignment, claim_id).status != "approved":
            raise AssertionError("HOD approval did not activate the module claim")
    approved_api_modules = approved_lecturer.get("/api/v1/modules")
    expect(approved_api_modules, 200, "approved lecturer API scope")
    if claimed_module_id not in {
        item["id"] for item in approved_api_modules.get_json()["modules"]
    }:
        raise AssertionError("approved module claim was missing from lecturer API")

    # Student-to-lecturer questions disclose the question but never the
    # student's identity. Replies return only to the owning student.
    private_question_submit = student_again.post("/questions", data={
        "module_id": claimed_module_id,
        "lecturer_id": pending_lecturer.id,
        "subject": "Feedback stability clarification",
        "body": "Please explain how negative feedback changes stability in this module.",
    }, follow_redirects=False)
    expect(private_question_submit, 302, "anonymous lecturer question")
    with app.app_context():
        lecturer_question = AcademicQuestion.query.filter_by(
            module_id=claimed_module_id, lecturer_id=pending_lecturer.id
        ).one()
        lecturer_question_id = lecturer_question.id
        anonymous_ref = lecturer_question.anonymous_ref
        if not LearningEvent.query.filter_by(
            student_id=lecturer_question.student_id,
            event_type="lecturer_question",
            qualifies_for_streak=True,
        ).first():
            raise AssertionError("genuine lecturer question did not qualify the streak")
        if not Notification.query.filter_by(
            user_id=pending_lecturer.id, kind="academic_question"
        ).first():
            raise AssertionError("lecturer question notification was not created")
    lecturer_inbox = approved_lecturer.get("/lecturer/questions")
    expect(lecturer_inbox, 200, "lecturer anonymous question inbox")
    if anonymous_ref.encode() not in lecturer_inbox.data:
        raise AssertionError("anonymous question reference was not shown to lecturer")
    if b"student@dit.ac.tz" in lecturer_inbox.data or b"24030001" in lecturer_inbox.data:
        raise AssertionError("student identity leaked into lecturer question inbox")
    expect(approved_lecturer.post(
        f"/lecturer/questions/{lecturer_question_id}/respond",
        data={
            "status": "answered",
            "answer": "Negative feedback can improve stability margins when the loop is designed correctly.",
        },
        follow_redirects=False,
    ), 302, "lecturer private answer")
    student_questions = student_again.get("/questions")
    expect(student_questions, 200, "student private question history")
    if b"improve stability margins" not in student_questions.data:
        raise AssertionError("student could not read the lecturer response")

    # Merely opening a resource no longer grants ten minutes. A server-capped
    # active session must reach the threshold first.
    study_start = student_again.post(f"/resource/{resource_id}/study/start")
    expect(study_start, 200, "resource study session start")
    study_token = study_start.get_json()["token"]
    with app.app_context():
        study_session = ResourceStudySession.query.filter_by(token=study_token).one()
        study_session.active_seconds = 590
        study_session.last_heartbeat_at = utcnow() - timedelta(seconds=35)
        db.session.commit()
    heartbeat = student_again.post(f"/resource/study/{study_token}/heartbeat")
    expect(heartbeat, 200, "resource study heartbeat")
    if not heartbeat.get_json().get("qualified"):
        raise AssertionError("ten active study minutes did not qualify the streak")

    # Deactivation preserves records, blocks both existing and new sessions,
    # and can be reversed without rebuilding assignments.
    expect(hod.post(
        f"/department/lecturers/{pending_lecturer.id}/deactivate",
        follow_redirects=False,
    ), 302, "lecturer deactivation")
    expect(approved_lecturer.get("/lecturer/workspace"), 302, "deactivated existing session guard")
    blocked_login = app.test_client().post("/login", data={
        "identifier": "14030002", "password": "LecturerTwo@123"
    }, follow_redirects=False)
    expect(blocked_login, 200, "deactivated lecturer login block")
    expect(hod.post(
        f"/department/lecturers/{pending_lecturer.id}/reactivate",
        follow_redirects=False,
    ), 302, "lecturer reactivation")
    with app.app_context():
        if db.session.get(AcademicQuestion, lecturer_question_id) is None:
            raise AssertionError("deactivation deleted lecturer academic history")
        if AuditLog.query.count() < 5:
            raise AssertionError("sensitive HOD and teaching actions were not audited")

    flashcard_html = ai_engine.render_ai_answer(
        "**Front:** What is negative feedback?\n**Back:** Returning part of the output to oppose the input.\n\n"
        "**Front:** Write Ohm’s law.\n**Back:** V = I × R",
        "flashcards",
    )
    if "study-flashcard-deck" not in flashcard_html or "data-flashcard-toggle" not in flashcard_html:
        raise AssertionError("AI flashcards were not rendered as interactive teal cards")
    mathematics_html = ai_engine.render_ai_answer(
        r"""## Worked mathematics

\[
\sum_{i=1}^{n} x_i \leq \sqrt{n}
\]

\[
\begin{bmatrix}1 & 2 \\ 3 & 4\end{bmatrix}
\]

The ratio is \frac{1}{2}.""",
        "solve",
    )
    if "\\sum" in mathematics_html or "\\frac" in mathematics_html or "\\begin" in mathematics_html:
        raise AssertionError("raw LaTeX leaked into an AI answer")
    if "∑" not in mathematics_html or "≤" not in mathematics_html or "math-matrix" not in mathematics_html:
        raise AssertionError("AI mathematics did not render readable signs and matrices")

    # Conversations remain scoped to the student who owns them.
    generated_private = {**generated, "answer_text": "Private learner answer", "answer_html": "<p>Private learner answer</p>"}
    with patch.object(ai_engine, "is_available", return_value=True), patch.object(ai_engine, "ask", return_value=generated_private):
        private_answer = new_student_client.post("/ai/ask", json={
            "message": "My private question", "module_id": upload_module_id, "mode": "explain",
        })
    expect(private_answer, 200, "private AI conversation")
    private_conversation_id = private_answer.get_json()["conversation_id"]
    first_student = app.test_client()
    login(first_student, "student")
    other_history = first_student.get(f"/ai?conversation_id={private_conversation_id}")
    expect(other_history, 200, "other student AI workspace")
    if b"Private learner answer" in other_history.data:
        raise AssertionError("one student could read another student's AI conversation")

    TEST_UPLOADS.cleanup()
    print("PASS: registration, AI presentation/feedback, topic lifecycle, privacy, streak, curriculum, security and notification checks")


if __name__ == "__main__":
    main()
