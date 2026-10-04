"""Curriculum administrator workspace: prospectus, versions and student context.

Every route here requires the ``admin`` role. Students, lecturers and heads
of department receive 403. All state changes are audit-logged by the
service layer (curriculum_service / academic_context).
"""

from __future__ import annotations

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

import academic_context
import curriculum_service as svc
from extensions import db
from integrations import soma
from models import (
    AcademicYear, AuditLog, CurriculumEntry, CurriculumVersion, DraftProgramme,
    IntegrationSyncLog, Module, NtaLevel, Programme, ProspectusDocument, Semester,
    StudentModuleRegistration, User, ENTRY_REVIEW_STATUSES,
)

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")
MODULE_TYPE_CHOICES = (("core", "Core Module"), ("general_studies", "General Studies Module"))


@admin_bp.before_request
@login_required
def require_admin():
    if not current_user.is_admin:
        abort(403)


def _version_or_404(version_id):
    return db.get_or_404(CurriculumVersion, version_id)


def _commit_or_flash(success_message):
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Curriculum administration change failed")
        flash("The change could not be saved. Nothing was modified; please try again.", "error")
        return False
    if success_message:
        flash(success_message, "success")
    return True


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------

@admin_bp.route("")
@admin_bp.route("/dashboard")
def dashboard():
    versions = CurriculumVersion.query.order_by(CurriculumVersion.created_at.desc()).all()
    documents = ProspectusDocument.query.order_by(ProspectusDocument.created_at.desc()).limit(5).all()
    adapter = soma.get_adapter(current_app.config)
    legacy_modules = Module.query.filter_by(provenance="legacy_seed", publication_status="published").count()
    return render_template(
        "admin/dashboard.html",
        versions=versions,
        documents=documents,
        soma_health=adapter.health_check(),
        soma_adapter=adapter.name,
        legacy_modules=legacy_modules,
        recent_syncs=IntegrationSyncLog.query.order_by(IntegrationSyncLog.created_at.desc()).limit(8).all(),
    )


# ---------------------------------------------------------------------------
# Prospectus documents
# ---------------------------------------------------------------------------

@admin_bp.route("/prospectus", methods=["GET", "POST"])
def prospectus():
    if request.method == "POST":
        try:
            document = svc.store_prospectus(
                request.files.get("file"), title=request.form.get("title"),
                academic_year_label=request.form.get("academic_year_label"), user=current_user,
            )
            message = f"“{document.title}” uploaded and preserved for audit."
            if document.extraction_status != "success":
                message = f"“{document.title}” was stored, but {document.extraction_message}"
            version = None
            if request.form.get("create_version"):
                version = svc.create_version(
                    label=request.form.get("version_label") or document.title,
                    academic_year_label=request.form.get("academic_year_label"),
                    user=current_user, is_demo=bool(request.form.get("is_demo")),
                )
                version.prospectus_document_id = document.id
                if document.extraction_status == "success":
                    count, problems = svc.extract_into_version(version, document, user=current_user)
                    message += f" {count} candidate entr{'y' if count == 1 else 'ies'} staged for review."
                    if problems:
                        message += f" {len(problems)} line(s) could not be read."
        except svc.CurriculumWorkflowError as error:
            db.session.rollback()
            flash(str(error), "error")
            return redirect(url_for("admin.prospectus"))
        if _commit_or_flash(None):
            flash(message, "success" if document.extraction_status == "success" else "warning")
            if version is not None:
                return redirect(url_for("admin.version_detail", version_id=version.id))
        return redirect(url_for("admin.prospectus"))
    documents = ProspectusDocument.query.order_by(ProspectusDocument.created_at.desc()).all()
    return render_template("admin/prospectus.html", documents=documents,
                           versions=CurriculumVersion.query.order_by(CurriculumVersion.created_at.desc()).all())


@admin_bp.route("/prospectus/<int:document_id>/file")
def prospectus_file(document_id):
    import io
    from flask import send_file
    from storage_backend import StorageError, read_file_bytes

    document = db.get_or_404(ProspectusDocument, document_id)
    try:
        content = read_file_bytes(document.stored_filename)
    except FileNotFoundError:
        abort(404)
    except StorageError:
        abort(503)
    response = send_file(io.BytesIO(content), mimetype=document.mime_type or "application/octet-stream",
                         as_attachment=True, download_name=document.original_filename)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.cache_control.private = True
    return response


