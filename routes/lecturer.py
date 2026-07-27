"""
routes/lecturer.py
-------------------
Lecturer-facing routes: dashboard, the upload wizard (which mirrors the
student's curriculum navigation step for step), and resource management
(list, search/filter, edit, delete).
"""

import os

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, abort, current_app, session,
)
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename

from extensions import db
from models import (
    Department, Programme, Module, Resource, ResourceChunk,
    RESOURCE_TYPES, RESOURCE_TYPE_KEYS, LecturerAssignment, Topic, Announcement,
)
from learning import lecturer_insights
from curriculum import (
    get_department_or_404, get_programme_or_404, get_level_or_404, get_semester_or_404,
    get_module_or_404, build_breadcrumbs, coming_soon_response,
)
from utils import (
    allowed_file, build_stored_filename, looks_like_claimed_type,
    human_filesize, safe_resource_mime_type,
)
from file_processing import process_resource_text
from storage_backend import StorageError, delete_resource_file, stage_uploaded_file

lecturer_bp = Blueprint("lecturer", __name__, url_prefix="/lecturer")


@lecturer_bp.before_request
@login_required
def require_lecturer_role():
    if not current_user.is_lecturer:
        abort(403)


def _all_modules_for_filters():
    return [assignment.module for assignment in _assignments()]


def _assignments():
    return LecturerAssignment.query.filter_by(lecturer_id=current_user.id).all()


def _assigned_module(module_id):
    return (LecturerAssignment.query.filter_by(
        lecturer_id=current_user.id, module_id=module_id
    ).first() is not None)


def _workspace_module():
    module_id = session.get("lecturer_module_id")
    if not module_id or not _assigned_module(module_id):
        return None
    return db.session.get(Module, module_id)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@lecturer_bp.route("/dashboard")
def dashboard():
    module = _workspace_module()
    if module is None:
        return redirect(url_for("lecturer.workspace"))
    base = Resource.query.filter_by(module_id=module.id)
    total = base.count()
    verified = base.filter_by(verification_status="verified").count()
    pending = total - verified
    recent = base.order_by(Resource.created_at.desc()).limit(6).all()
    days = request.args.get("days", 30, type=int)
    if days not in (7, 30, 90):
        days = 30
    return render_template(
        "lecturer/dashboard.html", module=module, total=total, verified=verified, pending=pending,
        recent=recent, insights=lecturer_insights(module, days=days), days=days,
    )


@lecturer_bp.route("/workspace", methods=["GET", "POST"])
def workspace():
    """A deliberate, scoped onboarding flow for the lecturer module workspace."""
    assignments = _assignments()
    if request.method == "POST":
        module_id = request.form.get("module_id", type=int)
        if not module_id or not _assigned_module(module_id):
            flash("That module is not assigned to your lecturer account.", "error")
            return redirect(url_for("lecturer.workspace"))
        session["lecturer_module_id"] = module_id
        flash("Your Control Engineering workspace is ready.", "success")
        return redirect(url_for("lecturer.dashboard"))
    return render_template("lecturer/workspace.html", assignments=assignments)


@lecturer_bp.route("/workspace/clear", methods=["POST"])
def clear_workspace():
    session.pop("lecturer_module_id", None)
    return redirect(url_for("lecturer.workspace"))


# ---------------------------------------------------------------------------
# Upload wizard: Department -> Programme -> NTA Level -> Semester -> Module
# ---------------------------------------------------------------------------

@lecturer_bp.route("/upload")
def upload_departments():
    depts = Department.query.order_by(Department.display_order).all()
    return render_template("lecturer/upload_departments.html", departments=depts)


@lecturer_bp.route("/upload/<dept_slug>")
def upload_programmes(dept_slug):
    department = get_department_or_404(dept_slug)
    if not department.is_active:
        return coming_soon_response(
            "lecturer", department.name, "department", url_for("lecturer.upload_departments")
        )
    progs = Programme.query.filter_by(department_id=department.id).order_by(
        Programme.display_order
    ).all()
    breadcrumbs = build_breadcrumbs("lecturer", department=department)
    return render_template(
        "lecturer/upload_programmes.html", department=department, programmes=progs,
        breadcrumbs=breadcrumbs,
    )


