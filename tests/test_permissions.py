"""Role-based access control for curriculum, resources and student data."""

import pytest

from conftest import ADMIN, db, sign_in, user_id
from models import AuditLog, LecturerAssignment, Module, Resource, User

ADMIN_PAGES = ["/admin", "/admin/prospectus", "/admin/students", "/admin/audit-log"]


@pytest.mark.parametrize("path", ADMIN_PAGES)
def test_admin_pages_render_for_admin(admin, path):
    assert admin.get(path).status_code == 200


@pytest.mark.parametrize("path", ADMIN_PAGES)
def test_students_lecturers_and_hods_cannot_open_admin(student, lecturer, hod, path):
    for client in (student, lecturer, hod):
        assert client.get(path).status_code == 403


def test_anonymous_users_are_sent_to_login(client):
    response = client.get("/admin")
    assert response.status_code == 302 and "/login" in response.headers["Location"]


def test_only_admin_can_create_or_publish_curriculum(app, student, lecturer, hod):
    for client in (student, lecturer, hod):
        assert client.post("/admin/versions", data={
            "label": "Unauthorised", "academic_year_label": "2050/2051"}).status_code == 403
        assert client.post("/admin/versions/1/publish").status_code == 403
        assert client.post("/admin/versions/1/archive").status_code == 403
    with app.app_context():
        from models import CurriculumVersion
        assert CurriculumVersion.query.filter_by(label="Unauthorised").first() is None


def test_admin_login_lands_on_admin_workspace(app, admin):
    response = app.test_client().post("/login", data={"identifier": ADMIN[0], "password": ADMIN[1]})
    assert response.headers["Location"].endswith("/admin/dashboard")


def test_admin_creation_is_audited_as_role_change(app, admin):
    with app.app_context():
        row = AuditLog.query.filter_by(action="role.changed").order_by(AuditLog.id.desc()).first()
        assert row is not None and '"to": "admin"' in row.details_json


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


def test_lecturer_cannot_change_curriculum_structure(lecturer, seeded):
    assert lecturer.post("/department/curriculum", data={"action": "module"}).status_code == 403
    assert lecturer.post(f"/department/curriculum/modules/{seeded['module_id']}/archive").status_code == 403


def test_students_cannot_read_other_students_context(app, student, new_student):
    other_id = user_id(app, new_student.registration_number)
    assert student.get(f"/admin/students/{other_id}/context").status_code == 403
    mine = student.get("/api/v1/me/academic-context").get_json()
    assert mine["ok"] and mine["programme"]
    with app.app_context():
        assert db.session.get(User, other_id).full_name not in str(mine)


def test_api_hides_unpublished_versions_from_students(app, admin, student, hod):
    admin.post("/admin/versions", data={"label": "DEMO hidden draft", "academic_year_label": "2051/2052"})
    student_view = student.get("/api/v1/curriculum/versions").get_json()["versions"]
    assert all(v["status"] == "published" for v in student_view)
    hod_view = hod.get("/api/v1/curriculum/versions").get_json()["versions"]
    assert any(v["label"] == "DEMO hidden draft" for v in hod_view)
    with app.app_context():
        from models import CurriculumVersion
        draft_id = CurriculumVersion.query.filter_by(label="DEMO hidden draft").one().id
    assert student.get(f"/api/v1/curriculum/versions/{draft_id}").status_code == 404
    assert "entries" in admin.get(f"/api/v1/curriculum/versions/{draft_id}").get_json()["version"]


def test_lecturer_and_admin_cannot_use_student_only_endpoints(lecturer, admin):
    assert lecturer.get("/api/v1/me/academic-context").status_code == 403
    assert admin.get("/api/v1/me/recommendations").status_code == 403
    assert admin.get("/ai").status_code == 403
