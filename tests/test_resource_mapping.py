"""Resources mapped to curriculum modules, and evidence-based study recommendations."""

import json
import uuid

import recommendations
from conftest import STUDENT, db
from models import AuditLog, LearningEvent, Module, Resource, User


def _title(base):
    return f"{base} {uuid.uuid4().hex[:8]}"


def _upload_link(client, module_id, title, verified=True, **extra):
    data = {"title": title, "external_url": "https://example.test/notes", "resource_type": "other",
            "description": "DEMO reference", **extra}
    if verified:
        data["verified"] = "1"
    return client.post(f"/lecturer/upload/module/{module_id}", data=data)


def test_upload_records_objectives_remarks_and_curriculum_audit(app, lecturer, student, seeded):
    module_id = seeded["teaching_module_id"]
    title = _title("DEMO resource")
    response = _upload_link(lecturer, module_id, title,
                            learning_objectives="Explain the DEMO objective", lecturer_remarks="Read before lab")
    assert response.status_code == 302
    with app.app_context():
        resource = Resource.query.filter_by(title=title).one()
        assert resource.module_id == module_id
        assert resource.learning_objectives == "Explain the DEMO objective"
        assert resource.lecturer_remarks == "Read before lab"
        audit = AuditLog.query.filter_by(action="resource.created", target_id=str(resource.id), target_label=title).one()
        details = json.loads(audit.details_json)
        assert details["module_id"] == module_id and "programme" in details and "semester" in details
        assert AuditLog.query.filter_by(action="resource.approved", target_id=str(resource.id), target_label=title).count() == 1
        resource_id = resource.id
        module_code = db.session.get(Module, module_id).code
        # The seeded student studies the lecturer's module, so the mapping must reach them.
        student_user = User.query.filter_by(email=STUDENT[0]).one()
        assert db.session.get(Module, module_id).semester_id == student_user.semester_id
    page = student.get(f"/resource/{resource_id}")
    assert page.status_code == 200
    assert b"Explain the DEMO objective" in page.data and b"Read before lab" in page.data
    data = student.get(f"/api/v1/resources/{resource_id}").get_json()["resource"]
    assert data["learning_objectives"] == "Explain the DEMO objective"
    assert data["curriculum"]["module_code"] == module_code


def test_api_resource_carries_curriculum_mapping(app, student, seeded):
    data = student.get(f"/api/v1/resources/{seeded['resource_id']}").get_json()["resource"]
    with app.app_context():
        module = db.session.get(Module, seeded["module_id"])
        level = module.semester.nta_level
        assert data["curriculum"]["programme"] == level.programme.name
        assert data["curriculum"]["nta_level"] == level.level_number
        assert data["curriculum"]["semester"] == module.semester.semester_number
        assert data["curriculum"]["module"] == module.name


def test_unverified_resources_are_hidden_from_students(app, lecturer, student, seeded):
    module_id = seeded["teaching_module_id"]
    title = _title("DEMO resource")
    _upload_link(lecturer, module_id, title, verified=False)
    with app.app_context():
        resource_id = Resource.query.filter_by(title=title).one().id
    assert student.get(f"/api/v1/resources/{resource_id}").status_code == 404
    listed = student.get(f"/api/v1/resources?module_id={module_id}").get_json()["resources"]
    assert resource_id not in {r["id"] for r in listed}


def test_owner_can_edit_mapping_fields(app, approved_lecturer, seeded):
    module_id = seeded["teaching_module_id"]
    title = _title("DEMO resource")
    _upload_link(approved_lecturer, module_id, title)
    with app.app_context():
        resource_id = Resource.query.filter_by(title=title).one().id
    response = approved_lecturer.post(f"/lecturer/resources/{resource_id}/edit", data={
        "title": title, "resource_type": "other", "external_url": "https://example.test/notes",
        "learning_objectives": "Updated DEMO objective", "lecturer_remarks": "Updated remark",
    })
    assert response.status_code == 302
    with app.app_context():
        resource = db.session.get(Resource, resource_id)
        assert resource.learning_objectives == "Updated DEMO objective"
        assert resource.lecturer_remarks == "Updated remark"


def _clear_activity(student_id):
    LearningEvent.query.filter_by(student_id=student_id).delete()
    from models import ResourceView
    ResourceView.query.filter_by(student_id=student_id).delete()


def test_recommendation_flags_low_activity_with_evidence(app, seeded):
    with app.app_context():
        student = User.query.filter_by(email=STUDENT[0]).one()
        import academic_context
        modules, _ = academic_context.current_modules(student)
        with_resources = [m for m in modules if any(r.is_verified for r in m.resources)]
        assert len(with_resources) >= 2, "seed data should give the student several resourced modules"
        _clear_activity(student.id)
        busy, quiet = with_resources[0], with_resources[1]
        for _ in range(6):
            db.session.add(LearningEvent(student_id=student.id, module_id=busy.id, event_type="resource_study"))
        db.session.flush()
        items = recommendations.curriculum_recommendations(student, limit=10)
        flagged = {item["module"].id: item for item in items}
        assert quiet.id in flagged and busy.id not in flagged
        item = flagged[quiet.id]
        assert item["message"].startswith(f"You have low recent activity in {quiet.name}")
        assert item["evidence"]["module_actions"] == 0
        assert item["evidence"]["window_days"] == recommendations.WINDOW_DAYS
        # Recommendations describe activity, never marks or ability.
        text = " ".join(i["message"] for i in items).lower()
        assert "grade" not in text and "fail" not in text and "weak" not in text
        assert "does not reflect marks" in recommendations.EVIDENCE_NOTE
        db.session.rollback()


def test_recommendations_endpoint_and_dashboard(app, student, seeded):
    data = student.get("/api/v1/me/recommendations").get_json()
    assert data["ok"] and "note" in data and isinstance(data["recommendations"], list)
    page = student.get("/dashboard")
    assert page.status_code == 200
    assert b"Your modules" in page.data and b"Study recommendations" in page.data