# ---------------------------------------------------------------------------
# Curriculum versions
# ---------------------------------------------------------------------------

@admin_bp.route("/versions", methods=["POST"])
def create_version():
    try:
        version = svc.create_version(
            label=request.form.get("label"), academic_year_label=request.form.get("academic_year_label"),
            notes=request.form.get("notes"), user=current_user, is_demo=bool(request.form.get("is_demo")),
        )
    except svc.CurriculumWorkflowError as error:
        db.session.rollback()
        flash(str(error), "error")
        return redirect(url_for("admin.dashboard"))
    if _commit_or_flash(f"{version.label} created as a draft."):
        return redirect(url_for("admin.version_detail", version_id=version.id))
    return redirect(url_for("admin.dashboard"))


@admin_bp.route("/versions/<int:version_id>")
def version_detail(version_id):
    version = _version_or_404(version_id)
    report = svc.ValidationReport.from_json(version.last_validation_json) if version.last_validation_json else None
    status_filter = request.args.get("review") or ""
    entries = version.entries
    if status_filter in dict(ENTRY_REVIEW_STATUSES):
        entries = [entry for entry in entries if entry.review_status == status_filter]
    counts = {key: sum(1 for e in version.entries if e.review_status == key) for key, _ in ENTRY_REVIEW_STATUSES}
    return render_template(
        "admin/version.html", version=version, entries=entries, report=report,
        counts=counts, review_statuses=ENTRY_REVIEW_STATUSES, status_filter=status_filter,
        module_types=MODULE_TYPE_CHOICES,
        documents=ProspectusDocument.query.order_by(ProspectusDocument.created_at.desc()).all(),
        live_modules=Module.query.filter_by(curriculum_version_id=version.id).count(),
    )


def _version_action(version_id, action, success):
    version = _version_or_404(version_id)
    try:
        result = action(version)
    except svc.CurriculumWorkflowError as error:
        # Refusals happen before any live data changes; keep the refreshed
        # validation report and status downgrade so the admin sees why.
        _commit_or_flash(None)
        flash(str(error), "error")
        return redirect(url_for("admin.version_detail", version_id=version_id))
    message = success(version, result) if callable(success) else success
    _commit_or_flash(message)
    return redirect(url_for("admin.version_detail", version_id=version_id))


@admin_bp.route("/versions/<int:version_id>/validate", methods=["POST"])
def validate_version(version_id):
    version = _version_or_404(version_id)
    try:
        report = svc.run_validation(version, user=current_user)
    except svc.CurriculumWorkflowError as error:
        flash(str(error), "error")
        return redirect(url_for("admin.version_detail", version_id=version_id))
    if _commit_or_flash(None):
        if report.ok:
            flash(f"Validation passed with {len(report.warnings)} warning(s). The version can now be approved.", "success")
        else:
            flash(f"Validation found {len(report.errors)} problem(s). Correct them and validate again.", "error")
    return redirect(url_for("admin.version_detail", version_id=version_id))


@admin_bp.route("/versions/<int:version_id>/approve", methods=["POST"])
def approve_version(version_id):
    return _version_action(version_id, lambda v: svc.approve_version(v, user=current_user),
                           "Version approved. It can now be published.")


@admin_bp.route("/versions/<int:version_id>/publish", methods=["POST"])
def publish_version(version_id):
    make_current = bool(request.form.get("make_current"))
    return _version_action(
        version_id,
        lambda v: svc.publish_version(v, user=current_user, make_current=make_current),
        lambda v, count: f"{v.label} published: {count} module(s) are now live for {v.academic_year_label}.",
    )


@admin_bp.route("/versions/<int:version_id>/archive", methods=["POST"])
def archive_version(version_id):
    return _version_action(
        version_id, lambda v: svc.archive_version(v, user=current_user),
        lambda v, count: f"{v.label} archived. {count} module(s) retired; resources and history were preserved.",
    )


@admin_bp.route("/versions/<int:version_id>/delete", methods=["POST"])
def delete_version(version_id):
    version = _version_or_404(version_id)
    try:
        svc.delete_unpublished_version(version, user=current_user)
    except svc.CurriculumWorkflowError as error:
        flash(str(error), "error")
        return redirect(url_for("admin.version_detail", version_id=version_id))
    _commit_or_flash("Draft version deleted.")
    return redirect(url_for("admin.dashboard"))


