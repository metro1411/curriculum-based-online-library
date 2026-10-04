"""Head of Department curriculum, lecturer and governance workspace."""

from __future__ import annotations

import re

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import case

from extensions import db
from governance import record_audit
from learning import department_insights
from models import (
    AcademicYear, AuditLog, LecturerAssignment, LecturerRequest, Module,
    NtaLevel, Programme, Semester, User, utcnow,
)
from notifications import notify


department_bp = Blueprint("department", __name__, url_prefix="/department")
MODULE_TYPES = (("core", "Core Module"), ("general_studies", "General Studies Module"))


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


@department_bp.route("/curriculum", methods=["GET", "POST"])
def curriculum():
    department = _department()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "academic_year":
            label = (request.form.get("label") or "").strip()
            match = re.fullmatch(r"(\d{4})/(\d{4})", label)
            if not match or int(match.group(2)) != int(match.group(1)) + 1:
                flash("Use an academic year in the format 2026/2027.", "error")
            elif AcademicYear.query.filter_by(department_id=department.id, label=label).first():
                flash("That academic year already exists.", "warning")
            else:
                make_current = bool(request.form.get("is_current"))
                if make_current:
                    AcademicYear.query.filter_by(department_id=department.id).update(
                        {"is_current": False}, synchronize_session=False
                    )
                year = AcademicYear(
                    department_id=department.id,
                    label=label,
                    is_current=make_current,
                    status="active",
                    created_by_id=current_user.id,
                )
                db.session.add(year)
                db.session.flush()
                record_audit(
                    "academic_year.created", "AcademicYear", target_id=year.id,
                    target_label=year.label, department_id=department.id,
                )
                db.session.commit()
                flash(f"Academic year {label} is ready.", "success")
            return redirect(url_for("department.curriculum"))

        if action == "module":
            return _create_module(department)

    programmes = (
        Programme.query.filter_by(department_id=department.id)
        .order_by(Programme.display_order)
        .all()
    )
    years = (
        AcademicYear.query.filter_by(department_id=department.id)
        .order_by(AcademicYear.label.desc())
        .all()
    )
    modules = _department_modules().all()
    return render_template(
        "department/curriculum.html",
        department=department,
        programmes=programmes,
        academic_years=years,
        modules=modules,
        module_types=MODULE_TYPES,
    )


def _create_module(department):
    name = " ".join((request.form.get("name") or "").split())
    code = (request.form.get("code") or "").strip().upper()
    module_type = request.form.get("module_type") or ""
    description = (request.form.get("description") or "").strip()
    cohort_label = " ".join((request.form.get("cohort_label") or "").split())[:80] or None
    academic_year_id = request.form.get("academic_year_id", type=int)
    programme_id = request.form.get("programme_id", type=int)
    level_id = request.form.get("nta_level_id", type=int)
    semester_id = request.form.get("semester_id", type=int)

    year = AcademicYear.query.filter_by(
        id=academic_year_id, department_id=department.id, status="active"
    ).first()
    programme = Programme.query.filter_by(
        id=programme_id, department_id=department.id
    ).first()
    level = db.session.get(NtaLevel, level_id) if level_id else None
    semester = db.session.get(Semester, semester_id) if semester_id else None

    error = None
    if not 3 <= len(name) <= 200:
        error = "Enter a clear module name."
    elif not re.fullmatch(r"[A-Z0-9][A-Z0-9./-]{1,29}", code):
        error = "Use a module code containing 2–30 letters, numbers, dots, slashes or hyphens."
    elif module_type not in dict(MODULE_TYPES):
        error = "Choose Core Module or General Studies Module."
    elif not all((year, programme, level, semester)):
        error = "Choose a valid academic year, programme, NTA level and semester."
    elif level.programme_id != programme.id or semester.nta_level_id != level.id:
        error = "The selected NTA level and semester do not belong to that programme."
    else:
        duplicate = (
            Module.query.join(Semester).join(NtaLevel)
            .filter(
                Module.academic_year_id == year.id,
                NtaLevel.programme_id == programme.id,
                db.func.lower(Module.code) == code.lower(),
            )
            .first()
        )
        if duplicate:
            error = f"Module code {code} already exists in this programme and academic year."

    if error:
        flash(error, "error")
        return redirect(url_for("department.curriculum"))

    next_order = (
        db.session.query(db.func.max(Module.display_order))
        .filter_by(semester_id=semester.id)
        .scalar()
        or 0
    ) + 1
    module = Module(
        semester_id=semester.id,
        academic_year_id=year.id,
        name=name,
        code=code,
        module_type=module_type,
        cohort_label=cohort_label,
        description=description or None,
        publication_status="published",
        is_active=True,
        display_order=next_order,
        created_by_id=current_user.id,
        provenance="hod_manual",
    )
    db.session.add(module)
    db.session.flush()
    record_audit(
        "module.created", "Module", target_id=module.id,
        target_label=f"{module.code} · {module.name}",
        department_id=department.id,
        details={
            "academic_year": year.label,
            "programme": programme.name,
            "nta_level": level.level_number,
            "semester": semester.semester_number,
            "module_type": module.module_type,
        },
    )
    db.session.commit()
    flash(f"{code} · {name} has been published to {year.label}.", "success")
    return redirect(url_for("department.curriculum"))


