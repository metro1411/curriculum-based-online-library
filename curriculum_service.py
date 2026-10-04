"""Prospectus ingestion and curriculum-version workflow.

Workflow (enforced here, exposed by routes/admin.py):

    UPLOAD -> EXTRACT -> REVIEW -> VALIDATE -> APPROVE -> PUBLISH (-> ARCHIVE)

* The uploaded prospectus is stored unchanged for audit (ProspectusDocument).
* Extraction only ever produces *staged* CurriculumEntry rows marked
  ``needs_review``. Nothing extracted is trusted or shown to students.
* Validation must pass, and every staged entry must be reviewed, before a
  version can be approved; approval is required before publishing.
* Publishing materialises the staged rows into the live
  Department -> Programme -> NtaLevel -> Semester -> Module tree under the
  version's academic year. Published and archived versions are immutable.
* Archiving retires a version's modules (``publication_status='archived'``)
  without deleting modules, resources, analytics or history.

Nothing in this module invents curriculum data: every value comes from the
administrator, an uploaded file, or a CSV the administrator supplied.
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
    Module, ModulePrerequisite, NtaLevel, Programme, ProspectusChunk,
    ProspectusDocument, Semester, utcnow,
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
    not raise: the document is kept with ``extraction_status='failed'`` so the
    administrator can still enter the curriculum manually.
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
    if academic_year_label and not _valid_academic_year(academic_year_label):
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
            academic_year_label=academic_year_label or None,
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
                "No readable text was found. The file may be scanned or protected; "
                "enter or import the curriculum manually."
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
    _require_editable(version)
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
    mark_changed(version)
    return created


def extract_into_version(version, document, *, user):
    """Run extraction for a stored prospectus and stage the results."""
    _require_editable(version)
    if document.extraction_status != "success" or not document.extracted_text:
        raise CurriculumWorkflowError(
            "No text could be extracted from this prospectus. Add the curriculum manually or import the CSV template."
        )
    if document.original_filename.lower().endswith(".csv"):
        rows, problems = parse_curriculum_csv(document.extracted_text)
        origin = "csv"
    else:
        rows, problems = extract_candidates(document.extracted_text), []
        origin = "extracted"
    if not rows:
        raise CurriculumWorkflowError(
            "No module lines were recognised. The layout may not be machine-readable; add entries manually."
        )
    count = stage_rows(version, rows, origin=origin, user=user)
    version.prospectus_document_id = document.id
    version.source = "prospectus_import"
    record_audit(
        "curriculum.extracted", "CurriculumVersion", target_id=version.id, target_label=version.label,
        actor=user, details={"document_id": document.id, "entries": count, "origin": origin,
                             "problems": problems[:20]},
    )
    return count, problems


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

@dataclass
class ValidationReport:
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    @property
    def ok(self):
        return not self.errors

    def error(self, message, entry=None, programme=None):
        self.errors.append(_issue(message, entry, programme))

    def warn(self, message, entry=None, programme=None):
        self.warnings.append(_issue(message, entry, programme))

    def for_entry(self, entry_id):
        return [item["message"] for item in self.errors + self.warnings if item.get("entry_id") == entry_id]

    def to_json(self):
        return json.dumps({"errors": self.errors, "warnings": self.warnings})

    @classmethod
    def from_json(cls, raw):
        try:
            data = json.loads(raw or "{}")
        except ValueError:
            data = {}
        return cls(errors=data.get("errors", []), warnings=data.get("warnings", []))


def _issue(message, entry, programme):
    item = {"message": message}
    if entry is not None:
        item["entry_id"] = entry.id
    if programme is not None:
        item["programme_id"] = programme.id
    return item


def validate_version(version):
    """Check a version for structural problems. Never modifies data."""
    report = ValidationReport()
    if not _valid_academic_year(version.academic_year_label):
        report.error("The academic year must use the format 2026/2027.")

    programme_ids = {p.id for p in version.programmes}
    for programme in version.programmes:
        if not programme.department_name.strip() or not programme.name.strip():
            report.error("Programme is missing its department or name.", programme=programme)
        if not programme.levels:
            report.error(f"{programme.name}: define at least one NTA level.", programme=programme)
        elif any(level < 1 or level > MAX_LEVEL for level in programme.levels):
            report.error(f"{programme.name}: NTA levels must be between 1 and {MAX_LEVEL}.", programme=programme)
        if not 1 <= (programme.semesters_per_level or 0) <= MAX_SEMESTERS:
            report.error(f"{programme.name}: semesters per level must be between 1 and {MAX_SEMESTERS}.",
                         programme=programme)

    active = [entry for entry in version.entries if entry.review_status != "rejected"]
    if not active:
        report.error("The version has no curriculum entries to publish.")

    seen_codes, seen_names = {}, {}
    codes_in_version = {}
    for entry in active:
        label = entry.module_code or entry.module_name or f"Entry {entry.id}"
        programme = entry.programme
        if programme is None or entry.programme_id not in programme_ids:
            report.error(f"{label}: assign the module to a programme defined in this version.", entry)
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
        elif programme is not None and not 1 <= entry.semester_number <= programme.semesters_per_level:
            report.error(
                f"{label}: semester {entry.semester_number} is invalid; {programme.name} has "
                f"{programme.semesters_per_level} semester(s) per level.", entry)
        if entry.module_type not in MODULE_TYPES:
            report.error(f"{label}: choose Core or General Studies.", entry)
        if entry.credits is not None and not (Decimal("0") <= entry.credits <= Decimal("100")):
            report.error(f"{label}: credits must be between 0 and 100.", entry)
        if entry.credits is None:
            report.warn(f"{label}: credits not recorded (leave blank only if the prospectus omits them).", entry)
        if entry.review_status != "reviewed":
            report.error(f"{label}: mark the entry as reviewed after checking it against the prospectus.", entry)

        if entry.programme_id and entry.module_code:
            code_key = (entry.programme_id, entry.module_code.lower())
            if code_key in seen_codes:
                report.error(f"{label}: duplicate module code in {programme.name}.", entry)
            seen_codes[code_key] = entry
            codes_in_version.setdefault(entry.module_code.upper(), []).append(entry)
        if entry.programme_id and entry.module_name:
            name_key = (entry.programme_id, entry.nta_level, entry.semester_number, entry.module_name.strip().lower())
            if name_key in seen_names:
                report.error(f"{label}: duplicate module name in the same programme, level and semester.", entry)
            seen_names[name_key] = entry

    # Prerequisites must resolve and must not loop.
    graph = {}
    for entry in active:
        if not entry.module_code:
            continue
        node = entry.module_code.upper()
        graph.setdefault(node, set())
        for code in entry.prerequisite_list:
            if code == node:
                report.error(f"{entry.module_code}: a module cannot be its own prerequisite.", entry)
                continue
            if code in codes_in_version:
                graph[node].add(code)
            elif not _live_module_with_code(code):
                report.error(f"{entry.module_code}: prerequisite {code} is not in this version or the live curriculum.",
                             entry)
    cycle = _find_cycle(graph)
    if cycle:
        report.error("Prerequisites form a loop: " + " → ".join(cycle) + ".")

    # Never silently collide with modules already live for the same year.
    for entry in active:
        if entry.programme is None or not entry.module_code:
            continue
        conflict = _live_conflict(version, entry)
        if conflict is not None:
            report.error(
                f"{entry.module_code}: already published in {entry.programme.name} for "
                f"{version.academic_year_label}. Archive it or correct this entry.", entry)
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


def _live_module_with_code(code):
    return (Module.query.filter(db.func.upper(Module.code) == code.upper(),
                                Module.publication_status == "published").first())


def _live_conflict(version, entry):
    return (
        Module.query.join(Semester).join(NtaLevel).join(Programme).join(Department)
        .join(AcademicYear, Module.academic_year_id == AcademicYear.id)
        .filter(
            db.func.lower(Department.name) == entry.programme.department_name.lower(),
            db.func.lower(Programme.name) == entry.programme.name.lower(),
            AcademicYear.label == version.academic_year_label,
            db.func.upper(Module.code) == entry.module_code.upper(),
            Module.publication_status != "archived",
            db.or_(Module.curriculum_version_id.is_(None), Module.curriculum_version_id != version.id),
        )
        .first()
    )


# ---------------------------------------------------------------------------
# State transitions
# ---------------------------------------------------------------------------

def create_version(*, label, academic_year_label, user, notes=None, is_demo=False):
    label = " ".join((label or "").split())[:150]
    academic_year_label = (academic_year_label or "").strip()
    if len(label) < 3:
        raise CurriculumWorkflowError("Give the curriculum version a clear name.")
    if is_demo and "DEMO" not in label.upper():
        label = f"DEMO · {label}"[:150]
    if not _valid_academic_year(academic_year_label):
        raise CurriculumWorkflowError("Use an academic year in the format 2026/2027.")
    if CurriculumVersion.query.filter(db.func.lower(CurriculumVersion.label) == label.lower()).first():
        raise CurriculumWorkflowError("A curriculum version with that name already exists.")
    version = CurriculumVersion(
        label=label, academic_year_label=academic_year_label, notes=(notes or "").strip() or None,
        is_demo=bool(is_demo), created_by_id=getattr(user, "id", None), status="draft",
    )
    db.session.add(version)
    db.session.flush()
    record_audit("curriculum.version_created", "CurriculumVersion", target_id=version.id,
                 target_label=version.label, actor=user,
                 details={"academic_year": academic_year_label, "demo": bool(is_demo)})
    return version


def mark_changed(version):
    """Any edit invalidates a previous validation or approval."""
    if version.status in {"validated", "approved"}:
        version.status = "draft"
        version.approved_by_id = None
        version.approved_at = None


def run_validation(version, *, user):
    _require_editable(version)
    report = validate_version(version)
    version.last_validation_json = report.to_json()
    version.validated_at = utcnow()
    version.status = "validated" if report.ok else "draft"
    record_audit("curriculum.validated", "CurriculumVersion", target_id=version.id,
                 target_label=version.label, actor=user,
                 details={"ok": report.ok, "errors": len(report.errors), "warnings": len(report.warnings)})
    return report


def approve_version(version, *, user):
    if version.status != "validated":
        raise CurriculumWorkflowError("Validate the version successfully before approving it.")
    report = validate_version(version)
    if not report.ok:
        version.status = "draft"
        version.last_validation_json = report.to_json()
        raise CurriculumWorkflowError("The version changed and no longer validates. Review the issues and try again.")
    version.status = "approved"
    version.approved_by_id = user.id
    version.approved_at = utcnow()
    record_audit("curriculum.approved", "CurriculumVersion", target_id=version.id,
                 target_label=version.label, actor=user)


def publish_version(version, *, user, make_current=False):
    """Materialise an approved version into the live curriculum tree."""
    if version.status != "approved":
        raise CurriculumWorkflowError("Only an approved version can be published.")
    report = validate_version(version)
    if not report.ok:
        version.status = "draft"
        version.last_validation_json = report.to_json()
        raise CurriculumWorkflowError("Publishing stopped: the version no longer validates.")

    active = [entry for entry in version.entries if entry.review_status != "rejected"]
    touched_departments = {}
    created_by_code = {}
    for entry in active:
        draft = entry.programme
        department = _get_or_create_department(draft.department_name)
        touched_departments[department.id] = department
        programme = _get_or_create_programme(department, draft)
        level = _get_or_create_level(programme, entry.nta_level, draft.year_label_map.get(entry.nta_level))
        semester = _get_or_create_semester(level, entry.semester_number)
        year = _get_or_create_year(department, version.academic_year_label, user)
        next_order = (db.session.query(db.func.max(Module.display_order))
                      .filter_by(semester_id=semester.id).scalar() or 0) + 1
        module = Module(
            semester_id=semester.id, academic_year_id=year.id, name=entry.module_name.strip(),
            code=entry.module_code, module_type=entry.module_type, credits=entry.credits,
            description=entry.description, publication_status="published", is_active=True,
            display_order=next_order, created_by_id=user.id, provenance="prospectus",
            curriculum_version_id=version.id,
        )
        db.session.add(module)
        db.session.flush()
        entry.live_module_id = module.id
        created_by_code.setdefault(entry.module_code.upper(), []).append((draft.id, module))

    for entry in active:
        for code in entry.prerequisite_list:
            prerequisite = _resolve_prerequisite(code, entry.programme_id, created_by_code)
            if prerequisite is not None and prerequisite.id != entry.live_module_id:
                db.session.add(ModulePrerequisite(module_id=entry.live_module_id,
                                                  prerequisite_module_id=prerequisite.id))

    if make_current:
        for department in touched_departments.values():
            for year in AcademicYear.query.filter_by(department_id=department.id).all():
                year.is_current = year.label == version.academic_year_label
                if year.is_current:
                    year.status = "active"

    version.status = "published"
    version.published_by_id = user.id
    version.published_at = utcnow()
    for department in touched_departments.values():
        record_audit("curriculum.published", "CurriculumVersion", target_id=version.id,
                     target_label=version.label, actor=user, department_id=department.id,
                     details={"modules": len(active), "make_current": bool(make_current)})
    return len(active)


def archive_version(version, *, user):
    if version.status != "published":
        raise CurriculumWorkflowError("Only a published version can be archived.")
    modules = Module.query.filter_by(curriculum_version_id=version.id).all()
    for module in modules:
        module.publication_status = "archived"
        module.is_active = False
    version.status = "archived"
    version.archived_by_id = user.id
    version.archived_at = utcnow()
    record_audit("curriculum.archived", "CurriculumVersion", target_id=version.id,
                 target_label=version.label, actor=user, details={"modules_retired": len(modules)})
    return len(modules)


def delete_unpublished_version(version, *, user):
    if version.status in {"published", "archived"} or version.published_at:
        raise CurriculumWorkflowError("Published curriculum is a historical record and cannot be deleted. Archive it instead.")
    record_audit("curriculum.draft_deleted", "CurriculumVersion", target_id=version.id,
                 target_label=version.label, actor=user)
    db.session.delete(version)


def _require_editable(version):
    if not version.is_editable:
        raise CurriculumWorkflowError(
            f"This version is {version.status_label.lower()} and can no longer be edited. Create a new version instead."
        )


# ---------------------------------------------------------------------------
# Live-tree helpers
# ---------------------------------------------------------------------------

def _get_or_create_department(name):
    department = Department.query.filter(db.func.lower(Department.name) == name.lower()).first()
    if department is None:
        department = Department(name=name, slug=_unique_slug(Department, name), is_active=True,
                                display_order=(db.session.query(db.func.max(Department.display_order)).scalar() or 0) + 1)
        db.session.add(department)
        db.session.flush()
    department.is_active = True
    return department


def _get_or_create_programme(department, draft):
    programme = Programme.query.filter(Programme.department_id == department.id,
                                       db.func.lower(Programme.name) == draft.name.lower()).first()
    if programme is None:
        base = slugify(draft.name)
        slug, n = base, 2
        while Programme.query.filter_by(department_id=department.id, slug=slug).first():
            slug, n = f"{base}-{n}", n + 1
        programme = Programme(department_id=department.id, name=draft.name, slug=slug,
                              description=draft.description, is_active=True,
                              display_order=len(department.programmes) + 1)
        db.session.add(programme)
        db.session.flush()
    programme.is_active = True
    return programme


def _get_or_create_level(programme, level_number, year_label):
    level = NtaLevel.query.filter_by(programme_id=programme.id, level_number=level_number).first()
    if level is None:
        level = NtaLevel(programme_id=programme.id, level_number=level_number)
        db.session.add(level)
        db.session.flush()
    level.is_active = True
    if year_label:
        level.year_label = year_label
    return level


def _get_or_create_semester(level, semester_number):
    semester = Semester.query.filter_by(nta_level_id=level.id, semester_number=semester_number).first()
    if semester is None:
        semester = Semester(nta_level_id=level.id, semester_number=semester_number)
        db.session.add(semester)
        db.session.flush()
    semester.is_active = True
    return semester


def _get_or_create_year(department, label, user):
    year = AcademicYear.query.filter_by(department_id=department.id, label=label).first()
    if year is None:
        has_current = AcademicYear.query.filter_by(department_id=department.id, is_current=True).first()
        year = AcademicYear(department_id=department.id, label=label, status="active",
                            is_current=has_current is None, created_by_id=user.id)
        db.session.add(year)
        db.session.flush()
    return year


def _resolve_prerequisite(code, draft_programme_id, created_by_code):
    candidates = created_by_code.get(code.upper(), [])
    same = [module for prog_id, module in candidates if prog_id == draft_programme_id]
    if same:
        return same[0]
    if candidates:
        return candidates[0][1]
    return _live_module_with_code(code)


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


def entry_from_form(entry, form, version):
    """Apply an administrator's corrections to a staged entry."""
    _require_editable(version)
    programme_id = _to_int(form.get("programme_id"))
    programme = next((p for p in version.programmes if p.id == programme_id), None)
    entry.programme = programme
    entry.nta_level = _to_int(form.get("nta_level"))
    entry.semester_number = _to_int(form.get("semester_number"))
    entry.module_code = _normalise_code(form.get("module_code"))
    entry.module_name = " ".join((form.get("module_name") or "").split())[:200] or None
    module_type = form.get("module_type") or "core"
    entry.module_type = module_type if module_type in MODULE_TYPES else "core"
    raw_credits = (form.get("credits") or "").strip()
    credits = _to_decimal(raw_credits)
    if raw_credits and credits is None:
        raise CurriculumWorkflowError("Credits must be a number, or left blank if the prospectus omits them.")
    entry.credits = credits
    entry.description = (form.get("description") or "").strip()[:4000] or None
    entry.prerequisite_codes = _normalise_codes(form.get("prerequisite_codes"))
    review_status = form.get("review_status") or "needs_review"
    entry.review_status = review_status if review_status in {"needs_review", "reviewed", "rejected"} else "needs_review"
    mark_changed(version)


def programme_from_form(programme, form, version):
    _require_editable(version)
    department_name = " ".join((form.get("department_name") or "").split())[:150]
    name = " ".join((form.get("name") or "").split())[:150]
    if len(department_name) < 2 or len(name) < 3:
        raise CurriculumWorkflowError("Enter the department and programme names exactly as printed in the prospectus.")
    levels = sorted({int(part) for part in re.split(r"[,\s]+", form.get("levels_csv") or "") if part.isdigit()})
    if not levels:
        raise CurriculumWorkflowError("List the NTA levels this programme covers, e.g. 4, 5, 6.")
    semesters = _to_int(form.get("semesters_per_level")) or 2
    duplicate = next((p for p in version.programmes if p is not programme
                      and p.department_name.lower() == department_name.lower() and p.name.lower() == name.lower()), None)
    if duplicate:
        raise CurriculumWorkflowError("That programme is already defined in this version.")
    programme.department_name = department_name
    programme.name = name
    programme.description = (form.get("description") or "").strip()[:2000] or None
    programme.levels_csv = ",".join(str(level) for level in levels)
    programme.semesters_per_level = semesters
    programme.year_labels = (form.get("year_labels") or "").strip()[:200] or None
    mark_changed(version)
