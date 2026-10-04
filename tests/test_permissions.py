"""Role-based access control for curriculum, resources and student data."""

import pytest

from conftest import db, user_id
from models import CurriculumVersion, LecturerAssignment, Module, Resource, User

HOD_PAGES = ["/department/students", "/department/curriculum"]
ADMIN_PAGES = ["/admin", "/admin/prospectus", "/admin/students", "/admin/audit-log"]


@pytest.mark.parametrize("path", HOD_PAGES)
def test_hod_pages_render_for_hod(hod, path):
    assert hod.get(path).status_code == 200


@pytest.mark.parametrize("path", HOD_PAGES)
def test_students_and_lecturers_cannot_open_hod_pages(student, lecturer, path):
    for client in (student, lecturer):
        assert client.get(path).status_code == 403


@pytest.mark.parametrize("path", ADMIN_PAGES)
def test_admin_pages_render_for_admin_only(admin, hod, student, lecturer, path):
    assert admin.get(path).status_code == 200
    for client in (hod, student, lecturer):
        assert client.get(path).status_code == 403


def test_admin_cannot_open_hod_pages(admin):
    assert admin.get("/department/curriculum").status_code == 403


def test_anonymous_users_are_sent_to_login(client):
    for path in ("/admin/prospectus", "/department/curriculum"):
        response = client.get(path)
        assert response.status_code == 302 and "/login" in response.headers["Location"]


def test_only_admin_can_publish_or_undo_a_prospectus(app, hod, student, lecturer):
    with app.app_context():
        before = CurriculumVersion.query.count()
    for client in (hod, student, lecturer):
        assert client.post("/admin/prospectus", data={"title": "Unauthorised"}).status_code == 403
        assert client.post("/admin/prospectus/undo").status_code == 403
    with app.app_context():
        assert CurriculumVersion.query.count() == before


def test_admin_signs_in_to_the_admin_dashboard(app, admin):
    with app.app_context():
        user = User.query.filter_by(registration_number="90000001").one()
        assert user.is_admin and user.department_id is None and user.is_active_account
    response = app.test_client().post("/login", data={"identifier": "90000001", "password": "CurriculumAdmin@123"})
    assert response.headers["Location"].endswith("/admin/dashboard")


def test_admin_sign_up_needs_the_activation_code(app, client):
    response = client.post("/register", data={
        "registration_number": "90000099", "full_name": "Not An Admin", "email": "notadmin@example.test",
        "password": "Password@12345", "password_confirm": "Password@12345", "admin_activation_code": "wrong",
    })
    assert response.status_code == 200 and b"activation code" in response.data
    with app.app_context():
        assert User.query.filter_by(registration_number="90000099").first() is None


def test_switched_off_admins_are_restored(app):
    from app import _apply_schema_migrations

    with app.app_context():
        user = User(full_name="Former Admin", email="former.admin@example.test", username="former.admin",
                    role="department_head", is_active_account=False)
        user.set_password("FormerAdmin@123")
        db.session.add(user)
        db.session.commit()
        _apply_schema_migrations(app)
        db.session.expire_all()
        restored = User.query.filter_by(email="former.admin@example.test").one()
        assert restored.is_admin and restored.is_active_account
        assert User.query.filter(User.role == "department_head", User.department_id.is_(None)).count() == 0


def test_create_curriculum_admin_command(app):
    result = app.test_cli_runner().invoke(args=[
        "create-curriculum-admin", "--email", "cli.admin@example.test", "--name", "CLI Admin",
        "--password", "CliAdminPass@123"])
    assert result.exit_code == 0, result.output
    with app.app_context():
        assert User.query.filter_by(email="cli.admin@example.test").one().is_admin


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
    assert lecturer.post("/department/curriculum", data={"action": "module"}).status_code == 403
    assert lecturer.post("/admin/prospectus").status_code == 403


def test_students_cannot_read_other_students_context(app, student, new_student):
    other_id = user_id(app, new_student.registration_number)
    assert student.get(f"/department/students/{other_id}").status_code == 403
    mine = student.get("/api/v1/me/academic-context").get_json()
    assert mine["ok"] and mine["programme"]
    with app.app_context():
        assert db.session.get(User, other_id).full_name not in str(mine)


def test_api_hides_stopped_uploads_from_students(app, student, hod, admin):
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
    assert "entries" in admin.get(f"/api/v1/curriculum/versions/{stopped_id}").get_json()["version"]


def test_lecturer_and_hod_cannot_use_student_only_endpoints(lecturer, hod):
    assert lecturer.get("/api/v1/me/academic-context").status_code == 403
    assert hod.get("/api/v1/me/recommendations").status_code == 403
    assert hod.get("/ai").status_code == 403
