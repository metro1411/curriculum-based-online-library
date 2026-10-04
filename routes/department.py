"""Head of Department curriculum, lecturer and governance workspace."""

from __future__ import annotations


from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import case

import academic_context
import curriculum_service as svc
from extensions import db
from governance import record_audit
from learning import department_insights
from models import (
    AcademicYear, AuditLog, CurriculumVersion, IntegrationSyncLog, LecturerAssignment, LecturerRequest,
    Module, NtaLevel, Programme, Semester, StudentModuleRegistration, User, utcnow,
)
from notifications import notify


department_bp = Blueprint("department", __name__, url_prefix="/department")


@department_bp.before_request
@login_required
def require_department_head():
    if not current_user.is_department_head:
        abort(403)


def _department():
    if current_user.department is None:
        abort(403)
    return current_user.department


def _request_or_404(user_id):
    item = LecturerRequest.query.filter_by(
        user_id=user_id, department_id=_department().id
    ).first()
    if item is None:
        abort(404)
    return item


def _department_modules():
    return (
        Module.query.join(Semester).join(NtaLevel).join(Programme)
        .filter(Programme.department_id == _department().id)
        .order_by(
            Module.academic_year_id.desc(),
            Programme.display_order,
            NtaLevel.level_number,
            Semester.semester_number,
            Module.display_order,
        )
    )


def _department_lecturer(user_id):
    lecturer = User.query.filter_by(
        id=user_id, department_id=_department().id, role="lecturer"
    ).first()
    if lecturer is None:
        abort(404)
    return lecturer


@department_bp.route("")
@department_bp.route("/dashboard")
def dashboard():
    department = _department()
    pending_requests = (
        LecturerRequest.query.filter_by(department_id=department.id, status="pending")
        .order_by(LecturerRequest.created_at.asc())
        .all()
    )
    lecturers = (
        User.query.filter_by(department_id=department.id, role="lecturer")
        .order_by(User.is_active_account.desc(), User.full_name.asc())
        .all()
    )
    modules = _department_modules().all()
    pending_claims = (
        LecturerAssignment.query.join(Module).join(Semester).join(NtaLevel).join(Programme)
        .filter(
            Programme.department_id == department.id,
            LecturerAssignment.status == "pending",
        )
        .order_by(LecturerAssignment.created_at.asc())
        .all()
    )
    current_year = AcademicYear.query.filter_by(
        department_id=department.id, is_current=True
    ).first()
    return render_template(
        "department/dashboard.html",
        department=department,
        pending_requests=pending_requests,
        pending_claims=pending_claims,
        lecturers=lecturers,
        modules=modules,
        current_year=current_year,
        insights=department_insights(department.id),
    )


@department_bp.route("/lecturer-requests")
def lecturer_requests():
    department = _department()
    items = (
        LecturerRequest.query.filter_by(department_id=department.id)
        .order_by(LecturerRequest.status.asc(), LecturerRequest.created_at.asc())
        .all()
    )
    return render_template(
        "department/lecturer_requests.html",
        department=department,
        lecturer_requests=items,
    )