@department_bp.route("/curriculum/years/<int:year_id>/current", methods=["POST"])
def set_current_year(year_id):
    department = _department()
    year = AcademicYear.query.filter_by(id=year_id, department_id=department.id).first_or_404()
    AcademicYear.query.filter_by(department_id=department.id).update(
        {"is_current": False}, synchronize_session=False
    )
    year.is_current = True
    year.status = "active"
    record_audit(
        "academic_year.set_current", "AcademicYear", target_id=year.id,
        target_label=year.label, department_id=department.id,
    )
    db.session.commit()
    flash(f"{year.label} is now the current curriculum edition.", "success")
    return redirect(url_for("department.curriculum"))


@department_bp.route("/curriculum/modules/<int:module_id>/archive", methods=["POST"])
def archive_module(module_id):
    module = _department_modules().filter(Module.id == module_id).first_or_404()
    module.publication_status = "archived"
    module.is_active = False
    record_audit(
        "module.archived", "Module", target_id=module.id,
        target_label=f"{module.code or 'Code pending'} · {module.name}",
        department_id=_department().id,
    )
    db.session.commit()
    flash("Module archived. Historical records and resources were preserved.", "success")
    return redirect(url_for("department.curriculum"))


@department_bp.route("/curriculum/modules/<int:module_id>/edit", methods=["GET", "POST"])
def edit_module(module_id):
    module = _department_modules().filter(Module.id == module_id).first_or_404()
    if request.method == "POST":
        name = " ".join((request.form.get("name") or "").split())
        code = (request.form.get("code") or "").strip().upper()
        module_type = request.form.get("module_type") or ""
        publication_status = request.form.get("publication_status") or ""
        error = None
        if not 3 <= len(name) <= 200:
            error = "Enter a clear module name."
        elif not re.fullmatch(r"[A-Z0-9][A-Z0-9./-]{1,29}", code):
            error = "Use a module code containing 2–30 letters, numbers, dots, slashes or hyphens."
        elif module_type not in dict(MODULE_TYPES):
            error = "Choose Core Module or General Studies Module."
        elif publication_status not in {"draft", "published", "archived"}:
            error = "Choose a valid publication status."
        else:
            duplicate = (
                Module.query.join(Semester).join(NtaLevel)
                .filter(
                    Module.id != module.id,
                    Module.academic_year_id == module.academic_year_id,
                    NtaLevel.programme_id == module.semester.nta_level.programme_id,
                    db.func.lower(Module.code) == code.lower(),
                )
                .first()
            )
            if duplicate:
                error = f"Module code {code} already exists in this programme and academic year."
        if error:
            flash(error, "error")
        else:
            before = {
                "name": module.name,
                "code": module.code,
                "module_type": module.module_type,
                "publication_status": module.publication_status,
            }
            module.name = name
            module.code = code
            module.module_type = module_type
            module.cohort_label = " ".join(
                (request.form.get("cohort_label") or "").split()
            )[:80] or None
            module.description = (request.form.get("description") or "").strip() or None
            module.publication_status = publication_status
            module.is_active = publication_status == "published"
            record_audit(
                "module.updated", "Module", target_id=module.id,
                target_label=f"{module.code} · {module.name}",
                department_id=_department().id,
                details={"before": before, "after": {
                    "name": module.name,
                    "code": module.code,
                    "module_type": module.module_type,
                    "publication_status": module.publication_status,
                }},
            )
            db.session.commit()
            flash("Module details updated and historical relationships preserved.", "success")
            return redirect(url_for("department.curriculum"))
    return render_template(
        "department/edit_module.html", department=_department(),
        module=module, module_types=MODULE_TYPES,
    )


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
