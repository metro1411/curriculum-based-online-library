"""Registration rules and Head of Department governance workflows."""

import pytest

from conftest import db
from models import (
    AcademicQuestion, AuditLog, LearningEvent, LecturerAssignment, LecturerRequest, Notification, User,
)


@pytest.mark.parametrize("path", [
    "/department", "/department/lecturer-requests", "/department/curriculum", "/department/prospectus",
    "/department/students",
    "/department/lecturers", "/department/module-claims", "/department/audit-log",
])
def test_hod_pages_render(hod, path):
    assert hod.get(path).status_code == 200


def test_student_registration_creates_active_account(app, new_student):
    with app.app_context():
        user = User.query.filter_by(registration_number=new_student.registration_number).one()
        assert user.is_student and user.is_active_account
    assert new_student.get("/profile").status_code == 200


def test_lecturer_registration_stays_pending(app, register_lecturer):
    number, password = register_lecturer()
    with app.app_context():
        user = User.query.filter_by(registration_number=number).one()
        assert user.account_status == "pending" and not user.is_active_account
        assert LecturerRequest.query.filter_by(user_id=user.id, status="pending").count() == 1
    blocked = app.test_client().post("/login", data={"identifier": number, "password": password})
    assert blocked.status_code == 200


def test_unknown_id_prefix_is_rejected(client, seeded):
    response = client.post("/register", data={
        "registration_number": "99990001", "full_name": "Nobody", "email": "nobody@example.test",
        "password": "Password@12345", "password_confirm": "Password@12345",
        "department_id": seeded["department_id"],
    })
    assert response.status_code == 200
    assert b"Use an 8" in response.data


def test_hod_approval_activates_lecturer(app, approved_lecturer, seeded):
    with app.app_context():
        user = db.session.get(User, approved_lecturer.user_id)
        assert user.is_active_account and user.account_status == "active"
        assert LecturerAssignment.query.filter_by(
            lecturer_id=user.id, module_id=seeded["teaching_module_id"]
        ).first()


def test_hod_can_no_longer_hand_edit_modules(hod, hod_module_id):
    assert hod.get(f"/department/curriculum/modules/{hod_module_id}/edit").status_code == 404
    assert hod.post("/department/curriculum", data={"action": "module"}).status_code == 405


def test_module_claim_requires_hod_approval_for_api_access(app, hod, approved_lecturer, hod_module_id):
    assert approved_lecturer.post("/lecturer/workspace", data={"action": "claim", "module_id": hod_module_id}).status_code == 302
    with app.app_context():
        claim = LecturerAssignment.query.filter_by(lecturer_id=approved_lecturer.user_id, module_id=hod_module_id).one()
        assert claim.status == "pending"
        claim_id = claim.id

    def api_module_ids():
        return {item["id"] for item in approved_lecturer.get("/api/v1/modules").get_json()["modules"]}

    assert hod_module_id not in api_module_ids()
    assert hod.post(f"/department/module-claims/{claim_id}/approve").status_code == 302
    assert hod_module_id in api_module_ids()


def test_anonymous_question_round_trip(app, hod, approved_lecturer, new_student, hod_module_id):
    with app.app_context():
        claim = LecturerAssignment(lecturer_id=approved_lecturer.user_id, module_id=hod_module_id, status="approved")
        db.session.add(claim)
        db.session.commit()
    submitted = new_student.post("/questions", data={
        "module_id": hod_module_id, "lecturer_id": approved_lecturer.user_id,
        "subject": "Feedback stability clarification",
        "body": "Please explain how negative feedback changes stability in this module.",
    })
    assert submitted.status_code == 302
    with app.app_context():
        question = AcademicQuestion.query.filter_by(module_id=hod_module_id, lecturer_id=approved_lecturer.user_id).one()
        question_id, reference = question.id, question.anonymous_ref
        assert LearningEvent.query.filter_by(
            student_id=question.student_id, event_type="lecturer_question", qualifies_for_streak=True
        ).first()
        assert Notification.query.filter_by(user_id=approved_lecturer.user_id, kind="academic_question").first()

    inbox = approved_lecturer.get("/lecturer/questions")
    assert reference.encode() in inbox.data
    assert new_student.registration_number.encode() not in inbox.data

    assert approved_lecturer.post(f"/lecturer/questions/{question_id}/respond", data={
        "status": "answered",
        "answer": "Negative feedback can improve stability margins when the loop is designed correctly.",
    }).status_code == 302
    assert b"improve stability margins" in new_student.get("/questions").data


def test_deactivation_blocks_access_and_is_reversible(app, hod, approved_lecturer):
    lecturer_id = approved_lecturer.user_id
    assert hod.post(f"/department/lecturers/{lecturer_id}/deactivate").status_code == 302
    assert approved_lecturer.get("/lecturer/workspace").status_code == 302
    number, password = approved_lecturer.credentials
    assert app.test_client().post("/login", data={"identifier": number, "password": password}).status_code == 200
    assert hod.post(f"/department/lecturers/{lecturer_id}/reactivate").status_code == 302
    with app.app_context():
        assert db.session.get(User, lecturer_id).is_active_account
        assert AuditLog.query.filter_by(target_id=lecturer_id).count() >= 2
