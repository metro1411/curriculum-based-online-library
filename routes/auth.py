"""Secure registration and role-aware authentication for the learning hub."""

from urllib.parse import urlparse

from flask import Blueprint, render_template, request, redirect, url_for, flash, session, current_app
from flask_login import login_user, logout_user, login_required, current_user

from extensions import db
from models import Department, Programme, NtaLevel, Semester, User, LecturerRequest, utcnow


auth_bp = Blueprint("auth", __name__)

ROLE_LABELS = {
    "student": "Student",
    "lecturer": "Lecturer",
    "department_head": "Head of Department",
}


def _safe_next(target):
    """Only allow redirecting to a same-site relative path."""
    if not target:
        return None
    parsed = urlparse(target)
    if parsed.netloc or parsed.scheme or not target.startswith("/") or target.startswith("//"):
        return None
    return target


def _role_for_registration_number(value):
    """Classify a 8-10 digit DIT identity without trusting user-selected roles."""
    if not value or not value.isdigit() or not 8 <= len(value) <= 10:
        return None
    if value.startswith("2403"):
        return "student"
    if value.startswith("1403"):
        return "lecturer"
    if value.startswith("5000"):
        return "department_head"
    return None


def _destination(user):
    if user.is_department_head:
        return url_for("department.dashboard")
    if user.is_lecturer:
        return url_for("lecturer.dashboard")
    return url_for("student.dashboard")


def _login_user_from_form(*, required_role=None, template="auth/login.html"):
    identifier = (request.form.get("identifier") or "").strip().lower()
    password = request.form.get("password") or ""
    remember = bool(request.form.get("remember"))
    next_url = _safe_next(request.form.get("next"))

    user = User.query.filter(
        db.or_(
            User.email == identifier,
            User.username == identifier,
            User.registration_number == identifier,
        )
    ).first()
    if required_role and user and user.role != required_role:
        user = None

    if user and user.check_password(password):
        if user.account_status == "pending":
            flash("Your lecturer account is waiting for Head of Department approval.", "warning")
        elif user.account_status == "rejected":
            flash("This account request was not approved. Please contact the department.", "error")
        elif not user.is_active_account:
            flash("This account is not active. Please contact the department.", "error")
        else:
            login_user(user, remember=remember)
            session.permanent = remember
            flash(f"Welcome back, {user.full_name.split()[0]}!", "success")
            return redirect(next_url or _destination(user))
    else:
        flash("Incorrect ID number, email or password. Please try again.", "error")

    return render_template(template, next=request.form.get("next") or request.args.get("next", ""))


def _registration_options():
    departments = Department.query.filter_by(is_active=True).order_by(Department.display_order).all()
    programmes = Programme.query.join(Department).filter(Department.is_active.is_(True)).order_by(
        Programme.display_order
    ).all()
    levels = NtaLevel.query.join(Programme).join(Department).filter(
        Department.is_active.is_(True)
    ).order_by(NtaLevel.level_number).all()
    semesters = Semester.query.join(NtaLevel).join(Programme).join(Department).filter(
        Department.is_active.is_(True)
    ).order_by(Semester.semester_number).all()
    return departments, programmes, levels, semesters


def _valid_student_placement():
    department_id = request.form.get("department_id", type=int)
    programme_id = request.form.get("programme_id", type=int)
    level_id = request.form.get("nta_level_id", type=int)
    semester_id = request.form.get("semester_id", type=int)
    department = db.session.get(Department, department_id) if department_id else None
    programme = db.session.get(Programme, programme_id) if programme_id else None
    level = db.session.get(NtaLevel, level_id) if level_id else None
    semester = db.session.get(Semester, semester_id) if semester_id else None
    if not all((department, programme, level, semester)):
        return None, "Choose your department, programme, NTA level and semester."
    if not department.is_active or programme.department_id != department.id:
        return None, "Choose an active programme within your department."
    if level.programme_id != programme.id or semester.nta_level_id != level.id:
        return None, "Your selected level and semester do not match your programme."
    return (department, programme, level, semester), None


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    """One login entry point that routes a verified role to its own workspace."""
    if current_user.is_authenticated:
        return redirect(_destination(current_user))
    if request.method == "POST":
        return _login_user_from_form()
    return render_template("auth/login.html", next=request.args.get("next", ""))


@auth_bp.route("/student/login", methods=["GET", "POST"])
def student_login():
    if current_user.is_authenticated:
        return redirect(_destination(current_user))
    if request.method == "POST":
        return _login_user_from_form(required_role="student", template="auth/student_login.html")
    return render_template("auth/student_login.html", next=request.args.get("next", ""))