@department_bp.route("/lecturer-requests/<int:user_id>/approve", methods=["POST"])
def approve_lecturer(user_id):
    item = _request_or_404(user_id)
    if item.status != "pending":
        flash("This lecturer request has already been reviewed.", "warning")
        return redirect(url_for("department.lecturer_requests"))

    user = item.user
    user.account_status = "active"
    user.is_active_account = True
    user.deactivated_at = None
    user.deactivated_by_id = None
    item.status = "approved"
    item.reviewed_by_id = current_user.id
    item.reviewed_at = utcnow()

    # Backwards-compatible support for packages that included assignment
    # checkboxes on this form. The new primary workflow is lecturer claim -> HOD
    # approval, but a valid HOD-selected module is still treated as approved.
    selected_ids = {value for value in request.form.getlist("module_ids") if value.isdigit()}
    allowed_by_id = {str(module.id): module for module in _department_modules().all()}
    for module_id in selected_ids:
        module = allowed_by_id.get(module_id)
        if not module:
            continue
        assignment = LecturerAssignment.query.filter_by(
            lecturer_id=user.id, module_id=module.id
        ).first()
        if assignment is None:
            assignment = LecturerAssignment(
                lecturer_id=user.id, module_id=module.id, status="approved"
            )
            db.session.add(assignment)
        assignment.status = "approved"
        assignment.reviewed_by_id = current_user.id
        assignment.reviewed_at = utcnow()

    notify(
        user,
        "lecturer_approved",
        "Your lecturer account is approved",
        "You can now sign in, review the published curriculum and claim the modules you teach.",
        target_url="/lecturer/workspace",
    )
    record_audit(
        "lecturer.approved", "User", target_id=user.id, target_label=user.full_name,
        department_id=item.department_id,
    )
    db.session.commit()
    flash(f"{user.full_name} is now an approved lecturer.", "success")
    return redirect(url_for("department.lecturer_requests"))


@department_bp.route("/lecturer-requests/<int:user_id>/reject", methods=["POST"])
def reject_lecturer(user_id):
    item = _request_or_404(user_id)
    if item.status == "pending":
        item.status = "rejected"
        item.reviewed_by_id = current_user.id
        item.reviewed_at = utcnow()
        item.user.account_status = "rejected"
        item.user.is_active_account = False
        notify(
            item.user,
            "lecturer_request_rejected",
            "Lecturer registration update",
            "Your lecturer registration was not approved. Contact your department for clarification.",
        )
        record_audit(
            "lecturer.rejected", "User", target_id=item.user.id,
            target_label=item.user.full_name, department_id=item.department_id,
        )
        db.session.commit()
        flash("Lecturer request rejected. The applicant cannot access the lecturer workspace.", "info")
    return redirect(url_for("department.lecturer_requests"))


@department_bp.route("/lecturers")
def lecturers():
    items = (
        User.query.filter_by(department_id=_department().id, role="lecturer")
        .order_by(User.is_active_account.desc(), User.full_name.asc())
        .all()
    )
    assignment_counts = {
        lecturer.id: LecturerAssignment.query.filter_by(
            lecturer_id=lecturer.id, status="approved"
        ).count()
        for lecturer in items
    }
    return render_template(
        "department/lecturers.html",
        department=_department(),
        lecturers=items,
        assignment_counts=assignment_counts,
    )


@department_bp.route("/lecturers/<int:user_id>/deactivate", methods=["POST"])
def deactivate_lecturer(user_id):
    lecturer = _department_lecturer(user_id)
    if not lecturer.is_active_account:
        flash("That lecturer is already inactive.", "info")
        return redirect(url_for("department.lecturers"))
    lecturer.is_active_account = False
    lecturer.account_status = "deactivated"
    lecturer.deactivated_at = utcnow()
    lecturer.deactivated_by_id = current_user.id
    notify(
        lecturer,
        "account_deactivated",
        "Lecturer account deactivated",
        "Your lecturer access has been paused by the department. Existing academic content remains preserved.",
    )
    record_audit(
        "lecturer.deactivated", "User", target_id=lecturer.id,
        target_label=lecturer.full_name, department_id=_department().id,
    )
    db.session.commit()
    flash(f"{lecturer.full_name} has been deactivated without deleting academic records.", "success")
    return redirect(url_for("department.lecturers"))