@lecturer_bp.route("/upload/<dept_slug>/<prog_slug>")
def upload_levels(dept_slug, prog_slug):
    department = get_department_or_404(dept_slug)
    programme = get_programme_or_404(department, prog_slug)
    if not (department.is_active and programme.is_active):
        return coming_soon_response(
            "lecturer", programme.name, "programme",
            url_for("lecturer.upload_programmes", dept_slug=department.slug),
        )
    breadcrumbs = build_breadcrumbs("lecturer", department=department, programme=programme)
    return render_template(
        "lecturer/upload_levels.html", department=department, programme=programme,
        levels=programme.nta_levels, breadcrumbs=breadcrumbs,
    )


@lecturer_bp.route("/upload/<dept_slug>/<prog_slug>/level/<int:level_number>")
def upload_semesters(dept_slug, prog_slug, level_number):
    department = get_department_or_404(dept_slug)
    programme = get_programme_or_404(department, prog_slug)
    level = get_level_or_404(programme, level_number)
    if not (department.is_active and programme.is_active):
        return coming_soon_response(
            "lecturer", programme.name, "programme",
            url_for("lecturer.upload_programmes", dept_slug=department.slug),
        )
    if not level.is_active:
        return coming_soon_response(
            "lecturer", level.label, "NTA level",
            url_for("lecturer.upload_levels", dept_slug=department.slug, prog_slug=programme.slug),
        )
    breadcrumbs = build_breadcrumbs(
        "lecturer", department=department, programme=programme, level=level
    )
    return render_template(
        "lecturer/upload_semesters.html", department=department, programme=programme,
        level=level, semesters=level.semesters, breadcrumbs=breadcrumbs,
    )


@lecturer_bp.route("/upload/<dept_slug>/<prog_slug>/level/<int:level_number>/semester/<int:semester_number>")
def upload_modules(dept_slug, prog_slug, level_number, semester_number):
    department = get_department_or_404(dept_slug)
    programme = get_programme_or_404(department, prog_slug)
    level = get_level_or_404(programme, level_number)
    semester = get_semester_or_404(level, semester_number)

    if not (department.is_active and programme.is_active and level.is_active):
        return coming_soon_response(
            "lecturer", level.label, "NTA level",
            url_for("lecturer.upload_levels", dept_slug=department.slug, prog_slug=programme.slug),
        )
    if not semester.is_active:
        return coming_soon_response(
            "lecturer", semester.label, "semester",
            url_for("lecturer.upload_semesters", dept_slug=department.slug, prog_slug=programme.slug,
                     level_number=level.level_number),
        )

    breadcrumbs = build_breadcrumbs(
        "lecturer", department=department, programme=programme, level=level, semester=semester
    )
    modules_list = sorted(semester.modules, key=lambda m: m.display_order)
    resource_counts = {m.id: len(m.resources) for m in modules_list}
    return render_template(
        "lecturer/upload_module_pick.html", department=department, programme=programme,
        level=level, semester=semester, modules=modules_list, resource_counts=resource_counts,
        breadcrumbs=breadcrumbs,
    )


@lecturer_bp.route("/upload/module/<int:module_id>", methods=["GET", "POST"])
def upload_form(module_id):
    module = get_module_or_404(module_id)
    if not _assigned_module(module.id):
        abort(403)
    semester = module.semester
    level = semester.nta_level
    programme = level.programme
    department = programme.department

    if not (department.is_active and programme.is_active and level.is_active and semester.is_active):
        return coming_soon_response(
            "lecturer", module.name, "module", url_for("lecturer.upload_departments")
        )

    if request.method == "POST":
        return _handle_upload_post(module)

    breadcrumbs = build_breadcrumbs(
        "lecturer", department=department, programme=programme, level=level,
        semester=semester, module=module,
    )
    return render_template(
        "lecturer/upload_form.html", module=module, department=department, programme=programme,
        level=level, semester=semester, resource_types=RESOURCE_TYPES, breadcrumbs=breadcrumbs,
    )


