"""Prospectus upload and automatic publishing.

A Head of Department uploads the prospectus and the system does the rest:

    UPLOAD -> READ -> CHECK -> SWAP (one transaction) -> LIVE

* The uploaded file is stored unchanged for audit (ProspectusDocument).
* Modules read from it are staged on a CurriculumVersion so a stopped read
  can be inspected. Any check failure publishes nothing.
* A clean read replaces every live module in every department. Old modules
  are archived, never deleted. Resources, lecturer assignments, topics and
  student registrations follow the module code to the new module.
* Every live change is journalled so the latest publish can be undone.

Nothing here invents curriculum data: every value comes from the uploaded file.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from extensions import db
from file_processing import chunk_text, extract_text
from governance import record_audit
from models import (
    AcademicYear, CurriculumEntry, CurriculumVersion, Department, DraftProgramme,
    LecturerAssignment, Module, ModulePrerequisite, NtaLevel, Programme, ProspectusChunk,
    ProspectusDocument, Resource, Semester, StudentModuleRegistration, Topic, User, utcnow,
)
from utils import build_stored_filename, looks_like_claimed_type, slugify

PROSPECTUS_EXTENSIONS = {"pdf", "docx", "txt", "md", "csv"}
PROSPECTUS_MIME_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "txt": "text/plain; charset=utf-8",
    "md": "text/markdown; charset=utf-8",
    "csv": "text/csv; charset=utf-8",
}
MODULE_TYPES = {"core", "general_studies"}
MODULE_CODE_PATTERN = re.compile(r"[A-Z0-9][A-Z0-9 ./-]{1,29}")
ACADEMIC_YEAR_PATTERN = re.compile(r"(\d{4})/(\d{4})")
MAX_LEVEL = 10
MAX_SEMESTERS = 3

CSV_COLUMNS = (
    "department", "programme", "nta_level", "year_label", "semester",
    "module_code", "module_name", "module_type", "credits", "prerequisites", "description",
)
CSV_REQUIRED_COLUMNS = {"department", "programme", "nta_level", "semester", "module_code", "module_name"}


class CurriculumWorkflowError(Exception):
    """A user-correctable workflow problem; the message is safe to display."""


# ---------------------------------------------------------------------------
# Upload and extraction
# ---------------------------------------------------------------------------

def store_prospectus(file_storage, *, title, academic_year_label, user):
    """Store the original prospectus and extract its text.

    Raises CurriculumWorkflowError for invalid files. Extraction failures do
    not raise: the document is kept with ``extraction_status='failed'`` and
    the publish stops with that reason.
    """
    from storage_backend import StorageError, delete_resource_file, stage_uploaded_file

    if file_storage is None or not file_storage.filename:
        raise CurriculumWorkflowError("Choose a prospectus file to upload.")
    filename = file_storage.filename
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in PROSPECTUS_EXTENSIONS:
        raise CurriculumWorkflowError("Upload the prospectus as PDF, DOCX, TXT, MD or CSV.")
    if not looks_like_claimed_type(file_storage, ext):
        raise CurriculumWorkflowError("The file contents do not match its extension. Check the document and try again.")
    title = " ".join((title or "").split())[:250]
    if len(title) < 3:
        raise CurriculumWorkflowError("Give the prospectus a clear title.")
    academic_year_label = (academic_year_label or "").strip()
    if not _valid_academic_year(academic_year_label):
        raise CurriculumWorkflowError("Use an academic year in the format 2026/2027.")

    stored_filename = build_stored_filename(filename)
    try:
        path = stage_uploaded_file(file_storage, stored_filename, PROSPECTUS_MIME_TYPES[ext])
    except (StorageError, OSError) as error:
        raise CurriculumWorkflowError("The prospectus could not be stored. Please try again.") from error

    try:
        with open(path, "rb") as handle:
            raw = handle.read()
        if not raw:
            delete_resource_file(stored_filename)
            raise CurriculumWorkflowError("The uploaded file is empty.")
        document = ProspectusDocument(
            title=title,
            academic_year_label=academic_year_label,
            stored_filename=stored_filename,
            original_filename=os.path.basename(filename)[:300],
            mime_type=PROSPECTUS_MIME_TYPES[ext],
            file_size_bytes=len(raw),
            sha256=hashlib.sha256(raw).hexdigest(),
            uploaded_by_id=getattr(user, "id", None),
        )
        text = raw.decode("utf-8", errors="ignore") if ext == "csv" else extract_text(path, ext)
        if text and text.strip():
            document.extracted_text = text
            document.extraction_status = "success"
            document.extraction_message = None
        else:
            document.extraction_status = "failed"
            document.extraction_message = (
                "No readable text was found. The file may be scanned or protected; upload a text PDF or the CSV template."
            )
        db.session.add(document)
        db.session.flush()
        if document.extraction_status == "success" and ext != "csv":
            for index, chunk in enumerate(chunk_text(text)):
                db.session.add(ProspectusChunk(document_id=document.id, chunk_index=index, content=chunk))
        record_audit(
            "prospectus.uploaded", "ProspectusDocument", target_id=document.id,
            target_label=document.title, actor=user,
            details={"filename": document.original_filename, "sha256": document.sha256,
                     "extraction_status": document.extraction_status},
        )
        return document
    except CurriculumWorkflowError:
        raise
    except Exception:
        db.session.rollback()
        delete_resource_file(stored_filename)
        raise


_LEVEL_RE = re.compile(r"\bNTA\s*level\s*(\d{1,2})\b", re.I)
_SEMESTER_RE = re.compile(r"\bsemester\s*(\d|iii|ii|i|one|two|three)\b", re.I)
_DEPARTMENT_RE = re.compile(r"^\s*department\s+of\s+(.{3,120}?)\s*$", re.I)
_PROGRAMME_RE = re.compile(
    r"^\s*((?:ordinary\s+|higher\s+|basic\s+|technician\s+)?(?:diploma|bachelor|certificate|master)\b.{3,140}?)\s*$",
    re.I,
)
_MODULE_RE = re.compile(
    r"^\s*([A-Z]{2,6}[ -]?\d{3,6}[A-Z]?)\s*[-–:|.]?\s+([A-Za-z(][^|\t]{2,180}?)"
    r"(?:\s+[|\t]?\s*(\d{1,2}(?:\.\d{1,2})?))?\s*$"
)
_SEMESTER_WORDS = {"i": 1, "one": 1, "1": 1, "ii": 2, "two": 2, "2": 2, "iii": 3, "three": 3, "3": 3}


def extract_candidates(text):
    """Heuristically find module lines in prospectus text.

    Returns a list of dicts. Every value is a *candidate* that must be
    reviewed: the parser only reads what is printed and never fills gaps.
    """
    department = programme = None
    level = semester = None
    candidates = []
    for line_number, raw_line in enumerate((text or "").splitlines(), start=1):
        line = " ".join(raw_line.replace(" ", " ").split())
        if not line:
            continue
        match = _DEPARTMENT_RE.match(line)
        if match:
            department = match.group(1).strip().title() if match.group(1).isupper() else match.group(1).strip()
            continue
        match = _PROGRAMME_RE.match(line)
        if match and not _MODULE_RE.match(line):
            programme = match.group(1).strip()
            level = semester = None
        level_match = _LEVEL_RE.search(line)
        if level_match:
            level = int(level_match.group(1))
        semester_match = _SEMESTER_RE.search(line)
        if semester_match:
            semester = _SEMESTER_WORDS.get(semester_match.group(1).lower())
        if level_match or semester_match or match:
            continue
        module_match = _MODULE_RE.match(line)
        if not module_match:
            continue
        name = module_match.group(2).strip(" -–:|.")
        if not re.search(r"[A-Za-z]{3}", name):
            continue
        candidates.append({
            "department": department,
            "programme": programme,
            "nta_level": level,
            "semester": semester,
            "module_code": _normalise_code(module_match.group(1)),
            "module_name": name[:200],
            "credits": module_match.group(3),
            "source_reference": f"Line {line_number}",
        })
    return candidates


def parse_curriculum_csv(text):
    """Parse the documented CSV import template.

    Returns (rows, problems). ``problems`` lists unreadable lines; readable
    rows are still staged for review so one bad line does not lose the file.
    """
    reader = csv.DictReader(io.StringIO((text or "").lstrip("﻿")))
    headers = {(name or "").strip().lower() for name in (reader.fieldnames or [])}
    missing = sorted(CSV_REQUIRED_COLUMNS - headers)
    if missing:
        raise CurriculumWorkflowError(
            "The CSV is missing required columns: " + ", ".join(missing)
            + ". Use the import template from the documentation."
        )
    rows, problems = [], []
    for line_number, raw in enumerate(reader, start=2):
        row = {(key or "").strip().lower(): (value or "").strip() for key, value in raw.items()}
        if not any(row.values()):
            continue
        level = _to_int(row.get("nta_level"))
        semester = _to_int(row.get("semester"))
        if row.get("nta_level") and level is None:
            problems.append(f"Line {line_number}: NTA level must be a number.")
        if row.get("semester") and semester is None:
            problems.append(f"Line {line_number}: semester must be a number.")
        rows.append({
            "department": row.get("department") or None,
            "programme": row.get("programme") or None,
            "nta_level": level,
            "year_label": row.get("year_label") or None,
            "semester": semester,
            "module_code": _normalise_code(row.get("module_code")),
            "module_name": row.get("module_name") or None,
            "module_type": row.get("module_type") or "core",
            "credits": row.get("credits") or None,
            "prerequisites": row.get("prerequisites") or None,
            "description": row.get("description") or None,
            "source_reference": f"CSV line {line_number}",
        })
    return rows, problems


def stage_rows(version, rows, *, origin, user):
    """Create staged programmes and entries from parsed rows."""
    programmes = {(p.department_name.lower(), p.name.lower()): p for p in version.programmes}
    created = 0
    for row in rows:
        programme = None
        dept_name = " ".join((row.get("department") or "").split())[:150]
        prog_name = " ".join((row.get("programme") or "").split())[:150]
        level = row.get("nta_level")
        if dept_name and prog_name:
            key = (dept_name.lower(), prog_name.lower())
            programme = programmes.get(key)
            if programme is None:
                programme = DraftProgramme(
                    version=version, department_name=dept_name, name=prog_name,
                    levels_csv="", semesters_per_level=2,
                )
                db.session.add(programme)
                programmes[key] = programme
            if isinstance(level, int) and level not in programme.levels:
                programme.levels_csv = ",".join(str(n) for n in sorted(set(programme.levels + [level])))
            if isinstance(level, int) and row.get("year_label"):
                labels = programme.year_label_map
                labels.setdefault(level, row["year_label"][:40])
                programme.year_labels = ";".join(f"{k}={v}" for k, v in sorted(labels.items()))
            semester = row.get("semester")
            if isinstance(semester, int) and semester > programme.semesters_per_level and semester <= MAX_SEMESTERS:
                programme.semesters_per_level = semester
        module_type = (row.get("module_type") or "core").strip().lower().replace(" ", "_")
        if module_type in {"general", "general_study", "gs"}:
            module_type = "general_studies"
        db.session.add(CurriculumEntry(
            version=version,
            programme=programme,
            nta_level=level if isinstance(level, int) else None,
            semester_number=row.get("semester") if isinstance(row.get("semester"), int) else None,
            module_code=row.get("module_code"),
            module_name=(row.get("module_name") or None) and row["module_name"][:200],
            module_type=module_type if module_type in MODULE_TYPES else "core",
            credits=_to_decimal(row.get("credits")),
            description=row.get("description"),
            prerequisite_codes=_normalise_codes(row.get("prerequisites")),
            origin=origin,
            source_reference=(row.get("source_reference") or "")[:200] or None,
            review_status="needs_review",
            updated_by_id=getattr(user, "id", None),
        ))
        created += 1
    return created


def read_rows(document):
    """Rows and line problems read from a stored prospectus."""
    if document.original_filename.lower().endswith(".csv"):
        rows, problems = parse_curriculum_csv(document.extracted_text)
        return rows, problems, "csv"
    return extract_candidates(document.extracted_text), [], "extracted"


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

@dataclass
class ValidationReport:
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    @property
    def ok(self):
        return not self.errors

    def error(self, message, entry=None):
        self.errors.append(_issue(message, entry))

    def warn(self, message, entry=None):
        self.warnings.append(_issue(message, entry))

    def to_json(self):
        return json.dumps({"errors": self.errors, "warnings": self.warnings})


def _issue(message, entry):
    item = {"message": message}
    if entry is not None and entry.source_reference:
        item["source"] = entry.source_reference
    return item


def validate_version(version):
    """Check staged modules for problems. Never modifies data."""
    report = ValidationReport()
    if not _valid_academic_year(version.academic_year_label):
        report.error("The academic year must use the format 2026/2027.")

    programme_ids = {p.id for p in version.programmes}
    for programme in version.programmes:
        if not programme.levels:
            report.error(f"{programme.name}: no NTA level was found.")
        elif any(level < 1 or level > MAX_LEVEL for level in programme.levels):
            report.error(f"{programme.name}: NTA levels must be between 1 and {MAX_LEVEL}.")

    entries = list(version.entries)
    if not entries:
        report.error("No modules were found in the file.")

    seen_codes, seen_names, codes_in_version = set(), set(), set()
    for entry in entries:
        label = entry.module_code or entry.module_name or "A module line"
        programme = entry.programme
        if programme is None or entry.programme_id not in programme_ids:
            report.error(f"{label}: department or programme is missing.", entry)
        if not entry.module_name or not 3 <= len(entry.module_name.strip()) <= 200:
            report.error(f"{label}: module name is required.", entry)
        if not entry.module_code:
            report.error(f"{label}: module code is required.", entry)
        elif not MODULE_CODE_PATTERN.fullmatch(entry.module_code):
            report.error(f"{label}: module code may only contain letters, numbers, spaces, dots, slashes or hyphens.",
                         entry)
        if entry.nta_level is None:
            report.error(f"{label}: NTA level is required.", entry)
        elif programme is not None and entry.nta_level not in programme.levels:
            report.error(f"{label}: NTA level {entry.nta_level} is not defined for {programme.name}.", entry)
        if entry.semester_number is None:
            report.error(f"{label}: semester is required.", entry)
        elif not 1 <= entry.semester_number <= MAX_SEMESTERS:
            report.error(f"{label}: semester {entry.semester_number} is invalid.", entry)
        if entry.credits is not None and not (Decimal("0") <= entry.credits <= Decimal("100")):
            report.error(f"{label}: credits must be between 0 and 100.", entry)
        if entry.credits is None:
            report.warn(f"{label}: credits not recorded.", entry)

        if entry.programme_id and entry.module_code:
            key = (entry.programme_id, entry.module_code.upper())
            if key in seen_codes:
                report.error(f"{label}: duplicate module code in {programme.name}.", entry)
            seen_codes.add(key)
            codes_in_version.add(entry.module_code.upper())
        if entry.programme_id and entry.module_name:
            key = (entry.programme_id, entry.nta_level, entry.semester_number, entry.module_name.strip().lower())
            if key in seen_names:
                report.error(f"{label}: duplicate module name in the same programme, level and semester.", entry)
            seen_names.add(key)

    graph = {}
    for entry in entries:
        if not entry.module_code:
            continue
        node = entry.module_code.upper()
        graph.setdefault(node, set())
        for code in entry.prerequisite_list:
            if code == node:
                report.error(f"{entry.module_code}: a module cannot be its own prerequisite.", entry)
            elif code in codes_in_version:
                graph[node].add(code)
            else:
                report.error(f"{entry.module_code}: prerequisite {code} is not in this prospectus.", entry)
    cycle = _find_cycle(graph)
    if cycle:
        report.error("Prerequisites form a loop: " + " → ".join(cycle) + ".")
    return report


def _find_cycle(graph):
    state = {}

    def visit(node, path):
        state[node] = "visiting"
        for nxt in sorted(graph.get(node, ())):
            if state.get(nxt) == "visiting":
                return path + [node, nxt]
            if nxt not in state:
                found = visit(nxt, path + [node])
                if found:
                    return found
        state[node] = "done"
        return None

    for node in sorted(graph):
        if node not in state:
            found = visit(node, [])
            if found:
                start = found.index(found[-1])
                return found[start:]
    return None


# ---------------------------------------------------------------------------
# Publish and undo
# ---------------------------------------------------------------------------

LARGE_CHANGE_SHARE = 0.5
_MODELS = {
    model.__tablename__: model for model in (
        AcademicYear, CurriculumVersion, Department, LecturerAssignment, Module, NtaLevel,
        Programme, Resource, Semester, StudentModuleRegistration, Topic, User,
    )
}
_WATERMARKED = (Resource, Topic, LecturerAssignment)


class _Journal:
    """Records every live change so the publish can be reversed exactly."""

    def __init__(self):
        self.changes, self.created = [], []

    def set(self, row, attr, value):
        old = getattr(row, attr)
        if old != value:
            self.changes.append([row.__tablename__, row.id, attr, old])
            setattr(row, attr, value)

    def add(self, row):
        db.session.add(row)
        db.session.flush()
        self.created.append([row.__tablename__, row.id])
        return row


def live_version():
    return (CurriculumVersion.query.filter_by(status="published")
            .order_by(CurriculumVersion.published_at.desc(), CurriculumVersion.id.desc()).first())


def publish_upload(file_storage, *, title, academic_year_label, user, allow_large_change=False):
    """Store, read, check and publish a prospectus. Returns its version.

    ``version.status`` is ``published`` on success or ``failed`` when a check
    stopped it; a failed version leaves the live curriculum untouched.
    """
    document = store_prospectus(file_storage, title=title, academic_year_label=academic_year_label, user=user)
    version = CurriculumVersion(
        label=f"{document.title} · {document.academic_year_label} · #{document.id}"[:150],
        academic_year_label=document.academic_year_label, status="failed", source="prospectus_import",
        prospectus_document_id=document.id, created_by_id=getattr(user, "id", None),
    )
    db.session.add(version)
    db.session.flush()

    report = ValidationReport()
    current = live_version()
    if current and current.prospectus_document and current.prospectus_document.sha256 == document.sha256:
        report.error("This prospectus is already live.")
    elif document.extraction_status != "success":
        report.error(document.extraction_message or "No readable text was found.")
    else:
        rows, problems, origin = read_rows(document)
        if rows:
            stage_rows(version, rows, origin=origin, user=user)
            db.session.flush()
            db.session.refresh(version)
            report = validate_version(version)
        else:
            report.error("No modules were found in the file. Upload a text PDF or the CSV template.")
        for problem in problems:
            report.error(problem)

    if report.ok and not allow_large_change:
        old = _live_modules()
        new_codes = {e.module_code.upper() for e in version.entries}
        retired = sum(1 for m in old if _normalise_code(m.code) not in new_codes)
        if old and retired / len(old) > LARGE_CHANGE_SHARE:
            report.error(f"This prospectus would retire {retired} of {len(old)} live modules. "
                         "Tick “Confirm a large change” if that is expected.")

    version.last_validation_json = report.to_json()
    version.validated_at = utcnow()
    if not report.ok:
        record_audit("prospectus.stopped", "CurriculumVersion", target_id=version.id,
                     target_label=version.label, actor=user, details={"errors": len(report.errors)})
        return version
    _swap(version, user)
    return version


def _live_modules():
    return Module.query.filter(Module.publication_status != "archived").all()


def _swap(version, user):
    """Replace every live module with the version's modules, in one transaction."""
    journal = _Journal()
    watermarks = {m.__tablename__: db.session.query(db.func.max(m.id)).scalar() or 0 for m in _WATERMARKED}
    old_modules = _live_modules()
    old_paths = {m.id: (m.semester.nta_level.programme, m.semester.nta_level.programme.department)
                 for m in old_modules}
    for module in old_modules:
        journal.set(module, "publication_status", "archived")
        journal.set(module, "is_active", False)
    for previous in CurriculumVersion.query.filter_by(status="published").all():
        journal.set(previous, "status", "archived")

    touched = {name: set() for name in ("departments", "programmes", "nta_levels", "semesters")}
    years, created_by_code = {}, {}
    for entry in version.entries:
        draft = entry.programme
        department = _department(draft.department_name, journal)
        programme = _programme(department, draft, journal)
        level = _level(programme, entry.nta_level, draft.year_label_map.get(entry.nta_level), journal)
        semester = _semester(level, entry.semester_number, journal)
        for row in (department, programme, level, semester):
            touched[row.__tablename__].add(row.id)
        if department.id not in years:
            years[department.id] = _year(department, version.academic_year_label, user, journal)
        order = (db.session.query(db.func.max(Module.display_order))
                 .filter_by(semester_id=semester.id).scalar() or 0) + 1
        module = journal.add(Module(
            semester_id=semester.id, academic_year_id=years[department.id].id, name=entry.module_name.strip(),
            code=entry.module_code, module_type=entry.module_type, credits=entry.credits,
            description=entry.description, publication_status="published", is_active=True,
            display_order=order, created_by_id=getattr(user, "id", None), provenance="prospectus",
            curriculum_version_id=version.id,
        ))
        entry.live_module_id = module.id
        created_by_code.setdefault(entry.module_code.upper(), []).append((programme, module))

    for entry in version.entries:
        for code in entry.prerequisite_list:
            prerequisite = _pick(entry.programme.name, created_by_code.get(code, []), fallback=True)
            if prerequisite is not None and prerequisite.id != entry.live_module_id:
                db.session.add(ModulePrerequisite(module_id=entry.live_module_id,
                                                  prerequisite_module_id=prerequisite.id))

    for department_id, year in years.items():
        for other in AcademicYear.query.filter_by(department_id=department_id).all():
            journal.set(other, "is_current", other.id == year.id)
        journal.set(year, "status", "active")

    outcomes = [_reallocate(old, old_paths[old.id], created_by_code, journal) for old in old_modules]
    old_codes = {_normalise_code(m.code) for m in old_modules}
    for code, matches in created_by_code.items():
        if code not in old_codes:
            for programme, module in matches:
                outcomes.append({"code": module.code, "name": module.name, "outcome": "new",
                                 "to": programme.department.name, "resources": 0, "lecturers": 0})
    flagged = _replace_students(created_by_code, years, journal)

    hidden = []
    for model in (Department, Programme, NtaLevel, Semester):
        for row in model.query.filter_by(is_active=True).all():
            if row.id not in touched[model.__tablename__]:
                journal.set(row, "is_active", False)
                if model is Department:
                    hidden.append(row.name)

    counts = {key: sum(1 for o in outcomes if o["outcome"] == key)
              for key in ("carried", "moved", "new", "retired", "unplaced")}
    version.allocation_report_json = json.dumps({
        "counts": counts, "modules": outcomes, "flagged_students": flagged, "hidden_departments": hidden,
    })
    version.undo_journal_json = json.dumps(
        {"changes": journal.changes, "created": journal.created, "watermarks": watermarks})
    version.status = "published"
    version.published_by_id = getattr(user, "id", None)
    version.published_at = utcnow()
    record_audit("prospectus.published", "CurriculumVersion", target_id=version.id, target_label=version.label,
                 actor=user, details={**counts, "flagged_students": len(flagged)})


