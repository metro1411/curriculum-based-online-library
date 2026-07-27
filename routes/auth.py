"""
routes/auth.py
---------------
Student and lecturer authentication. Two completely separate login forms
(and login routes) so the two experiences are visually and functionally
distinct, backed by a single User model with a `role` column.
"""

from urllib.parse import urlparse

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, session,
)
from flask_login import login_user, logout_user, login_required, current_user

from extensions import db
from models import User

auth_bp = Blueprint("auth", __name__)


def _safe_next(target):
    """Only allow redirecting to a same-site relative path."""
    if not target:
        return None
    parsed = urlparse(target)
    if parsed.netloc or parsed.scheme:
        return None
    if not target.startswith("/") or target.startswith("//"):
        return None
    return target


@auth_bp.route("/student/login", methods=["GET", "POST"])
def student_login():
    if current_user.is_authenticated:
        return redirect(url_for("student.dashboard") if current_user.is_student
                         else url_for("lecturer.dashboard"))

    if request.method == "POST":
        identifier = (request.form.get("identifier") or "").strip().lower()
        password = request.form.get("password") or ""
        remember = bool(request.form.get("remember"))
        next_url = _safe_next(request.form.get("next"))

        if len(password) < 8:
            flash("Enter a password of at least 8 characters.", "error")
            return render_template("auth/student_login.html", next=request.args.get("next", ""))

        user = User.query.filter(
            db.or_(User.email == identifier, User.username == identifier),
            User.role == "student",
        ).first()

        if user and user.check_password(password) and user.is_active_account:
            login_user(user, remember=remember)
            session.permanent = remember
            flash(f"Welcome back, {user.full_name.split()[0]}!", "success")
            return redirect(next_url or url_for("student.dashboard"))

        flash("Incorrect email/username or password. Please try again.", "error")

    return render_template("auth/student_login.html", next=request.args.get("next", ""))


@auth_bp.route("/lecturer/login", methods=["GET", "POST"])
def lecturer_login():
    if current_user.is_authenticated:
        return redirect(url_for("student.dashboard") if current_user.is_student
                         else url_for("lecturer.dashboard"))

    if request.method == "POST":
        identifier = (request.form.get("identifier") or "").strip().lower()
        password = request.form.get("password") or ""
        remember = bool(request.form.get("remember"))
        next_url = _safe_next(request.form.get("next"))

        if len(password) < 8:
            flash("Enter a password of at least 8 characters.", "error")
            return render_template("auth/lecturer_login.html", next=request.args.get("next", ""))

        user = User.query.filter(
            db.or_(User.email == identifier, User.username == identifier),
            User.role == "lecturer",
        ).first()

        if user and user.check_password(password) and user.is_active_account:
            login_user(user, remember=remember)
            session.permanent = remember
            flash(f"Welcome back, {user.full_name.split()[0]}!", "success")
            return redirect(next_url or url_for("lecturer.dashboard"))

        flash("Incorrect email/username or password. Please try again.", "error")

    return render_template("auth/lecturer_login.html", next=request.args.get("next", ""))


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    flash("You have been logged out.", "info")
    return redirect(url_for("main.landing"))
