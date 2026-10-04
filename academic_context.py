"""Student academic context: who supplied it, and what it contains.

Placement (programme, NTA level, semester, academic year) stays on ``User``
so every existing scoping rule keeps working. This module adds:

* provenance via ``StudentAcademicContext`` (self-registration, internal
  administrator, or SOMA);
* registered modules via ``StudentModuleRegistration``;
* a read-only SOMA sync that *matches* authoritative records against the
  published curriculum and never creates curriculum or writes to SOMA;
* ``describe_context`` - the single summary used by the dashboard, the AI
  assistant and recommendations.
"""

from __future__ import annotations

from flask import current_app

from extensions import db
from governance import record_audit
from integrations import soma
from models import (
    AcademicYear, Department, IntegrationSyncLog, Module, NtaLevel, Programme,
    Semester, StudentAcademicContext, StudentModuleRegistration, utcnow,
)


class AcademicContextError(Exception):
    """A correctable problem with supplied context; message is safe to show."""


def context_row(student, *, create=True):
    row = StudentAcademicContext.query.filter_by(student_id=student.id).first()
    if row is None and create:
        row = StudentAcademicContext(student_id=student.id, source="self_registration")
        db.session.add(row)
    return row


def registered_modules(student):
    """Modules the student is explicitly registered for (published only)."""
    rows = (StudentModuleRegistration.query
            .filter_by(student_id=student.id, status="registered")
            .join(Module).filter(Module.publication_status == "published", Module.is_active.is_(True))
            .all())
    return [row.module for row in rows]


def current_modules(student):
    """Registered modules when known, otherwise the student's semester modules."""
    registered = registered_modules(student)
    if registered:
        return registered, True
    modules = []
    if student.semester is not None:
        modules = [
            module for module in student.semester.modules
            if module.is_published and (
                not student.academic_year_id or not module.academic_year_id
                or module.academic_year_id == student.academic_year_id
            )
        ]
    return sorted(modules, key=lambda m: (m.module_type != "core", m.display_order)), False


def describe_context(student):
    """Plain dict describing the student's curriculum position."""
    row = context_row(student, create=False)
    modules, from_registration = current_modules(student)
    versions = sorted({m.curriculum_version.label for m in modules if m.curriculum_version},)
    legacy = any(m.provenance == "legacy_seed" for m in modules)
    missing = [label for label, value in (
        ("programme", student.programme), ("NTA level", student.nta_level),
        ("semester", student.semester), ("academic year", student.academic_year),
    ) if value is None]
    return {
        "department": student.department,
        "programme": student.programme,
        "level": student.nta_level,
        "semester": student.semester,
        "academic_year": student.academic_year,
        "modules": modules,
        "modules_from_registration": from_registration,
        "curriculum_versions": versions,
        "includes_legacy_modules": legacy,
        "source": row.source if row else "self_registration",
        "source_label": row.source_label if row else "Self-registered at sign-up",
        "last_synced_at": row.last_synced_at if row else None,
        "missing": missing,
        "is_complete": not missing,
    }


def set_internal_context(student, *, programme_id, level_id, semester_id, academic_year_id,
                         module_ids, actor):
    """Controlled internal mechanism used until an authorised SOMA API exists."""
    programme = db.session.get(Programme, programme_id) if programme_id else None
    level = db.session.get(NtaLevel, level_id) if level_id else None
    semester = db.session.get(Semester, semester_id) if semester_id else None
    year = db.session.get(AcademicYear, academic_year_id) if academic_year_id else None
    if not all((programme, level, semester, year)):
        raise AcademicContextError("Choose a programme, NTA level, semester and academic year.")
    if level.programme_id != programme.id or semester.nta_level_id != level.id:
        raise AcademicContextError("The NTA level and semester must belong to the selected programme.")
    if year.department_id != programme.department_id:
        raise AcademicContextError("The academic year belongs to a different department.")

    modules = []
    for module_id in module_ids or []:
        module = db.session.get(Module, module_id)
        if module is None or not module.is_published:
            raise AcademicContextError("Registered modules must be published modules.")
        if module.semester.nta_level.programme_id != programme.id:
            raise AcademicContextError(f"{module.name} is not part of {programme.name}.")
        modules.append(module)

    before = _placement_snapshot(student)
    student.department_id = programme.department_id
    student.programme_id = programme.id
    student.nta_level_id = level.id
    student.semester_id = semester.id
    student.academic_year_id = year.id
    _replace_registrations(student, modules, source="internal_admin", only_source=None)
    row = context_row(student)
    row.source = "internal_admin"
    row.updated_by_id = actor.id
    row.sync_status = None
    row.sync_message = None
    record_audit(
        "student_context.updated", "User", target_id=student.id, target_label=student.full_name,
        actor=actor, department_id=programme.department_id,
        details={"before": before, "after": _placement_snapshot(student),
                 "registered_module_ids": [m.id for m in modules]},
    )
    return row


