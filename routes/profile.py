"""Private account settings, goals, low-data preference and profile photo handling."""

import io
import mimetypes
import secrets

from flask import Blueprint, abort, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required

from extensions import db
from models import StudentPreference
from storage_backend import StorageError, delete_resource_file, read_file_bytes, stage_uploaded_file


profile_bp = Blueprint("profile", __name__, url_prefix="/profile")
_ALLOWED_PHOTO_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
_MAX_PHOTO_SIZE = 2 * 1024 * 1024


def _preference():
    preference = StudentPreference.query.filter_by(student_id=current_user.id).first()
    if preference is None:
        preference = StudentPreference(student_id=current_user.id)
        db.session.add(preference)
    return preference


@profile_bp.route("", methods=["GET", "POST"])
@login_required
def settings():
    preference = _preference() if current_user.is_student else None
    if request.method == "POST":
        action = request.form.get("action")
        if action == "password":
            current_password = request.form.get("current_password") or ""
            new_password = request.form.get("new_password") or ""
            confirm_password = request.form.get("confirm_password") or ""
            if not current_user.check_password(current_password):
                flash("Your current password is incorrect.", "error")
            elif len(new_password) < 12:
                flash("Use a new password of at least 12 characters.", "error")
            elif new_password != confirm_password:
                flash("Password confirmation does not match.", "error")
            else:
                current_user.set_password(new_password)
                db.session.commit()
                flash("Your password has been updated.", "success")
            return redirect(url_for("profile.settings"))

        full_name = " ".join((request.form.get("full_name") or "").split())
        if not 3 <= len(full_name) <= 150:
            flash("Enter your full name.", "error")
            return redirect(url_for("profile.settings"))
        current_user.full_name = full_name
        if preference:
            weekly_goal = request.form.get("weekly_goal_minutes", type=int) or preference.weekly_goal_minutes
            if not 15 <= weekly_goal <= 1200:
                flash("Choose a weekly goal between 15 and 1,200 minutes.", "error")
                return redirect(url_for("profile.settings"))
            preference.weekly_goal_minutes = weekly_goal
            preference.learning_goal = (request.form.get("learning_goal") or "").strip()[:220] or None
            preference.data_saver = bool(request.form.get("data_saver"))
            reminder_frequency = request.form.get("reminder_frequency") or "daily"
            if reminder_frequency not in {"daily", "weekly", "off"}:
                reminder_frequency = "daily"
            preference.reminder_frequency = reminder_frequency
            preference.optional_emails = bool(request.form.get("optional_emails"))
            preference.goal_reminders = bool(request.form.get("goal_reminders"))

        photo = request.files.get("profile_photo")
        if photo and photo.filename:
            extension = photo.filename.rsplit(".", 1)[-1].lower() if "." in photo.filename else ""
            if extension not in _ALLOWED_PHOTO_EXTENSIONS:
                flash("Use a JPG, PNG or WebP profile photo.", "error")
                return redirect(url_for("profile.settings"))
            # Browser uploads do not consistently expose content_length. Measure
            # the stream as well so oversized profile images are always rejected.
            photo.stream.seek(0, 2)
            uploaded_size = photo.stream.tell()
            photo.stream.seek(0)
            if uploaded_size > _MAX_PHOTO_SIZE:
                flash("Profile photos must be 2 MB or smaller.", "error")
                return redirect(url_for("profile.settings"))
            stored_filename = f"profile-{current_user.id}-{secrets.token_hex(10)}.{extension}"
            try:
                stage_uploaded_file(photo, stored_filename, mimetypes.guess_type(photo.filename)[0] or "image/jpeg")
            except StorageError:
                flash("Your profile photo could not be saved right now. Please try again.", "error")
                return redirect(url_for("profile.settings"))
            previous = current_user.profile_photo_filename
            current_user.profile_photo_filename = stored_filename
            current_user.profile_photo_mime_type = mimetypes.guess_type(photo.filename)[0] or "image/jpeg"
            if previous:
                delete_resource_file(previous)

        db.session.commit()
        flash("Your profile and learning preferences have been saved.", "success")
        return redirect(url_for("profile.settings"))
    return render_template("profile/settings.html", preference=preference)


@profile_bp.route("/photo")
@login_required
def photo():
    if not current_user.profile_photo_filename:
        abort(404)
    try:
        content = read_file_bytes(current_user.profile_photo_filename)
    except (FileNotFoundError, StorageError):
        abort(404)
    response = send_file(io.BytesIO(content), mimetype=current_user.profile_photo_mime_type or "image/jpeg",
                         conditional=True, max_age=3600)
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response
