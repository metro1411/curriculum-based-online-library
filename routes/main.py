"""
routes/main.py
---------------
Public landing page and the shared "Coming Soon" view used whenever a
student or lecturer reaches a curriculum node that isn't part of the
active learning pathway yet.
"""

from flask import Blueprint, render_template, redirect, url_for
from flask_login import current_user

import ai_engine

main_bp = Blueprint("main", __name__)


@main_bp.route("/")
def landing():
    if current_user.is_authenticated:
        return redirect(url_for("student.dashboard") if current_user.is_student
                         else url_for("lecturer.dashboard"))
    return render_template("landing.html", ai_available=ai_engine.is_available())


@main_bp.route("/about")
def about():
    return render_template("about.html")
