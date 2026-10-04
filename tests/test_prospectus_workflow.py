"""Prospectus upload -> extract -> review -> validate -> approve -> publish -> archive."""

import io
import itertools
from decimal import Decimal

import pytest

import curriculum_service as svc
from conftest import db
from models import (
    AuditLog, CurriculumEntry, CurriculumVersion, Department, DraftProgramme, Module,
    ModulePrerequisite, NtaLevel, Programme, ProspectusChunk, ProspectusDocument, User,
)

_ids = itertools.count(1)


def _unique(prefix="DEMO"):
    return f"{prefix} {next(_ids):03d}"


def _csv(department, programme, rows):
    header = "department,programme,nta_level,year_label,semester,module_code,module_name,module_type,credits,prerequisites,description\n"
    body = "".join(
        f"{department},{programme},{level},,{semester},{code},{name},core,{credits},{prereq},DEMO row\n"
        for level, semester, code, name, credits, prereq in rows
    )
    return (header + body).encode("utf-8")


def _upload(admin, content, filename, *, label, year="2031/2032", create_version=True):
    data = {
        "title": f"{label} prospectus", "academic_year_label": year,
        "version_label": label, "file": (io.BytesIO(content), filename), "is_demo": "1",
    }
    if create_version:
        data["create_version"] = "1"
    return admin.post("/admin/prospectus", data=data, content_type="multipart/form-data")


def _version(app, label):
    with app.app_context():
        return CurriculumVersion.query.filter(CurriculumVersion.label.contains(label)).one().id


def _review_all(app, version_id):
    with app.app_context():
        for entry in CurriculumEntry.query.filter_by(version_id=version_id):
            entry.review_status = "reviewed"
        db.session.commit()


def _staged_demo_version(app, admin, rows=None):
    label = _unique()
    department, programme = f"{label} Dept", f"{label} Programme"
    rows = rows or [(4, 1, f"D{next(_ids):03d}A", "DEMO Module A", 10, ""),
                    (4, 2, f"D{next(_ids):03d}B", "DEMO Module B", 12, "")]
    response = _upload(admin, _csv(department, programme, rows), "demo.csv", label=label)
    assert response.status_code == 302
    return _version(app, label), department, programme, rows


def test_upload_preserves_original_and_stages_entries_for_review(app, admin):
    version_id, department, programme, rows = _staged_demo_version(app, admin)
    with app.app_context():
        version = db.session.get(CurriculumVersion, version_id)
        assert version.status == "draft" and version.is_demo and version.label.startswith("DEMO")
        assert version.prospectus_document.sha256 and version.prospectus_document.extraction_status == "success"
        assert {e.review_status for e in version.entries} == {"needs_review"}
        assert {e.origin for e in version.entries} == {"csv"}
        # Nothing is live before publication.
        assert Department.query.filter_by(name=department).first() is None
        assert AuditLog.query.filter_by(action="prospectus.uploaded").count() >= 1
        document_id = version.prospectus_document_id
    original = admin.get(f"/admin/prospectus/{document_id}/file")
    assert original.status_code == 200 and b"DEMO Module A" in original.data


def test_unreviewed_entries_block_validation(app, admin):
    version_id, *_ = _staged_demo_version(app, admin)
    admin.post(f"/admin/versions/{version_id}/validate")
    with app.app_context():
        version = db.session.get(CurriculumVersion, version_id)
        assert version.status == "draft"
        report = svc.ValidationReport.from_json(version.last_validation_json)
        assert any("reviewed" in issue["message"] for issue in report.errors)
    refused = admin.post(f"/admin/versions/{version_id}/approve")
    assert refused.status_code == 302
    with app.app_context():
        assert db.session.get(CurriculumVersion, version_id).status == "draft"


