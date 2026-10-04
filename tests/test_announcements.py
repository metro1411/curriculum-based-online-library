"""Lecturer → student announcements: audience, delivery, guards and removal."""

import itertools

import pytest

import announcements
from conftest import STUDENT, db
from models import Announcement, AuditLog, Module, Notification, User

_titles = itertools.count(1)


def _send(client, module_id, **extra):
    title = extra.pop("title", f"Lab update {next(_titles)}")
    response = client.post("/lecturer/announcements", data={
        "module_id": module_id, "title": title,
        "body": extra.pop("body", "Lab 2 moves to Thursday 10:00 in Room B12."), **extra,
    })
    return title, response


def _student_id(app):
    with app.app_context():
        return User.query.filter_by(email=STUDENT[0]).one().id


def test_pages_render_for_each_role(lecturer, student):
    assert lecturer.get("/lecturer/announcements").status_code == 200
    assert student.get("/announcements").status_code == 200
    assert student.get("/lecturer/announcements").status_code == 403


def test_announcement_reaches_enrolled_students(app, lecturer, student, seeded):
    module_id = seeded["teaching_module_id"]
    title, response = _send(lecturer, module_id, is_pinned="on")
    assert response.status_code == 302

    with app.app_context():
        item = Announcement.query.filter_by(title=title).one()
        audience = announcements.module_audience(db.session.get(Module, module_id))
        assert item.recipient_count == len(audience) > 0
        assert item.is_pinned and not item.emailed
        alert = Notification.query.filter_by(user_id=_student_id(app), kind="announcement", is_read=False).filter(
            Notification.title.contains(title)
        ).one()
        assert alert.email_status == "in_app_only"
        assert alert.target_url.endswith(f"#{item.anchor}")
        assert AuditLog.query.filter_by(action="announcement.published", target_id=str(item.id)).count() == 1

    # Visible on the dashboard, the module page and the announcements page.
    assert title.encode() in student.get("/dashboard").data
    assert title.encode() in student.get(f"/module/{module_id}").data
    page = student.get("/announcements")
    assert title.encode() in page.data
    assert b"Ask about this" in page.data

    # Opening the page clears the student's unread announcement alerts.
    with app.app_context():
        assert announcements.unread_count(db.session.get(User, _student_id(app))) == 0


def test_unread_announcements_badge_in_sidebar(app, lecturer, student, seeded):
    with app.app_context():
        announcements.mark_read_for(db.session.get(User, _student_id(app)))
        db.session.commit()
    _send(lecturer, seeded["teaching_module_id"])
    sidebar = student.get("/dashboard").get_data(as_text=True)
    sidebar = sidebar[sidebar.index('<aside class="sidebar"'):sidebar.index("</aside>")]
    start = sidebar.index('data-nav-label="Announcements"')
    announcements_link = sidebar[start:sidebar.index("</a>", start)]
    assert 'class="sidebar__badge"' in announcements_link


def test_email_option_is_recorded(app, lecturer, seeded):
    title, _ = _send(lecturer, seeded["teaching_module_id"], send_email="on")
    with app.app_context():
        item = Announcement.query.filter_by(title=title).one()
        assert item.emailed
        statuses = {n.email_status for n in Notification.query.filter(Notification.title.contains(title))}
        # SMTP is not configured in tests, so delivery is attempted and reported.
        assert statuses == {"not_configured"}


def test_lecturer_cannot_post_to_a_module_they_do_not_teach(app, lecturer):
    with app.app_context():
        lecturer_user = User.query.filter_by(email="lecturer@dit.ac.tz").one()
        taught = {a.module_id for a in lecturer_user_assignments(lecturer_user)}
        other = Module.query.filter(Module.id.notin_(taught or [-1])).first()
        other_id = other.id if other else None
    if other_id is None:
        pytest.skip("seed data has no unassigned module")
    title, response = _send(lecturer, other_id)
    assert response.status_code == 302
    with app.app_context():
        assert Announcement.query.filter_by(title=title).count() == 0


def lecturer_user_assignments(user):
    from models import LecturerAssignment
    return LecturerAssignment.query.filter_by(lecturer_id=user.id, status="approved").all()


def test_duplicate_send_is_ignored(app, lecturer, seeded):
    title, _ = _send(lecturer, seeded["teaching_module_id"], body="Bring your calculators tomorrow.")
    _send(lecturer, seeded["teaching_module_id"], title=title, body="Bring your calculators tomorrow.")
    with app.app_context():
        assert Announcement.query.filter_by(title=title).count() == 1


def test_validation_rejects_empty_and_oversized_messages(app, lecturer, seeded):
    module_id = seeded["teaching_module_id"]
    with app.app_context():
        before = Announcement.query.count()
    _send(lecturer, module_id, body="   ")
    _send(lecturer, module_id, title="x" * (announcements.TITLE_MAX + 1))
    with app.app_context():
        assert Announcement.query.count() == before