def _pick(programme_name, matches, fallback=False):
    """The new module an old one hands over to: same programme, else the only match."""
    same = [module for programme, module in matches if programme.name.lower() == programme_name.lower()]
    if same:
        return same[0]
    if len(matches) == 1 or (fallback and matches):
        return matches[0][1]
    return None


def _reallocate(old, path, created_by_code, journal):
    programme, department = path
    matches = created_by_code.get(_normalise_code(old.code), []) if old.code else []
    target = _pick(programme.name, matches)
    if not matches:
        outcome = "retired"
    elif target is None:
        outcome = "unplaced"
    else:
        target_department = target.semester.nta_level.programme.department
        outcome = "carried" if target_department.id == department.id else "moved"

    lecturers = 0
    for assignment in LecturerAssignment.query.filter_by(module_id=old.id).all():
        for _, module in matches:
            if LecturerAssignment.query.filter_by(lecturer_id=assignment.lecturer_id, module_id=module.id).first():
                continue
            if module is target:
                journal.set(assignment, "module_id", module.id)
            else:
                journal.add(LecturerAssignment(
                    lecturer_id=assignment.lecturer_id, module_id=module.id, status=assignment.status,
                    reviewed_by_id=assignment.reviewed_by_id, reviewed_at=assignment.reviewed_at))
            db.session.flush()
            lecturers += 1

    resources = 0
    if target is not None:
        for resource in Resource.query.filter_by(module_id=old.id).all():
            journal.set(resource, "module_id", target.id)
            resources += 1
        for topic in Topic.query.filter_by(module_id=old.id).all():
            journal.set(topic, "module_id", target.id)
        for registration in StudentModuleRegistration.query.filter_by(module_id=old.id).all():
            if not StudentModuleRegistration.query.filter_by(student_id=registration.student_id,
                                                             module_id=target.id).first():
                journal.set(registration, "module_id", target.id)
        db.session.flush()
    return {
        "code": old.code, "name": old.name, "outcome": outcome, "from": department.name,
        "to": target.semester.nta_level.programme.department.name if target is not None else None,
        "resources": resources if target is not None else Resource.query.filter_by(module_id=old.id).count(),
        "lecturers": lecturers,
    }