def test_full_workflow_publishes_live_curriculum_and_archives_without_deleting(app, admin):
    label = _unique()
    department, programme = f"{label} Dept", f"{label} Programme"
    a, b = f"P{next(_ids):03d}A", f"P{next(_ids):03d}B"
    _upload(admin, _csv(department, programme, [
        (4, 1, a, "DEMO Foundations", 10, ""),
        (4, 2, b, "DEMO Follow-on", "", a),
    ]), "demo.csv", label=label, year="2032/2033")
    version_id = _version(app, label)
    _review_all(app, version_id)

    assert admin.post(f"/admin/versions/{version_id}/validate").status_code == 302
    assert admin.post(f"/admin/versions/{version_id}/approve").status_code == 302
    assert admin.post(f"/admin/versions/{version_id}/publish", data={"make_current": "1"}).status_code == 302

    with app.app_context():
        version = db.session.get(CurriculumVersion, version_id)
        assert version.status == "published" and version.published_by_id
        modules = Module.query.filter_by(curriculum_version_id=version_id).all()
        assert {m.code for m in modules} == {a, b}
        follow_on = next(m for m in modules if m.code == b)
        assert follow_on.provenance == "prospectus" and follow_on.credits is None
        assert follow_on.semester.nta_level.programme.name == programme
        assert follow_on.semester.nta_level.programme.department.name == department
        assert [p.code for p in follow_on.prerequisites] == [a]
        assert follow_on.academic_year.label == "2032/2033" and follow_on.academic_year.is_current
        assert next(m for m in modules if m.code == a).credits == Decimal("10")
        assert all(e.live_module_id for e in version.entries)

    # Published versions are immutable.
    with app.app_context():
        entry_id = CurriculumEntry.query.filter_by(version_id=version_id).first().id
    admin.post(f"/admin/versions/{version_id}/entries/{entry_id}", data={"module_name": "Changed"})
    admin.post(f"/admin/versions/{version_id}/delete")
    with app.app_context():
        assert db.session.get(CurriculumEntry, entry_id).module_name != "Changed"
        assert db.session.get(CurriculumVersion, version_id) is not None

    assert admin.post(f"/admin/versions/{version_id}/archive").status_code == 302
    with app.app_context():
        version = db.session.get(CurriculumVersion, version_id)
        assert version.status == "archived"
        modules = Module.query.filter_by(curriculum_version_id=version_id).all()
        assert modules and all(m.publication_status == "archived" and not m.is_active for m in modules)
        assert ModulePrerequisite.query.filter(ModulePrerequisite.module_id.in_([m.id for m in modules])).count() == 1


def test_editing_a_validated_version_returns_it_to_draft(app, admin):
    version_id, *_ = _staged_demo_version(app, admin)
    _review_all(app, version_id)
    admin.post(f"/admin/versions/{version_id}/validate")
    with app.app_context():
        assert db.session.get(CurriculumVersion, version_id).status == "validated"
        entry = CurriculumEntry.query.filter_by(version_id=version_id).first()
        form = {"programme_id": entry.programme_id, "nta_level": entry.nta_level,
                "semester_number": entry.semester_number, "module_code": entry.module_code,
                "module_name": "DEMO Corrected Name", "module_type": "core", "credits": "10",
                "review_status": "reviewed"}
        entry_id = entry.id
    admin.post(f"/admin/versions/{version_id}/entries/{entry_id}", data=form)
    with app.app_context():
        assert db.session.get(CurriculumEntry, entry_id).module_name == "DEMO Corrected Name"
        assert db.session.get(CurriculumVersion, version_id).status == "draft"


def test_publish_requires_approval(app, admin):
    version_id, *_ = _staged_demo_version(app, admin)
    _review_all(app, version_id)
    admin.post(f"/admin/versions/{version_id}/validate")
    admin.post(f"/admin/versions/{version_id}/publish")
    with app.app_context():
        assert db.session.get(CurriculumVersion, version_id).status == "validated"
        assert Module.query.filter_by(curriculum_version_id=version_id).count() == 0


