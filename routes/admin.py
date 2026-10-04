"""Curriculum Administrator workspace: the prospectus, student placement and audit log.

Every route here requires the ``admin`` role. The prospectus decides every
module in every department; Heads of Department manage their own modules
between uploads.
"""

from __future__ import annotations

import io

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required

import academic_context
import curriculum_service as svc
from extensions import db
from integrations import soma
from models import (
    AcademicYear, AuditLog, CurriculumVersion, Department, IntegrationSyncLog, Module, NtaLevel, Programme,
    Semester, StudentModuleRegistration, User,
)
from storage_backend import StorageError, read_file_bytes

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


@admin_bp.before_request
@login_required
def require_admin():
    if not current_user.is_admin:
        abort(403)


@admin_bp.route("")
@admin_bp.route("/dashboard")
def dashboard():
    adapter = soma.get_adapter(current_app.config)
    live_modules = Module.query.filter(Module.publication_status == "published")
    return render_template(
        "admin/dashboard.html",
        live=svc.live_version(),
        counts={
            "departments": Department.query.filter_by(is_active=True).count(),
            "programmes": Programme.query.filter_by(is_active=True).count(),
            "modules": live_modules.count(),
            "students": User.query.filter_by(role="student").count(),
        },
        versions=CurriculumVersion.query.order_by(CurriculumVersion.created_at.desc()).limit(5).all(),
        soma_health=adapter.health_check(),
        soma_adapter=adapter.name,
        recent_syncs=IntegrationSyncLog.query.order_by(IntegrationSyncLog.created_at.desc()).limit(5).all(),
    )


# ---------------------------------------------------------------------------
# Prospectus: the source of truth for every module
# ---------------------------------------------------------------------------

@admin_bp.route("/prospectus", methods=["GET", "POST"])
def prospectus():
    if request.method == "POST":
        try:
            version = svc.publish_upload(
                request.files.get("file"), title=request.form.get("title"),
                academic_year_label=request.form.get("academic_year_label"), user=current_user,
                allow_large_change=bool(request.form.get("allow_large_change")),
            )
            db.session.commit()
        except svc.CurriculumWorkflowError as error:
            db.session.rollback()
            flash(str(error), "error")
            return redirect(url_for("admin.prospectus"))
        if version.status == "published":
            flash("Prospectus published. Every module now follows it.", "success")
        else:
            flash("Nothing was published. Fix the problems below and upload again.", "error")
        return redirect(url_for("admin.prospectus_result", version_id=version.id))
    versions = CurriculumVersion.query.order_by(CurriculumVersion.created_at.desc()).limit(20).all()
    return render_template("admin/prospectus.html", live=svc.live_version(), versions=versions)


@admin_bp.route("/prospectus/<int:version_id>")
def prospectus_result(version_id):
    version = db.get_or_404(CurriculumVersion, version_id)
    report = version.allocation_report
    changes = [item for item in report.get("modules", []) if item["outcome"] != "new"]
    return render_template("admin/prospectus_result.html", version=version, report=report, changes=changes,
                           outline=svc.version_outline(version), notes=svc.grouped_notes(version))


@admin_bp.route("/prospectus/undo", methods=["POST"])
def prospectus_undo():
    try:
        version = svc.undo_last_publish(user=current_user)
        db.session.commit()
    except svc.CurriculumWorkflowError as error:
        db.session.rollback()
        flash(str(error), "error")
        return redirect(url_for("admin.prospectus"))
    flash(f"Undone. “{version.label}” is no longer live.", "success")
    return redirect(url_for("admin.prospectus"))


@admin_bp.route("/prospectus/<int:version_id>/file")
def prospectus_file(version_id):
    document = db.get_or_404(CurriculumVersion, version_id).prospectus_document
    if document is None:
        abort(404)
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
# Student academic context, every department (until SOMA is connected)
# ---------------------------------------------------------------------------

def _student(user_id):
    return User.query.filter_by(id=user_id, role="student").first_or_404()


@admin_bp.route("/students")
def students():
    query_text = (request.args.get("q") or "").strip()
    query = User.query.filter_by(role="student")
    if query_text:
        like = f"%{query_text}%"
        query = query.filter(db.or_(User.full_name.ilike(like), User.registration_number.ilike(like),
                                    User.email.ilike(like)))
    return render_template("department/students.html", area="admin", department=None,
                           students=query.order_by(User.full_name).limit(100).all(), query=query_text)


@admin_bp.route("/students/<int:user_id>", methods=["GET", "POST"])
def student_context(user_id):
    student = _student(user_id)
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
            db.session.commit()
            flash(f"Placement for {student.full_name} saved.", "success")
        except academic_context.AcademicContextError as error:
            db.session.rollback()
            flash(str(error), "error")
        return redirect(url_for("admin.student_context", user_id=user_id))

    registered_ids = {row.module_id for row in StudentModuleRegistration.query.filter_by(student_id=student.id)}
    modules = []
    if student.programme_id:
        modules = (Module.query.join(Semester).join(NtaLevel)
                   .filter(NtaLevel.programme_id == student.programme_id, Module.publication_status == "published")
                   .order_by(NtaLevel.level_number, Semester.semester_number, Module.display_order).all())
    return render_template(
        "department/student_context.html", area="admin", department=None, student=student,
        programmes=Programme.query.filter_by(is_active=True)
        .order_by(Programme.department_id, Programme.display_order).all(),
        academic_years=AcademicYear.query.order_by(AcademicYear.label.desc()).all(),
        modules=modules, registered_ids=registered_ids, context=academic_context.describe_context(student),
        sync_logs=IntegrationSyncLog.query.filter_by(student_id=student.id)
        .order_by(IntegrationSyncLog.created_at.desc()).limit(10).all(),
    )


@admin_bp.route("/students/<int:user_id>/soma-sync", methods=["POST"])
def student_soma_sync(user_id):
    student = _student(user_id)
    result = academic_context.sync_from_soma(student, triggered_by=current_user)
    db.session.commit()
    flash(result["message"], "success" if result["ok"] else "warning")
    return redirect(url_for("admin.student_context", user_id=user_id))


@admin_bp.route("/audit-log")
def audit_log():
    rows = AuditLog.query.order_by(AuditLog.created_at.desc()).limit(300).all()
    return render_template("admin/audit_log.html", audit_logs=rows)
