"""Student learning workflows: library, resources, AI, streaks and privacy."""

from datetime import timedelta
from unittest.mock import patch

import pytest

import ai_engine
from conftest import db
from models import AIAnswerFeedback, LearningEvent, ResourceStudySession, StudentPreference, User, utcnow

GENERATED = {
    "ok": True,
    "answer_text": "## Worked solution\n\nFor $V = IR$, substitute the known values.",
    "answer_html": "<h2>Worked solution</h2><p>For <span class=\"math-inline\">V = IR</span>, substitute the known values.</p>",
    "sources": [],
    "web_sources": [],
    "general_guidance": False,
    "web_grounded": False,
    "followups": ["Show a numerical example"],
}


@pytest.mark.parametrize("path", [
    "/dashboard", "/archive", "/search?q=control", "/learning-insights", "/ai",
    "/downloads", "/questions", "/notifications", "/profile",
])
def test_student_pages_render(student, path):
    assert student.get(path).status_code == 200


def test_module_resource_and_bookmark(student, seeded):
    assert student.get(f"/module/{seeded['module_id']}").status_code == 200
    assert student.get(f"/resource/{seeded['resource_id']}").status_code == 200
    assert student.post(f"/resource/{seeded['resource_id']}/save").status_code == 302


def test_curriculum_api_lists_departments(student):
    response = student.get("/api/v1/curriculum")
    assert response.status_code == 200
    assert response.get_json()["departments"]


def test_ai_is_explicitly_unavailable_without_a_provider(student, seeded):
    response = student.post("/ai/ask", json={"message": "Explain feedback control.", "module_id": seeded["module_id"]})
    assert response.status_code == 503


def test_ai_answer_and_feedback_are_saved_once(app, student, seeded):
    with patch.object(ai_engine, "is_available", return_value=True), patch.object(ai_engine, "ask", return_value=GENERATED):
        answer = student.post("/ai/ask", json={
            "message": "Solve V = IR step by step.", "module_id": seeded["module_id"],
            "mode": "solve", "response_style": "detailed",
        })
    assert answer.status_code == 200
    data = answer.get_json()
    assert data["ok"] and "math-inline" in data["answer_html"]

    message_id = data["message_id"]
    assert student.post("/ai/feedback", json={"message_id": message_id, "rating": 3}).status_code == 200
    assert student.post("/ai/feedback", json={
        "message_id": message_id, "rating": 1, "is_unclear": True,
        "note": "The final equation sign should be checked.",
    }).status_code == 200
    with app.app_context():
        rows = AIAnswerFeedback.query.filter_by(message_id=message_id).all()
        assert len(rows) == 1
        assert rows[0].is_unclear and "equation sign" in rows[0].note


def test_academic_api_question_counts_for_streak_without_leaking_text(app, student, seeded):
    with app.app_context():
        before = LearningEvent.query.filter_by(event_type="academic_ai_question", qualifies_for_streak=True).count()
    with patch.object(ai_engine, "is_available", return_value=True), patch.object(ai_engine, "ask", return_value=GENERATED):
        response = student.post("/api/v1/ai/ask", json={
            "question": "Explain how feedback control improves system stability.",
            "module_id": seeded["module_id"], "mode": "explain",
        })
    assert response.status_code == 200
    with app.app_context():
        events = LearningEvent.query.filter_by(event_type="academic_ai_question", qualifies_for_streak=True).all()
        assert len(events) == before + 1
        assert not any("feedback control improves" in (event.detail or "").lower() for event in events)


def test_ten_active_minutes_qualify_the_streak(app, student, seeded):
    start = student.post(f"/resource/{seeded['resource_id']}/study/start")
    assert start.status_code == 200
    token = start.get_json()["token"]
    with app.app_context():
        session = ResourceStudySession.query.filter_by(token=token).one()
        session.active_seconds = 590
        session.last_heartbeat_at = utcnow() - timedelta(seconds=35)
        db.session.commit()
    heartbeat = student.post(f"/resource/study/{token}/heartbeat")
    assert heartbeat.status_code == 200
    assert heartbeat.get_json()["qualified"]


def test_profile_preferences_persist(app, new_student):
    response = new_student.post("/profile", data={
        "action": "profile", "full_name": "Test Student", "learning_goal": "Master control systems",
        "weekly_goal_minutes": 180, "data_saver": "on",
    })
    assert response.status_code == 302
    with app.app_context():
        user = User.query.filter_by(registration_number=new_student.registration_number).one()
        preference = StudentPreference.query.filter_by(student_id=user.id).one()
        assert preference.data_saver and preference.learning_goal == "Master control systems"
    # Data Saver is reflected on the page shell.
    assert b'data-data-saver="true"' in new_student.get("/dashboard").data


def test_ai_conversations_are_private_to_their_owner(student, new_student, seeded):
    private = {**GENERATED, "answer_text": "Private learner answer", "answer_html": "<p>Private learner answer</p>"}
    with patch.object(ai_engine, "is_available", return_value=True), patch.object(ai_engine, "ask", return_value=private):
        answer = new_student.post("/ai/ask", json={
            "message": "My private question", "module_id": seeded["teaching_module_id"], "mode": "explain",
        })
    assert answer.status_code == 200
    conversation_id = answer.get_json()["conversation_id"]
    other = student.get(f"/ai?conversation_id={conversation_id}")
    assert other.status_code == 200
    assert b"Private learner answer" not in other.data