@admin_bp.route("/versions/<int:version_id>/extract", methods=["POST"])
def extract_version(version_id):
    version = _version_or_404(version_id)
    document = db.session.get(ProspectusDocument, request.form.get("document_id", type=int) or 0)
    if document is None:
        flash("Choose an uploaded prospectus to extract from.", "error")
        return redirect(url_for("admin.version_detail", version_id=version_id))
    try:
        count, problems = svc.extract_into_version(version, document, user=current_user)
    except svc.CurriculumWorkflowError as error:
        db.session.rollback()
        flash(str(error), "error")
        return redirect(url_for("admin.version_detail", version_id=version_id))
    note = f" {len(problems)} line(s) could not be read." if problems else ""
    _commit_or_flash(f"{count} candidate entries staged. Review each one against the prospectus.{note}")
    return redirect(url_for("admin.version_detail", version_id=version_id))


@admin_bp.route("/versions/<int:version_id>/import-csv", methods=["POST"])
def import_csv(version_id):
    version = _version_or_404(version_id)
    upload = request.files.get("file")
    if upload is None or not upload.filename or not upload.filename.lower().endswith(".csv"):
        flash("Choose a CSV file that follows the import template.", "error")
        return redirect(url_for("admin.version_detail", version_id=version_id))
    try:
        text = upload.read().decode("utf-8", errors="ignore")
        rows, problems = svc.parse_curriculum_csv(text)
        if not rows:
            raise svc.CurriculumWorkflowError("The CSV contains no curriculum rows.")
        count = svc.stage_rows(version, rows, origin="csv", user=current_user)
        from governance import record_audit
        record_audit("curriculum.csv_imported", "CurriculumVersion", target_id=version.id,
                     target_label=version.label, details={"entries": count, "problems": problems[:20]})
    except svc.CurriculumWorkflowError as error:
        db.session.rollback()
        flash(str(error), "error")
        return redirect(url_for("admin.version_detail", version_id=version_id))
    note = f" {len(problems)} line(s) need attention." if problems else ""
    _commit_or_flash(f"{count} entries imported for review.{note}")
    return redirect(url_for("admin.version_detail", version_id=version_id))


# --- programmes ------------------------------------------------------------

@admin_bp.route("/versions/<int:version_id>/programmes", methods=["POST"])
@admin_bp.route("/versions/<int:version_id>/programmes/<int:programme_id>", methods=["POST"])
def save_programme(version_id, programme_id=None):
    version = _version_or_404(version_id)
    if programme_id:
        programme = DraftProgramme.query.filter_by(id=programme_id, version_id=version.id).first_or_404()
    else:
        programme = DraftProgramme(version_id=version.id, department_name="", name="", levels_csv="")
    try:
        if programme_id is None:
            version.programmes.append(programme)
        svc.programme_from_form(programme, request.form, version)
        db.session.flush()
        from governance import record_audit
        record_audit("curriculum.programme_saved", "DraftProgramme", target_id=programme.id,
                     target_label=f"{programme.department_name} · {programme.name}",
                     details={"version_id": version.id, "levels": programme.levels_csv})
    except svc.CurriculumWorkflowError as error:
        db.session.rollback()
        flash(str(error), "error")
        return redirect(url_for("admin.version_detail", version_id=version_id))
    _commit_or_flash("Programme saved.")
    return redirect(url_for("admin.version_detail", version_id=version_id))


@admin_bp.route("/versions/<int:version_id>/programmes/<int:programme_id>/delete", methods=["POST"])
def delete_programme(version_id, programme_id):
    version = _version_or_404(version_id)
    programme = DraftProgramme.query.filter_by(id=programme_id, version_id=version.id).first_or_404()
    if not version.is_editable:
        flash("Published curriculum cannot be changed.", "error")
    elif programme.entries:
        flash("Reassign or delete this programme's entries first.", "error")
    else:
        db.session.delete(programme)
        svc.mark_changed(version)
        _commit_or_flash("Programme removed from the draft.")
    return redirect(url_for("admin.version_detail", version_id=version_id))


# --- entries ---------------------------------------------------------------

