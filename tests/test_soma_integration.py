"""SOMA adapter boundary, failure handling and the internal context mechanism."""

import io
from urllib.error import HTTPError, URLError

import pytest

import academic_context
from conftest import db, user_id
from integrations import soma
from models import (
    AuditLog, IntegrationSyncLog, Module, StudentAcademicContext, StudentModuleRegistration, User,
)


def _placement(user):
    return (user.programme_id, user.nta_level_id, user.semester_id, user.academic_year_id)


@pytest.fixture
def fresh_student(app, new_student):
    return user_id(app, new_student.registration_number), new_student


def _record_for(user, **overrides):
    data = {
        "department": user.department.name, "programme": user.programme.name,
        "nta_level": user.nta_level.level_number, "semester": user.semester.semester_number,
        "academic_year": user.academic_year.label, "registered_module_codes": [],
    }
    data.update(overrides)
    return data


def test_default_adapter_reports_not_configured(app):
    with app.app_context():
        adapter = soma.get_adapter(app.config)
        assert adapter.name == "none" and adapter.health_check()["status"] == "not_configured"
        with pytest.raises(soma.SomaNotConfigured):
            adapter.get_student_record("24030001")


def test_unconfigured_sync_keeps_context_and_logs(app, fresh_student):
    student_id, _ = fresh_student
    with app.app_context():
        student = db.session.get(User, student_id)
        before = _placement(student)
        result = academic_context.sync_from_soma(student)
        db.session.commit()
        assert not result["ok"] and result["status"] == "not_configured"
        assert _placement(db.session.get(User, student_id)) == before
        log = IntegrationSyncLog.query.filter_by(student_id=student_id).order_by(IntegrationSyncLog.id.desc()).first()
        assert log.status == "not_configured" and log.provider == "soma"


def test_http_adapter_refuses_to_guess_missing_contract():
    adapter = soma.HttpSomaAdapter(base_url="", api_key=None)
    missing = " ".join(adapter.missing_requirements())
    assert "SOMA_API_BASE_URL" in missing and "SOMA_API_KEY" in missing
    assert "official API specification" in missing
    with pytest.raises(soma.SomaNotConfigured):
        adapter.get_student_record("24030001")
    # Credentials alone are not enough: path, auth scheme and mapping must come from DIT.
    with pytest.raises(soma.SomaNotConfigured, match="SOMA_STUDENT_RECORD_PATH"):
        soma.HttpSomaAdapter(base_url="https://soma.invalid", api_key="k").get_student_record("1")


def _configured_http(mapper=None):
    return soma.HttpSomaAdapter(
        base_url="https://soma.invalid", api_key="test-key", student_record_path="/test/{student_id}",
        auth_headers=lambda adapter: {"X-Test-Auth": adapter.api_key},
        response_mapper=mapper or (lambda payload: soma.SomaStudentRecord(student_id=payload["id"])),
    )


@pytest.mark.parametrize("error, expected", [
    (HTTPError("https://soma.invalid", 401, "Unauthorized", {}, io.BytesIO()), soma.SomaAuthenticationError),
    (HTTPError("https://soma.invalid", 404, "Not found", {}, io.BytesIO()), soma.SomaRecordNotFound),
    (HTTPError("https://soma.invalid", 503, "Unavailable", {}, io.BytesIO()), soma.SomaUnavailable),
    (URLError("connection refused"), soma.SomaUnavailable),
])
def test_http_transport_maps_failures(monkeypatch, error, expected):
    def fake_urlopen(request, timeout):
        raise error
    monkeypatch.setattr(soma, "urlopen", fake_urlopen)
    with pytest.raises(expected):
        _configured_http().get_student_record("24030001")


def test_http_transport_rejects_unparseable_responses(monkeypatch):
    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False
    monkeypatch.setattr(soma, "urlopen", lambda request, timeout: Response(b"<html>not json</html>"))
    with pytest.raises(soma.SomaContractError):
        _configured_http().get_student_record("24030001")
    monkeypatch.setattr(soma, "urlopen", lambda request, timeout: Response(b'{"unexpected": true}'))
    with pytest.raises(soma.SomaContractError):
        _configured_http().get_student_record("24030001")