def _handle_upload_post(module):
    title = (request.form.get("title") or "").strip()
    description = (request.form.get("description") or "").strip()
    resource_type = request.form.get("resource_type") or "other"
    external_url = (request.form.get("external_url") or "").strip()
    verified = bool(request.form.get("verified"))
    upload_file = request.files.get("file")

    if resource_type not in RESOURCE_TYPE_KEYS:
        resource_type = "other"

    redirect_back = redirect(url_for("lecturer.upload_form", module_id=module.id))

    if not title:
        flash("Please provide a title for this resource.", "error")
        return redirect_back

    has_file = bool(upload_file and upload_file.filename)
    if not has_file and not external_url:
        flash("Please upload a file or provide an external link.", "error")
        return redirect_back

    if external_url and not (external_url.startswith("http://") or external_url.startswith("https://")):
        flash("External links must start with http:// or https://", "error")
        return redirect_back

    stored_filename = original_filename = None
    file_size = mime_type = None
    ext = None

    if has_file:
        if not allowed_file(upload_file.filename, current_app.config["ALLOWED_RESOURCE_EXTENSIONS"]):
            flash("Unsupported file type. Allowed formats: PDF, DOCX, PPTX, TXT, MD, MP4, WEBM.", "error")
            return redirect_back
        ext = upload_file.filename.rsplit(".", 1)[1].lower()
        if not looks_like_claimed_type(upload_file, ext):
            flash("This file's contents don't match its extension. Please check the file "
                  "and try again.", "error")
            return redirect_back

        stored_filename = build_stored_filename(upload_file.filename)
        original_filename = secure_filename(upload_file.filename) or upload_file.filename
        try:
            mime_type = safe_resource_mime_type(upload_file.filename)
            save_path = stage_uploaded_file(upload_file, stored_filename, mime_type)
        except (StorageError, OSError):
            current_app.logger.exception("Resource upload could not be persisted.")
            flash("The resource could not be stored. Please try again in a moment.", "error")
            return redirect_back
        try:
            file_size = os.path.getsize(save_path)
        except OSError:
            delete_resource_file(stored_filename)
            flash("The uploaded file could not be verified after saving. Please try again.", "error")
            return redirect_back

    try:
        resource = Resource(
            module_id=module.id,
            title=title,
            description=description,
            resource_type=resource_type,
            stored_filename=stored_filename,
            original_filename=original_filename,
            file_size_bytes=file_size,
            mime_type=mime_type,
            external_url=external_url or None,
            uploaded_by_id=current_user.id,
            verification_status="verified" if verified else "pending",
        )
        db.session.add(resource)
        db.session.flush()

        if stored_filename:
            chunks, status = process_resource_text(save_path, ext)
            resource.text_extraction_status = status
            for idx, chunk in enumerate(chunks):
                db.session.add(ResourceChunk(resource_id=resource.id, chunk_index=idx, content=chunk))
        elif external_url:
            resource.text_extraction_status = "not_applicable"
            db.session.add(ResourceChunk(
                resource_id=resource.id, chunk_index=0, content=f"{title}. {description}".strip()
            ))
        else:
            resource.text_extraction_status = "not_applicable"

        db.session.commit()
    except Exception:
        db.session.rollback()
        if stored_filename:
            delete_resource_file(stored_filename)
        current_app.logger.exception("Could not publish resource for module %s", module.id)
        flash("We could not publish that resource. Nothing was saved; please try again.", "error")
        return redirect_back
    flash(f"\u201c{title}\u201d has been published to {module.name}.", "success")
    return redirect(url_for("lecturer.manage_resources"))


# ---------------------------------------------------------------------------
# Module content workspace
# ---------------------------------------------------------------------------