def _replace_students(created_by_code, years, journal):
    """Point each student at the matching programme, level and semester. Returns those left unmatched."""
    programmes = {}
    for matches in created_by_code.values():
        for programme, _ in matches:
            programmes[programme.id] = programme
    flagged = []
    students = User.query.filter(User.role == "student", User.programme_id.isnot(None)).all()
    for student in students:
        current = student.programme
        if current.id in programmes:
            target = current
        else:
            same_name = [p for p in programmes.values() if p.name.lower() == current.name.lower()]
            target = same_name[0] if len(same_name) == 1 else None
        level = semester = None
        if target is not None and student.nta_level is not None:
            level = NtaLevel.query.filter_by(programme_id=target.id,
                                             level_number=student.nta_level.level_number).first()
        if level is not None and student.semester is not None:
            semester = Semester.query.filter_by(nta_level_id=level.id,
                                                semester_number=student.semester.semester_number).first()
        if semester is None:
            flagged.append({"id": student.id, "name": student.full_name})
            continue
        journal.set(student, "department_id", target.department_id)
        journal.set(student, "programme_id", target.id)
        journal.set(student, "nta_level_id", level.id)
        journal.set(student, "semester_id", semester.id)
        journal.set(student, "academic_year_id", years[target.department_id].id)
    return flagged