def test_mock_adapter_is_disabled_in_production():
    adapter = soma.get_adapter({"SOMA_ADAPTER": "mock", "IS_PRODUCTION": True})
    assert adapter.name == "none"


def test_mock_sync_matches_published_curriculum_and_registrations(app, fresh_student, seeded):
    student_id, client = fresh_student
    with app.app_context():
        student = db.session.get(User, student_id)
        module = db.session.get(Module, seeded["module_id"])
        original_code = module.code
        module.code = "DEMO-SOMA-1"
        db.session.commit()
        adapter = soma.MockSomaAdapter({student.registration_number: _record_for(
            student, registered_module_codes=["DEMO-SOMA-1", "DEMO-UNKNOWN-9"])})
        result = academic_context.sync_from_soma(student, adapter=adapter)
        db.session.commit()
        assert result["ok"] and result["status"] == "partial"
        assert result["unmatched_modules"] == ["DEMO-UNKNOWN-9"]
        row = StudentAcademicContext.query.filter_by(student_id=student_id).one()
        assert row.source == "soma" and row.last_synced_at is not None
        registration = StudentModuleRegistration.query.filter_by(student_id=student_id).one()
        assert registration.module_id == module.id and registration.source == "soma"
        assert AuditLog.query.filter_by(action="student_context.soma_synced", target_id=str(student_id)).count() == 1
        module.code = original_code
        db.session.commit()
    context = client.get("/api/v1/me/academic-context").get_json()
    assert context["source"] == "soma" and context["modules_from_registration"]


@pytest.mark.parametrize("failure, status", [
    (soma.SomaUnavailable, "unavailable"),
    (soma.SomaAuthenticationError, "auth_failed"),
    (soma.SomaRecordNotFound, "not_found"),
])
def test_soma_failures_leave_context_unchanged(app, fresh_student, failure, status):
    student_id, _ = fresh_student
    with app.app_context():
        student = db.session.get(User, student_id)
        before = _placement(student)
        result = academic_context.sync_from_soma(student, adapter=soma.MockSomaAdapter(failure=failure))
        db.session.commit()
        assert result["status"] == status and not result["ok"]
        assert result["message"] == failure.user_message
        assert _placement(db.session.get(User, student_id)) == before


def test_unmatched_programme_is_not_created(app, fresh_student):
    student_id, _ = fresh_student
    with app.app_context():
        student = db.session.get(User, student_id)
        before = _placement(student)
        adapter = soma.MockSomaAdapter({student.registration_number: _record_for(student, programme="DEMO Unknown")})
        result = academic_context.sync_from_soma(student, adapter=adapter)
        db.session.commit()
        assert result["status"] == "unmatched" and "not in the published curriculum" in result["message"]
        assert _placement(db.session.get(User, student_id)) == before
        from models import Programme
        assert Programme.query.filter_by(name="DEMO Unknown").first() is None


def test_admin_sync_button_reports_missing_configuration(app, admin, fresh_student):
    student_id, _ = fresh_student
    response = admin.post(f"/admin/students/{student_id}/soma-sync", follow_redirects=True)
    assert b"SOMA integration is not configured" in response.data


def test_admin_sets_internal_context_with_audit(app, admin, fresh_student, seeded):
    student_id, client = fresh_student
    with app.app_context():
        student = db.session.get(User, student_id)
        form = {"programme_id": student.programme_id, "nta_level_id": student.nta_level_id,
                "semester_id": student.semester_id, "academic_year_id": student.academic_year_id,
                "module_ids": [str(seeded["module_id"])]}
    assert admin.post(f"/admin/students/{student_id}/context", data=form).status_code == 302
    with app.app_context():
        assert StudentAcademicContext.query.filter_by(student_id=student_id).one().source == "internal_admin"
        assert StudentModuleRegistration.query.filter_by(student_id=student_id, module_id=seeded["module_id"]).count() == 1
        assert AuditLog.query.filter_by(action="student_context.updated", target_id=str(student_id)).count() == 1
    bad = dict(form, semester_id=999999)
    response = admin.post(f"/admin/students/{student_id}/context", data=bad, follow_redirects=True)
    assert b"Choose a programme" in response.data