@lecturer_bp.route("/module/<int:module_id>/content", methods=["GET", "POST"])
def module_content(module_id):
    if not _assigned_module(module_id):
        abort(403)
    module = get_module_or_404(module_id)
    if request.method == "POST":
        action = request.form.get("action")
        if action == "topic":
            title = (request.form.get("title") or "").strip()
            if not title:
                flash("A curriculum topic needs a title.", "error")
            else:
                next_order = (db.session.query(db.func.max(Topic.display_order))
                              .filter_by(module_id=module.id).scalar() or 0) + 1
                db.session.add(Topic(
                    module_id=module.id, title=title,
                    unit_label=(request.form.get("unit_label") or "").strip() or None,
                    learning_outcome=(request.form.get("learning_outcome") or "").strip() or None,
                    description=(request.form.get("description") or "").strip() or None,
                    display_order=next_order,
                ))
                db.session.commit()
                flash("Curriculum topic added to the module knowledge base.", "success")
        elif action == "announcement":
            title = (request.form.get("title") or "").strip()
            body = (request.form.get("body") or "").strip()
            if not title or not body:
                flash("An announcement needs both a title and message.", "error")
            else:
                db.session.add(Announcement(
                    module_id=module.id, author_id=current_user.id, title=title, body=body,
                    is_pinned=bool(request.form.get("is_pinned")),
                ))
                db.session.commit()
                flash("Announcement published to students in this module.", "success")
        return redirect(url_for("lecturer.module_content", module_id=module.id))

    return render_template(
        "lecturer/module_content.html", module=module,
        topics=Topic.query.filter_by(module_id=module.id).order_by(Topic.display_order).all(),
        announcements=Announcement.query.filter_by(module_id=module.id).order_by(
            Announcement.is_pinned.desc(), Announcement.created_at.desc()).all(),
        resources=Resource.query.filter_by(module_id=module.id).order_by(Resource.created_at.desc()).all(),
    )


@lecturer_bp.route("/module/<int:module_id>/topics/<int:topic_id>/delete", methods=["POST"])
def delete_topic(module_id, topic_id):
    if not _assigned_module(module_id):
        abort(403)
    topic = Topic.query.filter_by(id=topic_id, module_id=module_id).first_or_404()
    db.session.delete(topic)
    db.session.commit()
    flash("Curriculum topic removed.", "info")
    return redirect(url_for("lecturer.module_content", module_id=module_id))


@lecturer_bp.route("/module/<int:module_id>/announcements/<int:announcement_id>/delete", methods=["POST"])
def delete_announcement(module_id, announcement_id):
    if not _assigned_module(module_id):
        abort(403)
    announcement = Announcement.query.filter_by(id=announcement_id, module_id=module_id).first_or_404()
    db.session.delete(announcement)
    db.session.commit()
    flash("Announcement removed.", "info")
    return redirect(url_for("lecturer.module_content", module_id=module_id))


# ---------------------------------------------------------------------------
# Resource management
# ---------------------------------------------------------------------------

@lecturer_bp.route("/resources")
def manage_resources():
    module_ids = [assignment.module_id for assignment in _assignments()]
    query = Resource.query.filter(Resource.module_id.in_(module_ids))

    search = (request.args.get("q") or "").strip()
    if search:
        like = f"%{search}%"
        query = query.filter(db.or_(Resource.title.ilike(like), Resource.description.ilike(like)))

    module_id = request.args.get("module_id", type=int)
    if module_id:
        query = query.filter_by(module_id=module_id)

    resource_type = request.args.get("type") or ""
    if resource_type in RESOURCE_TYPE_KEYS:
        query = query.filter_by(resource_type=resource_type)

    status = request.args.get("status") or ""
    if status in ("verified", "pending"):
        query = query.filter_by(verification_status=status)

    resources = query.order_by(Resource.created_at.desc()).all()

    return render_template(
        "lecturer/manage_resources.html",
        resources=resources, modules=_all_modules_for_filters(), resource_types=RESOURCE_TYPES,
        search=search, selected_module_id=module_id, selected_type=resource_type,
        selected_status=status,
    )