def undo_last_publish(*, user):
    """Reverse the live prospectus and bring back the one it replaced."""
    version = live_version()
    if version is None or not version.undo_journal_json:
        raise CurriculumWorkflowError("There is no prospectus publish to undo.")
    journal = json.loads(version.undo_journal_json)
    created = {}
    for table, row_id in journal["created"]:
        created.setdefault(table, []).append(row_id)
    for model in _WATERMARKED:
        mark = journal["watermarks"].get(model.__tablename__, 0)
        if model.query.filter(model.id > mark, model.module_id.in_(created.get("modules", [])),
                              model.id.notin_(created.get(model.__tablename__, []))).first():
            raise CurriculumWorkflowError(
                "Lecturers have added content to the new modules since this prospectus went live, "
                "so it can no longer be undone.")
    for table, row_id, attr, old in reversed(journal["changes"]):
        row = db.session.get(_MODELS[table], row_id)
        if row is not None:
            setattr(row, attr, old)
    for table, row_id in journal["created"]:
        row = db.session.get(_MODELS[table], row_id)
        if row is None:
            continue
        if table == "modules":
            row.publication_status, row.is_active = "archived", False
        elif table == "lecturer_assignments":
            db.session.delete(row)
        elif table == "academic_years":
            row.is_current = False
        else:
            row.is_active = False
    version.status = "undone"
    version.undone_by_id = getattr(user, "id", None)
    version.undone_at = utcnow()
    record_audit("prospectus.undone", "CurriculumVersion", target_id=version.id,
                 target_label=version.label, actor=user)
    return version


