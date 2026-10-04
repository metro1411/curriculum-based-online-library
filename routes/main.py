"""
routes/main.py
---------------
Public landing page and the shared "Coming Soon" view used whenever a
student or lecturer reaches a curriculum node that isn't part of the
active learning pathway yet.
"""

from flask import Blueprint, current_app, render_template, redirect, send_from_directory, url_for
from flask_login import current_user

import ai_engine

main_bp = Blueprint("main", __name__)


@main_bp.route("/")
def landing():
    if current_user.is_authenticated:
        if current_user.is_department_head:
            return redirect(url_for("department.dashboard"))
        return redirect(url_for("student.dashboard") if current_user.is_student
                         else url_for("lecturer.dashboard"))
    return render_template("landing.html", ai_available=ai_engine.is_available())


@main_bp.route("/about")
def about():
    return render_template("about.html")


@main_bp.route("/sw.js")
def service_worker():
    """Served from the site root so the worker can control every page."""
    response = send_from_directory(current_app.static_folder, "sw.js", mimetype="text/javascript", max_age=0)
    response.headers["Cache-Control"] = "no-cache"
    return response


@main_bp.route("/offline")
def offline():
    """Generic page the service worker shows when the network is unreachable."""
    return render_template("offline.html")
