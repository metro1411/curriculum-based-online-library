"""Electrical Engineering Head of Department workspace and approval workflow."""

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from extensions import db
from learning import department_insights
from models import LecturerAssignment, LecturerRequest, Module, Semester, NtaLevel, Programme, User, utcnow


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
    item = LecturerRequest.query.filter_by(user_id=user_id, department_id=_department().id).first()
    if item is None:
        abort(404)
    return item


@department_bp.route("")
@department_bp.route("/dashboard")
def dashboard():
    department = _department()
    requests = (LecturerRequest.query.filter_by(department_id=department.id, status="pending")
                .order_by(LecturerRequest.created_at.asc()).all())
    lecturers = (User.query.filter_by(department_id=department.id, role="lecturer")
                 .order_by(User.full_name.asc()).all())
    modules = (Module.query.join(Semester).join(NtaLevel).join(Programme)
               .filter(Programme.department_id == department.id).order_by(Module.display_order).all())
    return render_template(
        "department/dashboard.html",
        department=department,
        pending_requests=requests,
        lecturers=lecturers,
        modules=modules,
        insights=department_insights(department.id),
    )


@department_bp.route("/lecturer-requests")
def lecturer_requests():
    department = _department()
    requests = (LecturerRequest.query.filter_by(department_id=department.id)
                .order_by(LecturerRequest.status.asc(), LecturerRequest.created_at.asc()).all())
    modules = (Module.query.join(Semester).join(NtaLevel).join(Programme)
               .filter(Programme.department_id == department.id).order_by(Module.display_order).all())
    return render_template("department/lecturer_requests.html", department=department,
                           lecturer_requests=requests, modules=modules)


@department_bp.route("/lecturer-requests/<int:user_id>/approve", methods=["POST"])
def approve_lecturer(user_id):
    item = _request_or_404(user_id)
    if item.status != "pending":
        flash("This lecturer request has already been reviewed.", "warning")
        return redirect(url_for("department.lecturer_requests"))

    selected_ids = {value for value in request.form.getlist("module_ids") if value.isdigit()}
    allowed_modules = (Module.query.join(Semester).join(NtaLevel).join(Programme)
                       .filter(Programme.department_id == _department().id).all())
    allowed_by_id = {str(module.id): module for module in allowed_modules}
    for module_id in selected_ids:
        module = allowed_by_id.get(module_id)
        if module and not LecturerAssignment.query.filter_by(
            lecturer_id=item.user_id, module_id=module.id
        ).first():
            db.session.add(LecturerAssignment(lecturer_id=item.user_id, module_id=module.id))

    user = item.user
    user.account_status = "active"
    user.is_active_account = True
    item.status = "approved"
    item.reviewed_by_id = current_user.id
    item.reviewed_at = utcnow()
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
        db.session.commit()
        flash("Lecturer request rejected. The applicant cannot access the lecturer workspace.", "info")
    return redirect(url_for("department.lecturer_requests"))