# ---------------------------------------------------------------------------
# Live-tree helpers (every change goes through the journal)
# ---------------------------------------------------------------------------

def _department(name, journal):
    department = Department.query.filter(db.func.lower(Department.name) == name.lower()).first()
    if department is None:
        return journal.add(Department(
            name=name, slug=_unique_slug(Department, name), is_active=True,
            display_order=(db.session.query(db.func.max(Department.display_order)).scalar() or 0) + 1))
    journal.set(department, "is_active", True)
    return department


def _programme(department, draft, journal):
    programme = Programme.query.filter(Programme.department_id == department.id,
                                       db.func.lower(Programme.name) == draft.name.lower()).first()
    if programme is None:
        base = slugify(draft.name)
        slug, n = base, 2
        while Programme.query.filter_by(department_id=department.id, slug=slug).first():
            slug, n = f"{base}-{n}", n + 1
        return journal.add(Programme(department_id=department.id, name=draft.name, slug=slug,
                                     description=draft.description, is_active=True,
                                     display_order=len(department.programmes) + 1))
    journal.set(programme, "is_active", True)
    return programme


def _level(programme, level_number, year_label, journal):
    level = NtaLevel.query.filter_by(programme_id=programme.id, level_number=level_number).first()
    if level is None:
        return journal.add(NtaLevel(programme_id=programme.id, level_number=level_number,
                                    year_label=year_label, is_active=True))
    journal.set(level, "is_active", True)
    if year_label:
        journal.set(level, "year_label", year_label)
    return level