@lecturer_bp.route("/resources/<int:resource_id>/edit", methods=["GET", "POST"])
def edit_resource(resource_id):
    resource = Resource.query.get_or_404(resource_id)
    if not _assigned_module(resource.module_id):
        abort(403)

    if request.method == "POST":
        title = (request.form.get("title") or "").strip()
        description = (request.form.get("description") or "").strip()
        resource_type = request.form.get("resource_type") or resource.resource_type
        external_url = (request.form.get("external_url") or "").strip()
        verified = bool(request.form.get("verified"))
        new_file = request.files.get("file")

        if not title:
            flash("Title cannot be empty.", "error")
            return redirect(url_for("lecturer.edit_resource", resource_id=resource.id))

        if external_url and not external_url.startswith(("http://", "https://")):
            flash("External links must start with http:// or https://", "error")
            return redirect(url_for("lecturer.edit_resource", resource_id=resource.id))

        resource.title = title
        resource.description = description
        if resource_type in RESOURCE_TYPE_KEYS:
            resource.resource_type = resource_type
        resource.verification_status = "verified" if verified else "pending"
        old_filename_to_delete = None

        stored_filename = None
        try:
            if new_file and new_file.filename:
                if not allowed_file(new_file.filename, current_app.config["ALLOWED_RESOURCE_EXTENSIONS"]):
                    flash("Unsupported file type. Allowed formats: PDF, DOCX, PPTX, TXT, MD, MP4, WEBM.", "error")
                    return redirect(url_for("lecturer.edit_resource", resource_id=resource.id))
                ext = new_file.filename.rsplit(".", 1)[1].lower()
                if not looks_like_claimed_type(new_file, ext):
                    flash("This file's contents don't match its extension. Please check the "
                          "file and try again.", "error")
                    return redirect(url_for("lecturer.edit_resource", resource_id=resource.id))

                stored_filename = build_stored_filename(new_file.filename)
                original_filename = secure_filename(new_file.filename) or new_file.filename
                mime_type = safe_resource_mime_type(new_file.filename)
                save_path = stage_uploaded_file(new_file, stored_filename, mime_type)

                old_filename_to_delete = resource.stored_filename
                ResourceChunk.query.filter_by(resource_id=resource.id).delete()

                resource.stored_filename = stored_filename
                resource.original_filename = original_filename
                resource.file_size_bytes = os.path.getsize(save_path)
                resource.mime_type = mime_type
                resource.external_url = None

                chunks, status = process_resource_text(save_path, ext)
                resource.text_extraction_status = status
                for idx, chunk in enumerate(chunks):
                    db.session.add(ResourceChunk(resource_id=resource.id, chunk_index=idx, content=chunk))

            elif external_url and not resource.stored_filename:
                resource.external_url = external_url
                ResourceChunk.query.filter_by(resource_id=resource.id).delete()
                db.session.add(ResourceChunk(
                    resource_id=resource.id, chunk_index=0, content=f"{title}. {description}".strip()
                ))

            db.session.commit()
        except (OSError, StorageError, ValueError):
            db.session.rollback()
            if stored_filename:
                delete_resource_file(stored_filename)
            current_app.logger.exception("Could not update resource %s", resource.id)
            flash("The resource could not be updated. Your existing version is unchanged; please try again.", "error")
            return redirect(url_for("lecturer.edit_resource", resource_id=resource.id))
        except Exception:
            db.session.rollback()
            if stored_filename:
                delete_resource_file(stored_filename)
            current_app.logger.exception("Unexpected resource update failure for %s", resource.id)
            flash("The resource could not be updated. Your existing version is unchanged; please try again.", "error")
            return redirect(url_for("lecturer.edit_resource", resource_id=resource.id))
        if old_filename_to_delete:
            delete_resource_file(old_filename_to_delete)
        flash("Resource updated successfully.", "success")
        return redirect(url_for("lecturer.manage_resources"))

    return render_template(
        "lecturer/edit_resource.html", resource=resource, resource_types=RESOURCE_TYPES,
    )


@lecturer_bp.route("/resources/<int:resource_id>/delete", methods=["POST"])
def delete_resource(resource_id):
    resource = Resource.query.get_or_404(resource_id)
    if not _assigned_module(resource.module_id):
        abort(403)

    title = resource.title
    stored_filename = resource.stored_filename

    db.session.delete(resource)
    db.session.commit()
    if stored_filename:
        delete_resource_file(stored_filename)
    flash(f"\u201c{title}\u201d has been deleted.", "info")
    return redirect(url_for("lecturer.manage_resources"))