@admin_bp.route("/versions/<int:version_id>/entries", methods=["POST"])
@admin_bp.route("/versions/<int:version_id>/entries/<int:entry_id>", methods=["POST"])
def save_entry(version_id, entry_id=None):
    version = _version_or_404(version_id)
    if entry_id:
        entry = CurriculumEntry.query.filter_by(id=entry_id, version_id=version.id).first_or_404()
    else:
        entry = CurriculumEntry(version_id=version.id, origin="manual")
    try:
        svc.entry_from_form(entry, request.form, version)
        entry.updated_by_id = current_user.id
        if entry_id is None:
            db.session.add(entry)
        db.session.flush()
        from governance import record_audit
        record_audit("curriculum.entry_saved", "CurriculumEntry", target_id=entry.id,
                     target_label=f"{entry.module_code or ''} {entry.module_name or ''}".strip(),
                     details={"version_id": version.id, "review_status": entry.review_status})
    except svc.CurriculumWorkflowError as error:
        db.session.rollback()
        flash(str(error), "error")
        return redirect(url_for("admin.version_detail", version_id=version_id))
    _commit_or_flash("Entry saved.")
    return redirect(url_for("admin.version_detail", version_id=version_id, _anchor=f"entry-{entry.id}"))


@admin_bp.route("/versions/<int:version_id>/entries/<int:entry_id>/delete", methods=["POST"])
def delete_entry(version_id, entry_id):
    version = _version_or_404(version_id)
    entry = CurriculumEntry.query.filter_by(id=entry_id, version_id=version.id).first_or_404()
    if not version.is_editable:
        flash("Published curriculum cannot be changed.", "error")
    else:
        db.session.delete(entry)
        svc.mark_changed(version)
        _commit_or_flash("Entry removed from the draft.")
    return redirect(url_for("admin.version_detail", version_id=version_id))


# ---------------------------------------------------------------------------
# Student academic context (internal mechanism until SOMA is authorised)
# ---------------------------------------------------------------------------

@admin_bp.route("/students")
def students():
    query_text = (request.args.get("q") or "").strip()
    query = User.query.filter_by(role="student")
    if query_text:
        like = f"%{query_text}%"
        query = query.filter(db.or_(User.full_name.ilike(like), User.registration_number.ilike(like),
                                    User.email.ilike(like)))
    return render_template("admin/students.html", students=query.order_by(User.full_name).limit(100).all(),
                           query=query_text)


@admin_bp.route("/students/<int:user_id>/context", methods=["GET", "POST"])
def student_context(user_id):
    student = User.query.filter_by(id=user_id, role="student").first_or_404()
    if request.method == "POST":
        try:
            academic_context.set_internal_context(
                student,
                programme_id=request.form.get("programme_id", type=int),
                level_id=request.form.get("nta_level_id", type=int),
                semester_id=request.form.get("semester_id", type=int),
                academic_year_id=request.form.get("academic_year_id", type=int),
                module_ids=[int(v) for v in request.form.getlist("module_ids") if v.isdigit()],
                actor=current_user,
            )
        except academic_context.AcademicContextError as error:
            db.session.rollback()
            flash(str(error), "error")
            return redirect(url_for("admin.student_context", user_id=user_id))
        _commit_or_flash(f"Academic context for {student.full_name} updated.")
        return redirect(url_for("admin.student_context", user_id=user_id))

    programmes = Programme.query.order_by(Programme.department_id, Programme.display_order).all()
    registered_ids = {row.module_id for row in StudentModuleRegistration.query.filter_by(student_id=student.id)}
    modules = []
    if student.programme_id:
        modules = (Module.query.join(Semester).join(NtaLevel)
                   .filter(NtaLevel.programme_id == student.programme_id, Module.publication_status == "published")
                   .order_by(NtaLevel.level_number, Semester.semester_number, Module.display_order).all())
    return render_template(
        "admin/student_context.html", student=student, programmes=programmes,
        academic_years=AcademicYear.query.order_by(AcademicYear.label.desc()).all(),
        modules=modules, registered_ids=registered_ids,
        context=academic_context.describe_context(student),
        sync_logs=IntegrationSyncLog.query.filter_by(student_id=student.id)
        .order_by(IntegrationSyncLog.created_at.desc()).limit(10).all(),
    )


@admin_bp.route("/students/<int:user_id>/soma-sync", methods=["POST"])
def student_soma_sync(user_id):
    student = User.query.filter_by(id=user_id, role="student").first_or_404()
    result = academic_context.sync_from_soma(student, triggered_by=current_user)
    if _commit_or_flash(None):
        flash(result["message"], "success" if result["ok"] else "warning")
    return redirect(url_for("admin.student_context", user_id=user_id))


@admin_bp.route("/audit-log")
def audit_log():
    rows = AuditLog.query.order_by(AuditLog.created_at.desc()).limit(300).all()
    return render_template("admin/audit_log.html", audit_logs=rows)
