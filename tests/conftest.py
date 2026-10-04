"""Shared pytest fixtures.

Every run uses a disposable SQLite database, local file storage and no AI,
email or Supabase credentials, so tests never touch a developer's data, a
deployed database or a paid provider. The environment must be set before the
application (and its config) is imported, which is why it happens at module
import time here rather than inside a fixture.
"""

from __future__ import annotations

import itertools
import os
import sys
import tempfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

_DATABASE_FILE = Path(tempfile.gettempdir()) / f"smart_dit_pytest_{os.getpid()}.db"
_DATABASE_FILE.unlink(missing_ok=True)
os.environ.update({
    "DATABASE_URL": f"sqlite:///{_DATABASE_FILE.as_posix()}",
    "FLASK_DEBUG": "0",
    "SECRET_KEY": "pytest-key-not-for-production",
    "APP_ENV": "development",
    "GEMINI_API_KEY": "",
    "SUPABASE_URL": "",
    "SUPABASE_SERVICE_ROLE_KEY": "",
    "HOD_ACTIVATION_CODE": "isolated-test-hod-code",
    "LOAD_BUNDLED_PROSPECTUS": "0",
    "SMTP_HOST": "",
    "SMTP_USERNAME": "",
    "SMTP_PASSWORD": "",
    "MAIL_FROM": "",
})
for _key in ("INITIAL_STUDENT_PASSWORD", "INITIAL_LECTURER_PASSWORD", "CURRENT_ACADEMIC_YEAR"):
    os.environ.pop(_key, None)

import config  # noqa: E402

# Seeding writes starter files at import time of the app factory; keep them
# out of the project's uploads folder.
_UPLOADS = tempfile.TemporaryDirectory(prefix="smart-dit-pytest-")
config.RESOURCE_UPLOAD_DIR = _UPLOADS.name

from app import app as flask_app  # noqa: E402
from extensions import db  # noqa: E402
from models import AcademicYear, LecturerAssignment, Module, Resource, User  # noqa: E402

STUDENT = ("student@dit.ac.tz", "Student@123")
LECTURER = ("lecturer@dit.ac.tz", "Lecturer@123")
HOD_CODE = "isolated-test-hod-code"

_student_ids = itertools.count(24031000)
_lecturer_ids = itertools.count(14031000)


def pytest_sessionfinish(session, exitstatus):
    _UPLOADS.cleanup()


@pytest.fixture(scope="session")
def app():
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    yield flask_app


def sign_in(client, identifier, password):
    response = client.post("/login", data={"identifier": identifier, "password": password})
    assert response.status_code == 302, f"sign-in failed for {identifier}"
    return client


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def student(app):
    return sign_in(app.test_client(), *STUDENT)


@pytest.fixture
def lecturer(app):
    return sign_in(app.test_client(), *LECTURER)


@pytest.fixture
def seeded(app):
    """IDs from the seed data that most workflows need."""
    with app.app_context():
        resource = Resource.query.filter_by(verification_status="verified").first()
        lecturer_user = User.query.filter_by(email=LECTURER[0]).one()
        assignment = LecturerAssignment.query.filter_by(
            lecturer_id=lecturer_user.id, status="approved"
        ).first()
        student_user = User.query.filter_by(email=STUDENT[0]).one()
        return {
            "resource_id": resource.id,
            "module_id": resource.module_id,
            "teaching_module_id": assignment.module_id,
            "department_id": student_user.department_id,
            "programme_id": student_user.programme_id,
            "level_id": student_user.nta_level_id,
            "semester_id": student_user.semester_id,
        }


def placement(seeded):
    return {
        "department_id": seeded["department_id"],
        "programme_id": seeded["programme_id"],
        "nta_level_id": seeded["level_id"],
        "semester_id": seeded["semester_id"],
    }


@pytest.fixture
def new_student(app, seeded):
    """Register a fresh student account and return a signed-in client."""
    number = str(next(_student_ids))
    password = "StudentPass@123"
    response = app.test_client().post("/register", data={
        "registration_number": number, "full_name": "Test Student",
        "email": f"student.{number}@example.test",
        "password": password, "password_confirm": password,
        **placement(seeded),
    })
    assert response.status_code == 302
    client = sign_in(app.test_client(), number, password)
    client.registration_number = number
    return client


@pytest.fixture
def register_lecturer(app, seeded):
    """Factory: submit a lecturer registration; returns (registration number, password)."""
    def _register():
        number = str(next(_lecturer_ids))
        password = "LecturerPass@123"
        response = app.test_client().post("/register", data={
            "registration_number": number, "full_name": f"Lecturer {number}",
            "email": f"lecturer.{number}@example.test",
            "password": password, "password_confirm": password,
            "department_id": seeded["department_id"],
            "teaching_interest": "Control Engineering",
        })
        assert response.status_code == 302
        return number, password
    return _register


@pytest.fixture(scope="session")
def hod(app):
    """The department's single Head of Department, activated once per run."""
    with app.app_context():
        department_id = User.query.filter_by(email=STUDENT[0]).one().department_id
    password = "DepartmentHead@123"
    response = app.test_client().post("/register", data={
        "registration_number": "50000001", "full_name": "Electrical Head",
        "email": "hod@example.test", "password": password, "password_confirm": password,
        "department_id": department_id, "hod_activation_code": HOD_CODE,
    })
    assert response.status_code == 302
    return sign_in(app.test_client(), "50000001", password)


@pytest.fixture
def approved_lecturer(app, hod, register_lecturer, seeded):
    number, password = register_lecturer()
    lecturer_id = user_id(app, number)
    approval = hod.post(f"/department/lecturer-requests/{lecturer_id}/approve", data={
        "module_ids": str(seeded["teaching_module_id"]),
    })
    assert approval.status_code == 302
    client = sign_in(app.test_client(), number, password)
    client.user_id = lecturer_id
    client.credentials = (number, password)
    return client


@pytest.fixture
def hod_module_id(app, seeded):
    """A fresh live module in the student's semester (modules normally come from a prospectus)."""
    with app.app_context():
        year = AcademicYear.query.filter_by(department_id=seeded["department_id"], is_current=True).one()
        module = Module(semester_id=seeded["semester_id"], academic_year_id=year.id,
                        code=f"EE-TEST-{Module.query.count():03d}", name="Academic Workflow Verification",
                        module_type="general_studies", cohort_label="EE5-QA",
                        description="Isolated test curriculum module.", publication_status="published",
                        is_active=True, provenance="prospectus")
        db.session.add(module)
        db.session.commit()
        return module.id


def user_id(app, registration_number):
    with app.app_context():
        return User.query.filter_by(registration_number=registration_number).one().id


__all__ = ["db", "sign_in", "placement", "user_id"]