@department_bp.route("/lecturers/<int:user_id>/reactivate", methods=["POST"])
def reactivate_lecturer(user_id):
    lecturer = _department_lecturer(user_id)
    lecturer.is_active_account = True
    lecturer.account_status = "active"
    lecturer.deactivated_at = None
    lecturer.deactivated_by_id = None
    notify(
        lecturer,
        "account_reactivated",
        "Lecturer account reactivated",
        "Your lecturer access has been restored. Your approved modules and content are available again.",
        target_url="/lecturer/workspace",
    )
    record_audit(
        "lecturer.reactivated", "User", target_id=lecturer.id,
        target_label=lecturer.full_name, department_id=_department().id,
    )
    db.session.commit()
    flash(f"{lecturer.full_name} can access the lecturer workspace again.", "success")
    return redirect(url_for("department.lecturers"))


@department_bp.route("/curriculum")
def curriculum():
    department = _department()
    modules = _department_modules().filter(Module.publication_status != "archived").all()
    return render_template("department/curriculum.html", department=department, modules=modules,
                           live=svc.live_version())


# ---------------------------------------------------------------------------
# Prospectus: the source of truth for every module
# ---------------------------------------------------------------------------

@department_bp.route("/prospectus", methods=["GET", "POST"])
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
            return redirect(url_for("department.prospectus"))
        if version.status == "published":
            flash("Prospectus published. Every module now follows it.", "success")
        else:
            flash("Nothing was published. Fix the problems below and upload again.", "error")
        return redirect(url_for("department.prospectus_result", version_id=version.id))
    versions = CurriculumVersion.query.order_by(CurriculumVersion.created_at.desc()).limit(20).all()
    return render_template("department/prospectus.html", department=_department(),
                           live=svc.live_version(), versions=versions)


@department_bp.route("/prospectus/<int:version_id>")
def prospectus_result(version_id):
    version = db.get_or_404(CurriculumVersion, version_id)
    report = version.allocation_report
    changes = [item for item in report.get("modules", []) if item["outcome"] != "new"]
    return render_template("department/prospectus_result.html", department=_department(),
                           version=version, report=report, changes=changes,
                           outline=svc.version_outline(version), notes=svc.grouped_notes(version))


@department_bp.route("/prospectus/undo", methods=["POST"])
def prospectus_undo():
    try:
        version = svc.undo_last_publish(user=current_user)
        db.session.commit()
    except svc.CurriculumWorkflowError as error:
        db.session.rollback()
        flash(str(error), "error")
        return redirect(url_for("department.prospectus"))
    flash(f"Undone. “{version.label}” is no longer live.", "success")
    return redirect(url_for("department.prospectus"))


@department_bp.route("/prospectus/<int:version_id>/file")
def prospectus_file(version_id):
    import io
    from flask import send_file
    from storage_backend import StorageError, read_file_bytes

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
# Student academic context (until SOMA is connected)
# ---------------------------------------------------------------------------

def _department_student(user_id):
    return User.query.filter_by(id=user_id, role="student", department_id=_department().id).first_or_404()


@department_bp.route("/students")
def students():
    query_text = (request.args.get("q") or "").strip()
    query = User.query.filter_by(role="student", department_id=_department().id)
    if query_text:
        like = f"%{query_text}%"
        query = query.filter(db.or_(User.full_name.ilike(like), User.registration_number.ilike(like),
                                    User.email.ilike(like)))
    return render_template("department/students.html", department=_department(),
                           students=query.order_by(User.full_name).limit(100).all(), query=query_text)


@department_bp.route("/students/<int:user_id>", methods=["GET", "POST"])
def student_context(user_id):
    student = _department_student(user_id)
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
        return redirect(url_for("department.student_context", user_id=user_id))

    registered_ids = {row.module_id for row in StudentModuleRegistration.query.filter_by(student_id=student.id)}
    modules = []
    if student.programme_id:
        modules = (Module.query.join(Semester).join(NtaLevel)
                   .filter(NtaLevel.programme_id == student.programme_id, Module.publication_status == "published")
                   .order_by(NtaLevel.level_number, Semester.semester_number, Module.display_order).all())
    return render_template(
        "department/student_context.html", department=_department(), student=student,
        programmes=Programme.query.filter_by(is_active=True)
        .order_by(Programme.department_id, Programme.display_order).all(),
        academic_years=AcademicYear.query.order_by(AcademicYear.label.desc()).all(),
        modules=modules, registered_ids=registered_ids, context=academic_context.describe_context(student),
        sync_logs=IntegrationSyncLog.query.filter_by(student_id=student.id)
        .order_by(IntegrationSyncLog.created_at.desc()).limit(10).all(),
    )


