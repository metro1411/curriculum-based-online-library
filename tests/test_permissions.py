"""Role-based access control for curriculum, resources and student data."""

import pytest

from conftest import db, user_id
from models import CurriculumVersion, LecturerAssignment, Module, Resource, User

HOD_PAGES = ["/department/prospectus", "/department/students", "/department/curriculum"]


@pytest.mark.parametrize("path", HOD_PAGES)
def test_hod_pages_render_for_hod(hod, path):
    assert hod.get(path).status_code == 200


@pytest.mark.parametrize("path", HOD_PAGES)
def test_students_and_lecturers_cannot_open_hod_pages(student, lecturer, path):
    for client in (student, lecturer):
        assert client.get(path).status_code == 403


def test_anonymous_users_are_sent_to_login(client):
    response = client.get("/department/prospectus")
    assert response.status_code == 302 and "/login" in response.headers["Location"]


def test_only_hod_can_publish_or_undo_a_prospectus(app, student, lecturer):
    with app.app_context():
        before = CurriculumVersion.query.count()
    for client in (student, lecturer):
        assert client.post("/department/prospectus", data={"title": "Unauthorised"}).status_code == 403
        assert client.post("/department/prospectus/undo").status_code == 403
    with app.app_context():
        assert CurriculumVersion.query.count() == before


def test_retired_admin_routes_are_gone(hod):
    assert hod.get("/admin").status_code == 404


def test_students_cannot_open_lecturer_analytics(student, seeded):
    assert student.get(f"/lecturer/module/{seeded['teaching_module_id']}/students").status_code == 403
    assert student.get("/lecturer/dashboard").status_code == 403
    assert student.get("/department").status_code == 403


def test_lecturer_sees_students_only_for_taught_modules(app, lecturer, seeded):
    assert lecturer.get(f"/lecturer/module/{seeded['teaching_module_id']}/students").status_code == 200
    with app.app_context():
        lecturer_id = User.query.filter_by(email="lecturer@dit.ac.tz").one().id
        taught = {a.module_id for a in LecturerAssignment.query.filter_by(lecturer_id=lecturer_id, status="approved")}
        other = Module.query.filter(~Module.id.in_(taught)).first().id
    assert lecturer.get(f"/lecturer/module/{other}/students").status_code == 403


def test_lecturer_cannot_modify_another_lecturers_resource(app, approved_lecturer, seeded):
    with app.app_context():
        seeded_lecturer = User.query.filter_by(email="lecturer@dit.ac.tz").one().id
        resource = Resource.query.filter_by(module_id=seeded["teaching_module_id"], uploaded_by_id=seeded_lecturer).first()
        resource_id, title = resource.id, resource.title
    # Same module, different owner: protected.
    assert approved_lecturer.get(f"/lecturer/resources/{resource_id}/edit").status_code == 403
    assert approved_lecturer.post(f"/lecturer/resources/{resource_id}/edit", data={"title": "Hijacked"}).status_code == 403
    assert approved_lecturer.post(f"/lecturer/resources/{resource_id}/delete").status_code == 403
    with app.app_context():
        assert db.session.get(Resource, resource_id).title == title


def test_lecturer_cannot_change_curriculum_structure(lecturer):
    assert lecturer.get("/department/curriculum").status_code == 403
    assert lecturer.post("/department/prospectus").status_code == 403


def test_students_cannot_read_other_students_context(app, student, new_student):
    other_id = user_id(app, new_student.registration_number)
    assert student.get(f"/department/students/{other_id}").status_code == 403
    mine = student.get("/api/v1/me/academic-context").get_json()
    assert mine["ok"] and mine["programme"]
    with app.app_context():
        assert db.session.get(User, other_id).full_name not in str(mine)


def test_api_hides_stopped_uploads_from_students(app, student, hod):
    with app.app_context():
        version = CurriculumVersion(label="DEMO stopped upload", academic_year_label="2051/2052", status="failed")
        db.session.add(version)
        db.session.commit()
        stopped_id = version.id
    student_view = student.get("/api/v1/curriculum/versions").get_json()["versions"]
    assert all(v["status"] in ("published", "archived") for v in student_view)
    assert any(v["label"] == "DEMO stopped upload" for v in hod.get("/api/v1/curriculum/versions").get_json()["versions"])
    assert student.get(f"/api/v1/curriculum/versions/{stopped_id}").status_code == 404
    assert "entries" in hod.get(f"/api/v1/curriculum/versions/{stopped_id}").get_json()["version"]


def test_lecturer_and_hod_cannot_use_student_only_endpoints(lecturer, hod):
    assert lecturer.get("/api/v1/me/academic-context").status_code == 403
    assert hod.get("/api/v1/me/recommendations").status_code == 403
    assert hod.get("/ai").status_code == 403