def test_hourly_rate_limit(app, approved_lecturer, seeded):
    module_id = seeded["teaching_module_id"]
    for index in range(announcements.HOURLY_LIMIT):
        _, response = _send(approved_lecturer, module_id, title=f"Rate test {index}")
        assert response.status_code == 302
    title, _ = _send(approved_lecturer, module_id, title="One too many")
    with app.app_context():
        assert Announcement.query.filter_by(author_id=approved_lecturer.user_id).count() == announcements.HOURLY_LIMIT
        assert Announcement.query.filter_by(title=title).count() == 0


def test_removal_clears_unread_alerts_and_is_audited(app, lecturer, student, seeded):
    module_id = seeded["teaching_module_id"]
    title, _ = _send(lecturer, module_id)
    with app.app_context():
        item = Announcement.query.filter_by(title=title).one()
        item_id = item.id
        target = announcements.target_url(item)
    response = lecturer.post(f"/lecturer/module/{module_id}/announcements/{item_id}/delete",
                             data={"return_to": "announcements"})
    assert response.headers["Location"].endswith("/lecturer/announcements")
    with app.app_context():
        assert db.session.get(Announcement, item_id) is None
        assert Notification.query.filter_by(target_url=target, is_read=False).count() == 0
        assert AuditLog.query.filter_by(action="announcement.removed", target_id=str(item_id)).count() == 1
    assert title.encode() not in student.get("/announcements").data


def test_module_workspace_quick_announcement_uses_the_same_rules(app, lecturer, seeded):
    module_id = seeded["teaching_module_id"]
    response = lecturer.post(f"/lecturer/module/{module_id}/content", data={
        "action": "announcement", "title": "Quick workspace notice", "body": "Tutorial notes are now online.",
    })
    assert response.status_code == 302
    with app.app_context():
        item = Announcement.query.filter_by(title="Quick workspace notice").one()
        assert item.recipient_count > 0


def test_question_form_preselects_module_from_announcement(student, seeded):
    html = student.get(f"/questions?module_id={seeded['teaching_module_id']}").get_data(as_text=True)
    assert f'value="{seeded["teaching_module_id"]}" selected' in html


def test_registered_lecturer_can_announce_after_claim_approval(app, hod, register_lecturer, new_student, hod_module_id):
    """The path a real staff member follows: register, get verified, claim a
    module, wait for the HOD, then message the class."""
    from conftest import sign_in, user_id
    from models import LecturerAssignment

    number, password = register_lecturer()
    lecturer_id = user_id(app, number)
    assert hod.post(f"/department/lecturer-requests/{lecturer_id}/approve").status_code == 302
    lecturer = sign_in(app.test_client(), number, password)

    assert lecturer.post("/lecturer/workspace", data={"action": "claim", "module_id": hod_module_id}).status_code == 302
    pending_page = lecturer.get("/lecturer/announcements").get_data(as_text=True)
    assert "waiting for Head of Department approval" in pending_page
    title, _ = _send(lecturer, hod_module_id)
    with app.app_context():
        assert Announcement.query.filter_by(title=title).count() == 0  # not yet allowed
        claim_id = LecturerAssignment.query.filter_by(lecturer_id=lecturer_id, module_id=hod_module_id).one().id

    assert hod.post(f"/department/module-claims/{claim_id}/approve").status_code == 302
    ready_page = lecturer.get("/lecturer/announcements").get_data(as_text=True)
    assert f'value="{hod_module_id}" data-reach=' in ready_page
    assert "waiting for Head of Department approval" not in ready_page

    title, response = _send(lecturer, hod_module_id, body="Welcome to the module. Our first lab is on Monday.")
    assert response.status_code == 302
    with app.app_context():
        item = Announcement.query.filter_by(title=title).one()
        assert item.author_id == lecturer_id and item.recipient_count >= 2
    page = new_student.get("/announcements").get_data(as_text=True)
    assert title in page and "Welcome to the module" in page


def test_unpublished_module_is_explained_not_silently_hidden(app, hod, approved_lecturer, hod_module_id):
    from models import LecturerAssignment
    with app.app_context():
        db.session.add(LecturerAssignment(lecturer_id=approved_lecturer.user_id, module_id=hod_module_id, status="approved"))
        module = db.session.get(Module, hod_module_id)
        module.publication_status = "draft"
        db.session.commit()
    page = approved_lecturer.get("/lecturer/announcements").get_data(as_text=True)
    assert "not published yet" in page
    title, _ = _send(approved_lecturer, hod_module_id)
    with app.app_context():
        assert Announcement.query.filter_by(title=title).count() == 0
