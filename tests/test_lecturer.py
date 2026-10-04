"""Lecturer workflows: module selection, topics and resource publishing."""

import io

import pytest

from conftest import db, sign_in, STUDENT
from models import LecturerAssignment, Module, Resource, Topic, User

TOPIC = {
    "unit_label": "Lab 1",
    "category": "practical",
    "status": "planned",
    "title": "Category Workflow Verification",
    "learning_outcome": "Apply the concept during a guided laboratory activity.",
    "description": "A practical introduction to the category workflow.",
    "is_published": "on",
}


@pytest.fixture
def topic_id(app, lecturer, seeded):
    module_id = seeded["teaching_module_id"]
    response = lecturer.post(f"/lecturer/module/{module_id}/content", data={"action": "topic", **TOPIC})
    assert response.status_code == 302
    with app.app_context():
        return Topic.query.filter_by(module_id=module_id, title=TOPIC["title"]).order_by(Topic.id.desc()).first().id


@pytest.mark.parametrize("path", ["/lecturer/workspace", "/lecturer/resources", "/lecturer/questions", "/lecturer/upload"])
def test_lecturer_pages_render(lecturer, path):
    assert lecturer.get(path).status_code == 200


def test_workspace_routes_selected_module(lecturer, seeded):
    module_id = seeded["teaching_module_id"]
    assert b"Select &amp; manage topics" in lecturer.get("/lecturer/workspace").data
    topics = lecturer.post("/lecturer/workspace", data={"module_id": module_id, "destination": "topics"})
    assert topics.headers["Location"].endswith(f"/lecturer/module/{module_id}/content")
    dashboard = lecturer.post("/lecturer/workspace", data={"module_id": module_id, "destination": "dashboard"})
    assert dashboard.headers["Location"].endswith("/lecturer/dashboard")
    assert lecturer.get("/lecturer/dashboard").status_code == 200


def test_lecturer_cannot_edit_unassigned_modules(app, lecturer):
    with app.app_context():
        lecturer_user = User.query.filter_by(email="lecturer@dit.ac.tz").one()
        assigned = {a.module_id for a in LecturerAssignment.query.filter_by(lecturer_id=lecturer_user.id, status="approved")}
        other = Module.query.filter(Module.id.notin_(assigned or [-1])).first()
        other_id = other.id if other else None
    if other_id is None:
        pytest.skip("seed data has no unassigned module")
    response = lecturer.post(f"/lecturer/module/{other_id}/content", data={"action": "topic", "title": "Must not be created"})
    assert response.status_code == 403


def test_topic_category_update_archive_and_restore(app, lecturer, student, seeded, topic_id):
    module_id = seeded["teaching_module_id"]
    with app.app_context():
        topic = db.session.get(Topic, topic_id)
        assert topic.category_label == "Practical / Lab"
    assert b"Practical / Lab" in lecturer.get(f"/lecturer/module/{module_id}/content").data

    updated = lecturer.post(f"/lecturer/module/{module_id}/topics/{topic_id}/update", data={
        **TOPIC, "unit_label": "Tutorial 1", "category": "tutorial", "status": "currently_teaching",
        "description": "A revised guided tutorial description.",
    })
    assert updated.status_code == 302
    with app.app_context():
        assert db.session.get(Topic, topic_id).category == "tutorial"

    module_page = student.get(f"/module/{module_id}").data
    assert b"Tutorial" in module_page and TOPIC["title"].encode() in module_page

    assert lecturer.post(f"/lecturer/module/{module_id}/topics/{topic_id}/delete").status_code == 302
    assert TOPIC["title"].encode() not in student.get(f"/module/{module_id}").data
    assert TOPIC["title"].encode() not in student.get(f"/learning-insights?module_id={module_id}").data

    assert lecturer.post(f"/lecturer/module/{module_id}/topics/{topic_id}/restore").status_code == 302
    assert TOPIC["title"].encode() in student.get(f"/module/{module_id}").data


def test_topic_reorder_moves_topic_up(app, lecturer, seeded, topic_id):
    module_id = seeded["teaching_module_id"]
    with app.app_context():
        if Topic.query.filter_by(module_id=module_id).count() < 2:
            pytest.skip("needs at least two topics")
    assert lecturer.post(f"/lecturer/module/{module_id}/topics/{topic_id}/move", data={"direction": "up"}).status_code == 302
    with app.app_context():
        ordered = Topic.query.filter_by(module_id=module_id).order_by(Topic.display_order, Topic.id).all()
        assert ordered[-1].id != topic_id


def test_upload_privacy_edit_and_delete(app, lecturer, seeded, topic_id):
    module_id = seeded["teaching_module_id"]
    assert lecturer.get(f"/lecturer/upload/module/{module_id}").status_code == 200
    upload = lecturer.post(f"/lecturer/upload/module/{module_id}", data={
        "title": "Pytest lecture note", "description": "Verified upload workflow test.",
        "resource_type": "notes", "topic_id": str(topic_id), "verified": "on",
        "file": (io.BytesIO(b"Feedback control keeps a system stable."), "pytest-note.txt"),
    }, content_type="multipart/form-data")
    assert upload.status_code == 302
    with app.app_context():
        resource = Resource.query.filter_by(title="Pytest lecture note").one()
        assert resource.stored_filename and resource.text_extraction_status == "success"
        assert resource.topic_id == topic_id
        resource_id = resource.id
        resource.verification_status = "pending"
        db.session.commit()

    viewer = sign_in(app.test_client(), *STUDENT)
    assert viewer.get(f"/resource/{resource_id}").status_code == 404
    assert viewer.get(f"/api/v1/resources/{resource_id}").status_code == 404
    with app.app_context():
        db.session.get(Resource, resource_id).verification_status = "verified"
        db.session.commit()
    served = viewer.get(f"/resource/{resource_id}/file")
    assert served.status_code == 200
    assert served.headers["X-Content-Type-Options"] == "nosniff"

    assert lecturer.get(f"/lecturer/resources/{resource_id}/edit").status_code == 200
    assert lecturer.post(f"/lecturer/resources/{resource_id}/edit", data={
        "title": "Pytest lecture note", "external_url": "not-a-url",
    }).status_code == 302
    assert lecturer.post(f"/lecturer/resources/{resource_id}/edit", data={
        "title": "Pytest lecture note (edited)", "description": "Updated safely.",
        "resource_type": "notes", "verified": "on",
    }).status_code == 302
    assert lecturer.post(f"/lecturer/resources/{resource_id}/delete").status_code == 302
    with app.app_context():
        assert db.session.get(Resource, resource_id) is None