def test_invalid_document_is_rejected(app, admin):
    response = admin.post("/admin/prospectus", data={
        "title": "Disguised file", "file": (io.BytesIO(b"not really a pdf"), "prospectus.pdf"),
    }, content_type="multipart/form-data", follow_redirects=True)
    assert b"do not match" in response.data
    response = admin.post("/admin/prospectus", data={
        "title": "Wrong type", "file": (io.BytesIO(b"MZ"), "prospectus.exe"),
    }, content_type="multipart/form-data", follow_redirects=True)
    assert b"PDF, DOCX, TXT, MD or CSV" in response.data


def test_unreadable_prospectus_is_kept_with_failed_extraction(app, admin):
    label = _unique()
    response = _upload(admin, b"%PDF-1.4\n%%EOF", "scanned.pdf", label=label)
    assert response.status_code == 302
    with app.app_context():
        document = ProspectusDocument.query.filter_by(title=f"{label} prospectus").one()
        assert document.extraction_status == "failed" and document.extraction_message
        version = CurriculumVersion.query.filter(CurriculumVersion.label.contains(label)).one()
        assert version.entries == []
        with pytest.raises(svc.CurriculumWorkflowError):
            svc.extract_into_version(version, document, user=None)


def test_text_extraction_reads_structure_and_marks_everything_for_review(app, admin):
    text = (
        "DEPARTMENT OF DEMO STUDIES\n"
        "Ordinary Diploma in DEMO Technology\n"
        "NTA Level 6 Semester I\n"
        "DMO 06101 DEMO Circuit Theory 12\n"
        "DMO 06102 - DEMO Workshop Practice\n"
        "Semester II\n"
        "DMO 06201 DEMO Measurements 9.5\n"
    )
    candidates = svc.extract_candidates(text)
    assert [c["module_code"] for c in candidates] == ["DMO 06101", "DMO 06102", "DMO 06201"]
    assert candidates[0]["department"] == "Demo Studies"
    assert candidates[0]["programme"] == "Ordinary Diploma in DEMO Technology"
    assert (candidates[0]["nta_level"], candidates[0]["semester"]) == (6, 1)
    assert candidates[2]["semester"] == 2 and candidates[2]["credits"] == "9.5"
    assert candidates[1]["credits"] is None

    label = _unique()
    _upload(admin, text.encode(), "prospectus.txt", label=label)
    with app.app_context():
        version = CurriculumVersion.query.filter(CurriculumVersion.label.contains(label)).one()
        assert len(version.entries) == 3
        assert {e.origin for e in version.entries} == {"extracted"}
        assert {e.review_status for e in version.entries} == {"needs_review"}
        assert ProspectusChunk.query.filter_by(document_id=version.prospectus_document_id).count() >= 1


def test_csv_missing_columns_is_rejected():
    with pytest.raises(svc.CurriculumWorkflowError, match="missing required columns"):
        svc.parse_curriculum_csv("department,programme\nA,B\n")


# --- validation rules --------------------------------------------------------

@pytest.fixture
def draft(app, admin):
    """A blank draft version with one programme (levels 4-5, 2 semesters)."""
    with app.app_context():
        admin_user = User.query.filter_by(role="admin").first()
        version = svc.create_version(label=_unique(), academic_year_label="2033/2034", user=admin_user, is_demo=True)
        programme = DraftProgramme(version=version, department_name=f"{version.label} Dept",
                                   name=f"{version.label} Programme", levels_csv="4,5", semesters_per_level=2)
        db.session.add(programme)
        db.session.commit()
        yield version, programme
        db.session.rollback()


def _entry(version, programme, code, name="DEMO Module", level=4, semester=1, **extra):
    entry = CurriculumEntry(version=version, programme=programme, module_code=code, module_name=name,
                            nta_level=level, semester_number=semester, review_status="reviewed",
                            credits=extra.pop("credits", Decimal("10")), **extra)
    db.session.add(entry)
    return entry