@auth_bp.route("/lecturer/login", methods=["GET", "POST"])
def lecturer_login():
    if current_user.is_authenticated:
        return redirect(_destination(current_user))
    if request.method == "POST":
        return _login_user_from_form(required_role="lecturer", template="auth/lecturer_login.html")
    return render_template("auth/lecturer_login.html", next=request.args.get("next", ""))


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    """Register with a DIT-number rule; lecturer and HOD privileges stay protected."""
    if current_user.is_authenticated:
        return redirect(_destination(current_user))
    departments, programmes, levels, semesters = _registration_options()
    if request.method == "POST":
        registration_number = "".join((request.form.get("registration_number") or "").split())
        role = _role_for_registration_number(registration_number)
        full_name = " ".join((request.form.get("full_name") or "").split())
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        password_confirm = request.form.get("password_confirm") or ""

        if role is None:
            flash("Use an 8–10 digit ID beginning with 2403 (student), 1403 (lecturer), or 5000 (Head of Department).", "error")
        elif not 3 <= len(full_name) <= 150:
            flash("Enter your full name.", "error")
        elif "@" not in email or len(email) > 150:
            flash("Enter a valid email address.", "error")
        elif len(password) < 12:
            flash("Use a password of at least 12 characters.", "error")
        elif password != password_confirm:
            flash("Password confirmation does not match.", "error")
        elif User.query.filter(db.or_(User.registration_number == registration_number, User.email == email)).first():
            flash("An account already uses that ID number or email address.", "error")
        else:
            department = None
            programme = level = semester = None
            if role == "student":
                placement, placement_error = _valid_student_placement()
                if placement_error:
                    flash(placement_error, "error")
                    return render_template("auth/register.html", departments=departments, programmes=programmes,
                                           levels=levels, semesters=semesters)
                department, programme, level, semester = placement
            elif role == "lecturer":
                department = db.session.get(Department, request.form.get("department_id", type=int))
                if not department or not department.is_active:
                    flash("Choose the Electrical Engineering department for your request.", "error")
                    return render_template("auth/register.html", departments=departments, programmes=programmes,
                                           levels=levels, semesters=semesters)
            else:
                department = next((item for item in departments if item.slug == "electrical-engineering"), None)
                activation_code = request.form.get("hod_activation_code") or ""
                if department is None:
                    flash("The Electrical Engineering department has not been set up yet. Contact the system owner.", "error")
                    return render_template("auth/register.html", departments=departments, programmes=programmes,
                                           levels=levels, semesters=semesters)
                existing_head = User.query.filter_by(role="department_head", department_id=department.id).first()
                if not current_app.config["HOD_ACTIVATION_CODE"] or activation_code != current_app.config["HOD_ACTIVATION_CODE"]:
                    flash("A valid protected Head of Department activation code is required.", "error")
                    return render_template("auth/register.html", departments=departments, programmes=programmes,
                                           levels=levels, semesters=semesters)
                if existing_head:
                    flash("A Head of Department account is already active for Electrical Engineering.", "error")
                    return render_template("auth/register.html", departments=departments, programmes=programmes,
                                           levels=levels, semesters=semesters)

            user = User(
                full_name=full_name,
                email=email,
                username=registration_number,
                registration_number=registration_number,
                role=role,
                account_status="pending" if role == "lecturer" else "active",
                is_active_account=role != "lecturer",
                department_id=department.id if department else None,
                programme_id=programme.id if programme else None,
                nta_level_id=level.id if level else None,
                semester_id=semester.id if semester else None,
            )
            user.set_password(password)
            db.session.add(user)
            db.session.flush()
            if role == "lecturer":
                db.session.add(LecturerRequest(
                    user_id=user.id,
                    department_id=department.id,
                    teaching_interest=(request.form.get("teaching_interest") or "").strip()[:300] or None,
                    note=(request.form.get("request_note") or "").strip()[:1500] or None,
                ))
            db.session.commit()

            if role == "lecturer":
                flash("Your lecturer request has been submitted. You can sign in after Head of Department approval.", "success")
            elif role == "department_head":
                flash("Department Head account activated securely. Please sign in.", "success")
            else:
                flash("Your student account is ready. Please sign in to personalise your learning space.", "success")
            return redirect(url_for("auth.login"))

    return render_template("auth/register.html", departments=departments, programmes=programmes,
                           levels=levels, semesters=semesters)


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    flash("You have been logged out.", "info")
    return redirect(url_for("main.landing"))