def sync_from_soma(student, *, triggered_by=None, adapter=None):
    """Pull read-only context from SOMA and match it to the published curriculum.

    Returns a result dict. Local data is only changed when SOMA returns a
    record that fully matches the published curriculum; any failure leaves
    the existing context untouched and is logged.
    """
    adapter = adapter or soma.get_adapter(current_app.config)
    identifier = student.registration_number or ""
    row = context_row(student)
    result = {"ok": False, "status": "error", "message": "", "unmatched_modules": []}
    try:
        if not identifier:
            raise AcademicContextError("This student has no registration number to look up in SOMA.")
        record = adapter.get_student_record(identifier)
        placement, problem = _match_placement(record)
        if problem:
            result.update(status="unmatched", message=problem)
        else:
            programme, level, semester, year = placement
            modules, unmatched = _match_modules(record.registered_module_codes, programme, year)
            before = _placement_snapshot(student)
            student.department_id = programme.department_id
            student.programme_id = programme.id
            student.nta_level_id = level.id
            student.semester_id = semester.id
            student.academic_year_id = year.id
            _replace_registrations(student, modules, source="soma", only_source="soma")
            row.source = "soma"
            row.external_student_id = record.student_id
            row.last_synced_at = utcnow()
            message = "Academic context synchronised from SOMA."
            if unmatched:
                message += f" {len(unmatched)} registered module code(s) are not in the published curriculum."
            result.update(ok=True, status="partial" if unmatched else "success",
                          message=message, unmatched_modules=unmatched)
            record_audit("student_context.soma_synced", "User", target_id=student.id,
                         target_label=student.full_name, actor=triggered_by,
                         department_id=programme.department_id,
                         details={"before": before, "after": _placement_snapshot(student),
                                  "unmatched_modules": unmatched})
    except soma.SomaError as error:
        current_app.logger.warning("SOMA sync failed for user %s: %s", student.id, error.detail or error)
        result.update(status=error.status, message=error.user_message)
    except AcademicContextError as error:
        result.update(status="invalid", message=str(error))
    row.sync_status = result["status"]
    row.sync_message = result["message"][:500]
    db.session.add(IntegrationSyncLog(
        provider="soma", operation="student_record", status=result["status"],
        student_id=student.id, message=result["message"][:500],
        triggered_by_id=getattr(triggered_by, "id", None),
    ))
    return result


def _match_placement(record):
    if not record.programme or record.nta_level is None or record.semester is None or not record.academic_year:
        return None, "SOMA record is missing programme, level, semester or academic year."
    query = Programme.query.join(Department).filter(db.func.lower(Programme.name) == record.programme.lower())
    if record.department:
        query = query.filter(db.func.lower(Department.name) == record.department.lower())
    programme = query.first()
    if programme is None:
        return None, f"Programme “{record.programme}” is not in the published curriculum."
    level = NtaLevel.query.filter_by(programme_id=programme.id, level_number=record.nta_level).first()
    semester = Semester.query.filter_by(nta_level_id=level.id, semester_number=record.semester).first() if level else None
    if level is None or semester is None:
        return None, "SOMA level/semester does not exist in the published curriculum."
    year = AcademicYear.query.filter_by(department_id=programme.department_id, label=record.academic_year).first()
    if year is None:
        return None, f"Academic year {record.academic_year} has no published curriculum."
    return (programme, level, semester, year), None


def _match_modules(codes, programme, year):
    modules, unmatched = [], []
    for code in codes or ():
        module = (Module.query.join(Semester).join(NtaLevel)
                  .filter(NtaLevel.programme_id == programme.id,
                          Module.academic_year_id == year.id,
                          Module.publication_status == "published",
                          db.func.upper(Module.code) == str(code).strip().upper())
                  .first())
        if module is None:
            unmatched.append(str(code))
        else:
            modules.append(module)
    return modules, unmatched


def _replace_registrations(student, modules, *, source, only_source):
    query = StudentModuleRegistration.query.filter_by(student_id=student.id)
    if only_source:
        query = query.filter_by(source=only_source)
    wanted = {module.id for module in modules}
    for row in query.all():
        if row.module_id not in wanted:
            db.session.delete(row)
    db.session.flush()
    existing = {row.module_id: row for row in StudentModuleRegistration.query.filter_by(student_id=student.id)}
    for module in modules:
        row = existing.get(module.id)
        if row is None:
            db.session.add(StudentModuleRegistration(student_id=student.id, module_id=module.id,
                                                     source=source, status="registered"))
        else:
            row.source = source
            row.status = "registered"


def _placement_snapshot(student):
    return {
        "programme_id": student.programme_id, "nta_level_id": student.nta_level_id,
        "semester_id": student.semester_id, "academic_year_id": student.academic_year_id,
    }