def _messages(report):
    return " | ".join(issue["message"] for issue in report.errors)


def test_validation_detects_duplicate_modules(draft):
    version, programme = draft
    _entry(version, programme, "DUP-100", "DEMO One")
    _entry(version, programme, "DUP-100", "DEMO Two")
    _entry(version, programme, "DUP-101", "DEMO One")
    assert "duplicate module code" in _messages(svc.validate_version(version))
    assert "duplicate module name" in _messages(svc.validate_version(version))


def test_validation_detects_nonexistent_programme_and_invalid_semester(draft):
    version, programme = draft
    _entry(version, None, "ORPH-100")
    _entry(version, programme, "SEM-100", semester=3)
    _entry(version, programme, "LVL-100", level=7)
    messages = _messages(svc.validate_version(version))
    assert "assign the module to a programme" in messages
    assert "semester 3 is invalid" in messages
    assert "NTA level 7 is not defined" in messages


def test_validation_detects_missing_fields_and_broken_prerequisites(draft):
    version, programme = draft
    _entry(version, programme, None, name=None)
    _entry(version, programme, "PRE-100", prerequisite_codes="NOPE-999")
    _entry(version, programme, "CYC-1", "DEMO Cycle A", prerequisite_codes="CYC-2")
    _entry(version, programme, "CYC-2", "DEMO Cycle B", prerequisite_codes="CYC-1")
    _entry(version, programme, "SELF-1", "DEMO Self", prerequisite_codes="SELF-1")
    messages = _messages(svc.validate_version(version))
    assert "module code is required" in messages and "module name is required" in messages
    assert "NOPE-999 is not in this version" in messages
    assert "Prerequisites form a loop" in messages
    assert "cannot be its own prerequisite" in messages


def test_valid_version_passes_with_credit_warning_only(draft):
    version, programme = draft
    _entry(version, programme, "OK-100", "DEMO Base")
    _entry(version, programme, "OK-200", "DEMO Next", semester=2, prerequisite_codes="OK-100", credits=None)
    report = svc.validate_version(version)
    assert report.ok, _messages(report)
    assert any("credits not recorded" in w["message"] for w in report.warnings)


def test_validation_blocks_collision_with_live_modules(app, admin, seeded):
    with app.app_context():
        module = db.session.get(Module, seeded["module_id"])
        module.code = module.code or "LEGACY-CODE-1"
        programme = module.semester.nta_level.programme
        admin_user = User.query.filter_by(role="admin").first()
        version = svc.create_version(label=_unique(), academic_year_label=module.academic_year.label,
                                     user=admin_user, is_demo=True)
        draft = DraftProgramme(version=version, department_name=programme.department.name,
                               name=programme.name, levels_csv=str(module.semester.nta_level.level_number))
        _entry(version, draft, module.code, "DEMO Clash", level=module.semester.nta_level.level_number,
               semester=module.semester.semester_number)
        assert "already published" in _messages(svc.validate_version(version))
        db.session.rollback()


def test_creating_department_programme_level_and_semester_on_publish(app, admin):
    """Publishing creates each hierarchy level once and reuses it afterwards."""
    label = _unique()
    department, programme = f"{label} Dept", f"{label} Programme"
    _upload(admin, _csv(department, programme, [(5, 1, f"H{next(_ids):03d}", "DEMO Hierarchy", 8, "")]),
            "demo.csv", label=label)
    version_id = _version(app, label)
    _review_all(app, version_id)
    for step in ("validate", "approve", "publish"):
        admin.post(f"/admin/versions/{version_id}/{step}")
    with app.app_context():
        dept = Department.query.filter_by(name=department).one()
        prog = Programme.query.filter_by(department_id=dept.id, name=programme).one()
        level = NtaLevel.query.filter_by(programme_id=prog.id, level_number=5).one()
        assert dept.is_active and prog.is_active and level.is_active
        assert level.semesters[0].semester_number == 1 and level.semesters[0].modules