def _semester(level, semester_number, journal):
    semester = Semester.query.filter_by(nta_level_id=level.id, semester_number=semester_number).first()
    if semester is None:
        return journal.add(Semester(nta_level_id=level.id, semester_number=semester_number, is_active=True))
    journal.set(semester, "is_active", True)
    return semester


def _year(department, label, user, journal):
    year = AcademicYear.query.filter_by(department_id=department.id, label=label).first()
    if year is None:
        return journal.add(AcademicYear(department_id=department.id, label=label, status="active",
                                        is_current=False, created_by_id=getattr(user, "id", None)))
    return year


def _unique_slug(model, name):
    base = slugify(name)
    slug, n = base, 2
    while model.query.filter_by(slug=slug).first():
        slug, n = f"{base}-{n}", n + 1
    return slug


# ---------------------------------------------------------------------------
# Small parsers
# ---------------------------------------------------------------------------

def _valid_academic_year(label):
    match = ACADEMIC_YEAR_PATTERN.fullmatch(label or "")
    return bool(match and int(match.group(2)) == int(match.group(1)) + 1)


def _normalise_code(value):
    value = " ".join((value or "").upper().split())
    return value[:50] or None


def _normalise_codes(value):
    codes = [_normalise_code(part) for part in re.split(r"[,;]", value or "")]
    joined = ", ".join(code for code in codes if code)
    return joined[:300] or None


def _to_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _to_decimal(value):
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