@department_bp.route("/students/<int:user_id>/soma-sync", methods=["POST"])
def student_soma_sync(user_id):
    student = _department_student(user_id)
    result = academic_context.sync_from_soma(student, triggered_by=current_user)
    db.session.commit()
    flash(result["message"], "success" if result["ok"] else "warning")
    return redirect(url_for("department.student_context", user_id=user_id))


@department_bp.route("/module-claims")
def module_claims():
    claims = (
        LecturerAssignment.query.join(Module).join(Semester).join(NtaLevel).join(Programme)
        .filter(Programme.department_id == _department().id)
        .order_by(
            case((LecturerAssignment.status == "pending", 0), else_=1),
            LecturerAssignment.created_at.desc(),
        )
        .all()
    )
    return render_template(
        "department/module_claims.html", department=_department(), claims=claims
    )


def _department_claim(claim_id):
    claim = (
        LecturerAssignment.query.join(Module).join(Semester).join(NtaLevel).join(Programme)
        .filter(
            LecturerAssignment.id == claim_id,
            Programme.department_id == _department().id,
        )
        .first()
    )
    if claim is None:
        abort(404)
    return claim


@department_bp.route("/module-claims/<int:claim_id>/approve", methods=["POST"])
def approve_claim(claim_id):
    claim = _department_claim(claim_id)
    if not claim.lecturer.is_active_account:
        flash("Reactivate this lecturer before approving module access.", "error")
        return redirect(url_for("department.module_claims"))
    claim.status = "approved"
    claim.reviewed_by_id = current_user.id
    claim.reviewed_at = utcnow()
    claim.rejection_reason = None
    notify(
        claim.lecturer,
        "module_claim_approved",
        f"Module claim approved: {claim.module.code or claim.module.name}",
        f"You can now manage {claim.module.name}, its topics, resources and private student questions.",
        target_url="/lecturer/workspace",
    )
    record_audit(
        "module_claim.approved", "LecturerAssignment", target_id=claim.id,
        target_label=f"{claim.lecturer.full_name} · {claim.module.name}",
        department_id=_department().id,
    )
    db.session.commit()
    flash("Module claim approved.", "success")
    return redirect(url_for("department.module_claims"))


@department_bp.route("/module-claims/<int:claim_id>/reject", methods=["POST"])
def reject_claim(claim_id):
    claim = _department_claim(claim_id)
    claim.status = "rejected"
    claim.reviewed_by_id = current_user.id
    claim.reviewed_at = utcnow()
    claim.rejection_reason = (request.form.get("reason") or "").strip()[:500] or None
    notify(
        claim.lecturer,
        "module_claim_rejected",
        f"Module claim update: {claim.module.code or claim.module.name}",
        claim.rejection_reason or "Your module claim was not approved. Contact the HOD if clarification is needed.",
        target_url="/lecturer/workspace",
    )
    record_audit(
        "module_claim.rejected", "LecturerAssignment", target_id=claim.id,
        target_label=f"{claim.lecturer.full_name} · {claim.module.name}",
        department_id=_department().id,
    )
    db.session.commit()
    flash("Module claim rejected.", "info")
    return redirect(url_for("department.module_claims"))


@department_bp.route("/audit-log")
def audit_log():
    rows = (
        AuditLog.query.filter_by(department_id=_department().id)
        .order_by(AuditLog.created_at.desc())
        .limit(250)
        .all()
    )
    return render_template(
        "department/audit_log.html", department=_department(), audit_logs=rows
    )
