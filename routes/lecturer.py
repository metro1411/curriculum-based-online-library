"""
routes/lecturer.py
-------------------
Lecturer-facing routes: dashboard, the upload wizard (which mirrors the
student's curriculum navigation step for step), and resource management
(list, search/filter, edit, delete).
"""

import os
from dataclasses import dataclass

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, abort, current_app, session,
)
from flask_login import login_required, current_user
from sqlalchemy import case
from werkzeug.utils import secure_filename

from extensions import db
from models import (
    Department, Programme, Module, Resource, ResourceChunk,
    RESOURCE_TYPES, RESOURCE_TYPE_KEYS, LecturerAssignment, Topic, Announcement,
    AcademicQuestion, AcademicYear, QUESTION_STATUSES, QUESTION_STATUS_KEYS,
    TOPIC_CATEGORIES, TOPIC_CATEGORY_KEYS, User, utcnow,
)
import announcements
from learning import lecturer_insights
from governance import record_audit
from notifications import notify
from curriculum import (
    get_department_or_404, get_programme_or_404, get_level_or_404, get_semester_or_404,
    get_module_or_404, build_breadcrumbs, coming_soon_response,
)
from utils import (
    allowed_file, build_stored_filename, looks_like_claimed_type,
    safe_resource_mime_type,
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
    return LecturerAssignment.query.filter_by(
        lecturer_id=current_user.id, status="approved"
    ).all()


def _assigned_module(module_id):
    return (LecturerAssignment.query.filter_by(
        lecturer_id=current_user.id, module_id=module_id, status="approved"
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
        recent=recent, insights=lecturer_insights(
            module, lecturer_id=current_user.id, days=days
        ), days=days,
    )


@lecturer_bp.route("/workspace", methods=["GET", "POST"])
def workspace():
    """A deliberate, scoped onboarding flow for the lecturer module workspace."""
    assignments = _assignments()
    if request.method == "POST":
        if request.form.get("action") == "claim":
            module_id = request.form.get("module_id", type=int)
            module = db.session.get(Module, module_id) if module_id else None
            if (
                not module
                or not module.is_published
                or not current_user.department_id
                or module.semester.nta_level.programme.department_id != current_user.department_id
            ):
                flash("Choose a published module from your department.", "error")
                return redirect(url_for("lecturer.workspace"))
            claim = LecturerAssignment.query.filter_by(
                lecturer_id=current_user.id, module_id=module.id
            ).first()
            if claim and claim.status == "approved":
                flash("You are already approved to teach that module.", "info")
                return redirect(url_for("lecturer.workspace"))
            if claim is None:
                claim = LecturerAssignment(
                    lecturer_id=current_user.id, module_id=module.id, status="pending"
                )
                db.session.add(claim)
            else:
                claim.status = "pending"
                claim.reviewed_by_id = None
                claim.reviewed_at = None
                claim.rejection_reason = None
            db.session.flush()
            heads = User.query.filter_by(
                department_id=current_user.department_id,
                role="department_head",
                is_active_account=True,
            ).all()
            for head in heads:
                notify(
                    head,
                    "module_claim",
                    f"Module claim: {module.code or module.name}",
                    f"{current_user.full_name} requested approval to teach {module.name}.",
                    target_url="/department/module-claims",
                )
            record_audit(
                "module_claim.submitted", "LecturerAssignment", target_id=claim.id,
                target_label=f"{current_user.full_name} · {module.name}",
                department_id=current_user.department_id,
            )
            db.session.commit()
            flash("Your module claim was sent to the HOD for approval.", "success")
            return redirect(url_for("lecturer.workspace"))

        module_id = request.form.get("module_id", type=int)
        if not module_id or not _assigned_module(module_id):
            flash("That module is not assigned to your lecturer account.", "error")
            return redirect(url_for("lecturer.workspace"))
        session["lecturer_module_id"] = module_id
        module = db.session.get(Module, module_id)
        destination = request.form.get("destination") or "topics"
        if destination == "dashboard":
            flash(f"{module.name} is now your active teaching module.", "success")
            return redirect(url_for("lecturer.dashboard"))
        flash(
            f"{module.name} selected. Add, arrange and publish its topics below.",
            "success",
        )
        return redirect(url_for("lecturer.module_content", module_id=module.id))
    claims = LecturerAssignment.query.filter_by(lecturer_id=current_user.id).all()
    claimed_ids = {claim.module_id for claim in claims}
    available = []
    if current_user.department_id:
        current_year = AcademicYear.query.filter_by(
            department_id=current_user.department_id, is_current=True, status="active"
        ).first()
        available = [
            module for module in Module.query.all()
            if module.is_published
            and module.id not in claimed_ids
            and module.semester.nta_level.programme.department_id == current_user.department_id
            and (not current_year or module.academic_year_id == current_year.id)
        ]
        available.sort(key=lambda module: (
            module.academic_year.label if module.academic_year else "",
            module.semester.nta_level.programme.name,
            module.semester.nta_level.level_number,
            module.semester.semester_number,
            module.display_order,
        ), reverse=True)
    return render_template(
        "lecturer/workspace.html",
        assignments=assignments,
        claims=claims,
        available_modules=available,
    )


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
        topics=Topic.query.filter_by(module_id=module.id, is_published=True)
        .order_by(Topic.display_order).all(),
    )


class ResourceFormError(Exception):
    """A lecturer-correctable problem with the resource form; the message is shown as-is."""


@dataclass
class StagedFile:
    stored_filename: str
    original_filename: str
    mime_type: str
    path: str
    size: int
    ext: str


def _read_resource_form(module_id):
    """Parse and validate the fields shared by the create and edit forms."""
    title = (request.form.get("title") or "").strip()
    external_url = (request.form.get("external_url") or "").strip()
    topic_id = request.form.get("topic_id", type=int)
    resource_type = request.form.get("resource_type")
    upload_file = request.files.get("file")

    if not title:
        raise ResourceFormError("Please provide a title for this resource.")
    if external_url and not external_url.startswith(("http://", "https://")):
        raise ResourceFormError("External links must start with http:// or https://")
    topic = None
    if topic_id:
        topic = Topic.query.filter_by(id=topic_id, module_id=module_id).first()
        if topic is None:
            raise ResourceFormError("Choose a topic that belongs to this module.")
    return {
        "title": title,
        "description": (request.form.get("description") or "").strip(),
        # None means "not a recognised type"; callers choose the fallback.
        "resource_type": resource_type if resource_type in RESOURCE_TYPE_KEYS else None,
        "topic": topic,
        "external_url": external_url,
        "verified": bool(request.form.get("verified")),
        "file": upload_file if upload_file and upload_file.filename else None,
    }


def _stage_resource_file(upload_file):
    """Validate an uploaded file and store it.

    Raises ResourceFormError for problems the lecturer can fix, and
    StorageError/OSError when storage fails. The caller deletes the staged
    file if a later step fails.
    """
    if not allowed_file(upload_file.filename, current_app.config["ALLOWED_RESOURCE_EXTENSIONS"]):
        raise ResourceFormError("Unsupported file type. Allowed formats: PDF, DOCX, PPTX, TXT, MD, MP4, WEBM.")
    ext = upload_file.filename.rsplit(".", 1)[1].lower()
    if not looks_like_claimed_type(upload_file, ext):
        raise ResourceFormError("This file's contents don't match its extension. Please check the file and try again.")

    stored_filename = build_stored_filename(upload_file.filename)
    mime_type = safe_resource_mime_type(upload_file.filename)
    path = stage_uploaded_file(upload_file, stored_filename, mime_type)
    try:
        size = os.path.getsize(path)
    except OSError:
        delete_resource_file(stored_filename)
        raise
    return StagedFile(
        stored_filename=stored_filename,
        original_filename=secure_filename(upload_file.filename) or upload_file.filename,
        mime_type=mime_type, path=path, size=size, ext=ext,
    )


def _index_resource_text(resource, staged=None):
    """Rebuild the chunks DIT AI retrieves: the file's text, or the title and
    description for an external link."""
    ResourceChunk.query.filter_by(resource_id=resource.id).delete()
    if staged:
        chunks, resource.text_extraction_status = process_resource_text(staged.path, staged.ext)
    else:
        resource.text_extraction_status = "not_applicable"
        chunks = [f"{resource.title}. {resource.description or ''}".strip()] if resource.external_url else []
    for index, chunk in enumerate(chunks):
        db.session.add(ResourceChunk(resource_id=resource.id, chunk_index=index, content=chunk))


def _handle_upload_post(module):
    redirect_back = redirect(url_for("lecturer.upload_form", module_id=module.id))
    try:
        form = _read_resource_form(module.id)
        if not form["file"] and not form["external_url"]:
            raise ResourceFormError("Please upload a file or provide an external link.")
        staged = _stage_resource_file(form["file"]) if form["file"] else None
    except ResourceFormError as error:
        flash(str(error), "error")
        return redirect_back
    except (StorageError, OSError):
        current_app.logger.exception("Resource upload could not be persisted.")
        flash("The resource could not be stored. Please try again in a moment.", "error")
        return redirect_back

    try:
        resource = Resource(
            module_id=module.id,
            topic_id=form["topic"].id if form["topic"] else None,
            title=form["title"],
            description=form["description"],
            resource_type=form["resource_type"] or "other",
            stored_filename=staged.stored_filename if staged else None,
            original_filename=staged.original_filename if staged else None,
            file_size_bytes=staged.size if staged else None,
            mime_type=staged.mime_type if staged else None,
            external_url=form["external_url"] or None,
            uploaded_by_id=current_user.id,
            verification_status="verified" if form["verified"] else "pending",
        )
        db.session.add(resource)
        db.session.flush()
        record_audit(
            "resource.created", "Resource", target_id=resource.id,
            target_label=resource.title,
            department_id=module.semester.nta_level.programme.department_id,
            details={"module_id": module.id, "resource_type": resource.resource_type},
        )
        _index_resource_text(resource, staged)
        db.session.commit()
    except Exception:
        db.session.rollback()
        if staged:
            delete_resource_file(staged.stored_filename)
        current_app.logger.exception("Could not publish resource for module %s", module.id)
        flash("We could not publish that resource. Nothing was saved; please try again.", "error")
        return redirect_back
    flash(f"\u201c{resource.title}\u201d has been published to {module.name}.", "success")
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
            title = " ".join((request.form.get("title") or "").split())[:200]
            category = request.form.get("category") or "concept"
            if not title:
                flash("Enter a topic title.", "error")
            elif category not in TOPIC_CATEGORY_KEYS:
                flash("Choose a valid topic category.", "error")
            else:
                next_order = (db.session.query(db.func.max(Topic.display_order))
                              .filter_by(module_id=module.id).scalar() or 0) + 1
                db.session.add(Topic(
                    module_id=module.id, title=title,
                    category=category,
                    unit_label=(request.form.get("unit_label") or "").strip() or None,
                    learning_outcome=(request.form.get("learning_outcome") or "").strip()[:2000] or None,
                    description=(request.form.get("description") or "").strip()[:4000] or None,
                    status=request.form.get("status") if request.form.get("status") in {
                        "planned", "currently_teaching", "completed", "revision"
                    } else "planned",
                    is_published=bool(request.form.get("is_published")),
                    display_order=next_order,
                ))
                record_audit(
                    "topic.created", "Topic", target_label=title,
                    department_id=module.semester.nta_level.programme.department_id,
                    details={"module_id": module.id, "category": category},
                )
                db.session.commit()
                flash("Topic added.", "success")
        elif action == "announcement":
            _publish_announcement_from_form(module)
        return redirect(url_for("lecturer.module_content", module_id=module.id))

    return render_template(
        "lecturer/module_content.html", module=module,
        topics=Topic.query.filter_by(module_id=module.id).order_by(
            Topic.display_order, Topic.id
        ).all(),
        topic_categories=TOPIC_CATEGORIES,
        announcements=Announcement.query.filter_by(module_id=module.id).order_by(
            Announcement.is_pinned.desc(), Announcement.created_at.desc()).all(),
        resources=Resource.query.filter_by(module_id=module.id).order_by(Resource.created_at.desc()).all(),
    )


@lecturer_bp.route("/module/<int:module_id>/topics/<int:topic_id>/delete", methods=["POST"])
def delete_topic(module_id, topic_id):
    if not _assigned_module(module_id):
        abort(403)
    topic = Topic.query.filter_by(id=topic_id, module_id=module_id).first_or_404()
    topic.is_published = False
    topic.status = "archived"
    record_audit(
        "topic.archived", "Topic", target_id=topic.id, target_label=topic.title,
        department_id=topic.module.semester.nta_level.programme.department_id,
    )
    db.session.commit()
    flash("Curriculum topic archived. Existing resources and history were preserved.", "info")
    return redirect(url_for("lecturer.module_content", module_id=module_id))


@lecturer_bp.route("/module/<int:module_id>/topics/<int:topic_id>/restore", methods=["POST"])
def restore_topic(module_id, topic_id):
    if not _assigned_module(module_id):
        abort(403)
    topic = Topic.query.filter_by(id=topic_id, module_id=module_id).first_or_404()
    topic.is_published = True
    if topic.status == "archived":
        topic.status = "planned"
    record_audit(
        "topic.restored", "Topic", target_id=topic.id, target_label=topic.title,
        department_id=topic.module.semester.nta_level.programme.department_id,
        details={"status": topic.status, "published": True},
    )
    db.session.commit()
    flash("Topic restored to the student learning path.", "success")
    return redirect(url_for("lecturer.module_content", module_id=module_id))


@lecturer_bp.route("/module/<int:module_id>/topics/<int:topic_id>/move", methods=["POST"])
def move_topic(module_id, topic_id):
    if not _assigned_module(module_id):
        abort(403)
    topic = Topic.query.filter_by(id=topic_id, module_id=module_id).first_or_404()
    direction = request.form.get("direction")
    if direction not in {"up", "down"}:
        abort(400)
    topics = Topic.query.filter_by(module_id=module_id).order_by(
        Topic.display_order, Topic.id
    ).all()
    current_index = next(index for index, item in enumerate(topics) if item.id == topic.id)
    target_index = current_index - 1 if direction == "up" else current_index + 1
    if not 0 <= target_index < len(topics):
        flash("This topic is already at the edge of the learning path.", "info")
        return redirect(url_for("lecturer.module_content", module_id=module_id))
    topics.insert(target_index, topics.pop(current_index))
    for index, item in enumerate(topics, start=1):
        item.display_order = index
    record_audit(
        "topic.reordered", "Topic", target_id=topic.id, target_label=topic.title,
        department_id=topic.module.semester.nta_level.programme.department_id,
        details={"direction": direction, "display_order": target_index + 1},
    )
    db.session.commit()
    flash("Topic order updated.", "success")
    return redirect(url_for("lecturer.module_content", module_id=module_id))


@lecturer_bp.route("/module/<int:module_id>/topics/<int:topic_id>/update", methods=["POST"])
def update_topic(module_id, topic_id):
    if not _assigned_module(module_id):
        abort(403)
    topic = Topic.query.filter_by(id=topic_id, module_id=module_id).first_or_404()
    title = " ".join((request.form.get("title") or "").split())
    status = request.form.get("status") or "planned"
    category = request.form.get("category") or "concept"
    if not title:
        flash("A topic title is required.", "error")
    elif status not in {"planned", "currently_teaching", "completed", "revision"}:
        flash("Choose a valid topic status.", "error")
    elif category not in TOPIC_CATEGORY_KEYS:
        flash("Choose a valid topic category.", "error")
    else:
        topic.title = title
        topic.category = category
        topic.unit_label = (request.form.get("unit_label") or "").strip()[:100] or None
        topic.learning_outcome = (request.form.get("learning_outcome") or "").strip()[:2000] or None
        topic.description = (request.form.get("description") or "").strip()[:4000] or None
        topic.status = status
        topic.is_published = bool(request.form.get("is_published"))
        record_audit(
            "topic.updated", "Topic", target_id=topic.id, target_label=topic.title,
            department_id=topic.module.semester.nta_level.programme.department_id,
            details={
                "category": topic.category,
                "status": topic.status,
                "published": topic.is_published,
            },
        )
        db.session.commit()
        flash("Topic details updated.", "success")
    return redirect(url_for("lecturer.module_content", module_id=module_id))


def _publish_announcement_from_form(module):
    try:
        announcement = announcements.publish(
            module, current_user,
            request.form.get("title"), request.form.get("body"),
            pinned=bool(request.form.get("is_pinned")),
            email=bool(request.form.get("send_email")),
        )
        db.session.commit()
    except announcements.AnnouncementError as error:
        db.session.rollback()
        flash(str(error), "error")
        return None
    reach = announcement.recipient_count
    flash(
        f"Announcement sent to {reach} student{'' if reach == 1 else 's'} in {module.code or module.name}.",
        "success",
    )
    return announcement


@lecturer_bp.route("/announcements", methods=["GET", "POST"])
def announcements_page():
    channels, blocked = announcements.lecturer_channels(current_user)
    modules = [channel["module"] for channel in channels]
    module_ids = [module.id for module in modules]
    if request.method == "POST":
        module_id = request.form.get("module_id", type=int)
        if module_id not in module_ids:
            flash("Choose one of your approved, published modules.", "error")
        else:
            _publish_announcement_from_form(db.session.get(Module, module_id))
        return redirect(url_for("lecturer.announcements_page", module_id=module_id or None))

    selected_module_id = request.args.get("module_id", type=int)
    shown_ids = [selected_module_id] if selected_module_id in module_ids else module_ids
    return render_template(
        "lecturer/announcements.html",
        channels=channels,
        blocked=blocked,
        modules=modules,
        selected_module_id=selected_module_id if selected_module_id in module_ids else None,
        announcements=announcements.for_modules(shown_ids),
        title_max=announcements.TITLE_MAX,
        body_max=announcements.BODY_MAX,
    )


@lecturer_bp.route("/module/<int:module_id>/announcements/<int:announcement_id>/delete", methods=["POST"])
def delete_announcement(module_id, announcement_id):
    if not _assigned_module(module_id):
        abort(403)
    announcement = Announcement.query.filter_by(id=announcement_id, module_id=module_id).first_or_404()
    announcements.remove(announcement, current_user)
    db.session.commit()
    flash("Announcement removed. Students who had not opened it will no longer see the alert.", "info")
    if request.form.get("return_to") == "announcements":
        return redirect(url_for("lecturer.announcements_page"))
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
    resource = db.get_or_404(Resource, resource_id)
    if not _assigned_module(resource.module_id):
        abort(403)

    if request.method == "POST":
        return _handle_edit_post(resource)

    return render_template(
        "lecturer/edit_resource.html", resource=resource, resource_types=RESOURCE_TYPES,
        topics=Topic.query.filter_by(module_id=resource.module_id, is_published=True)
        .order_by(Topic.display_order).all(),
    )


def _handle_edit_post(resource):
    redirect_back = redirect(url_for("lecturer.edit_resource", resource_id=resource.id))
    staged = None
    try:
        form = _read_resource_form(resource.module_id)
        if form["file"]:
            staged = _stage_resource_file(form["file"])
    except ResourceFormError as error:
        flash(str(error), "error")
        return redirect_back
    except (StorageError, OSError):
        current_app.logger.exception("Replacement file for resource %s could not be stored", resource.id)
        flash("The resource could not be updated. Your existing version is unchanged; please try again.", "error")
        return redirect_back

    replaced_filename = None
    try:
        resource.title = form["title"]
        resource.description = form["description"]
        resource.topic_id = form["topic"].id if form["topic"] else None
        resource.resource_type = form["resource_type"] or resource.resource_type
        resource.verification_status = "verified" if form["verified"] else "pending"
        if staged:
            replaced_filename = resource.stored_filename
            resource.stored_filename = staged.stored_filename
            resource.original_filename = staged.original_filename
            resource.file_size_bytes = staged.size
            resource.mime_type = staged.mime_type
            resource.external_url = None
            _index_resource_text(resource, staged)
        elif form["external_url"] and not resource.stored_filename:
            resource.external_url = form["external_url"]
            _index_resource_text(resource)
        record_audit(
            "resource.updated", "Resource", target_id=resource.id,
            target_label=resource.title,
            department_id=resource.module.semester.nta_level.programme.department_id,
            details={"resource_type": resource.resource_type, "topic_id": resource.topic_id},
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        if staged:
            delete_resource_file(staged.stored_filename)
        current_app.logger.exception("Could not update resource %s", resource.id)
        flash("The resource could not be updated. Your existing version is unchanged; please try again.", "error")
        return redirect_back
    # Remove the old file only after the new version is committed.
    if replaced_filename:
        delete_resource_file(replaced_filename)
    flash("Resource updated successfully.", "success")
    return redirect(url_for("lecturer.manage_resources"))


@lecturer_bp.route("/resources/<int:resource_id>/delete", methods=["POST"])
def delete_resource(resource_id):
    resource = db.get_or_404(Resource, resource_id)
    if not _assigned_module(resource.module_id):
        abort(403)

    title = resource.title
    stored_filename = resource.stored_filename

    db.session.delete(resource)
    record_audit(
        "resource.deleted", "Resource", target_id=resource.id, target_label=title,
        department_id=resource.module.semester.nta_level.programme.department_id,
    )
    db.session.commit()
    if stored_filename:
        delete_resource_file(stored_filename)
    flash(f"\u201c{title}\u201d has been deleted.", "info")
    return redirect(url_for("lecturer.manage_resources"))


# ---------------------------------------------------------------------------
# Anonymous student questions
# ---------------------------------------------------------------------------

@lecturer_bp.route("/questions")
def questions():
    module_ids = [assignment.module_id for assignment in _assignments()]
    query = AcademicQuestion.query.filter(
        AcademicQuestion.lecturer_id == current_user.id,
        AcademicQuestion.module_id.in_(module_ids or [-1]),
    )
    status = request.args.get("status") or ""
    if status in QUESTION_STATUS_KEYS:
        query = query.filter_by(status=status)
    module_id = request.args.get("module_id", type=int)
    if module_id in module_ids:
        query = query.filter_by(module_id=module_id)
    items = query.order_by(
        case((AcademicQuestion.status == "new", 0),
             (AcademicQuestion.status == "reviewing", 1), else_=2),
        AcademicQuestion.updated_at.desc(),
    ).all()
    status_counts = {
        key: AcademicQuestion.query.filter_by(
            lecturer_id=current_user.id, status=key
        ).count()
        for key, _label in QUESTION_STATUSES
    }
    return render_template(
        "lecturer/questions.html",
        questions=items,
        modules=_all_modules_for_filters(),
        statuses=QUESTION_STATUSES,
        selected_status=status,
        selected_module_id=module_id,
        status_counts=status_counts,
    )


@lecturer_bp.route("/questions/<int:question_id>/respond", methods=["POST"])
def respond_to_question(question_id):
    question = AcademicQuestion.query.filter_by(
        id=question_id, lecturer_id=current_user.id
    ).first_or_404()
    if not _assigned_module(question.module_id):
        abort(403)
    status = request.form.get("status") or ""
    answer = (request.form.get("answer") or "").strip()
    if status not in QUESTION_STATUS_KEYS:
        flash("Choose a valid question status.", "error")
        return redirect(url_for("lecturer.questions"))
    if status == "answered" and len(answer) < 3:
        flash("Write an answer before marking the question as answered.", "error")
        return redirect(url_for("lecturer.questions"))
    if status == "will_address_in_class" and not answer:
        answer = "Your lecturer has marked this topic for explanation in class."
    question.status = status
    question.answer = answer or question.answer
    if status in {"answered", "will_address_in_class"}:
        question.responded_at = utcnow()
    if status == "closed":
        question.closed_at = utcnow()
    notify(
        question.student,
        "academic_question_update",
        f"Update for private question {question.anonymous_ref}",
        f"Your lecturer changed the question status to “{question.status_label}”. "
        "Sign in to read the private response.",
        target_url=f"/questions#{question.anonymous_ref}",
    )
    record_audit(
        "academic_question.status_updated", "AcademicQuestion",
        target_id=question.anonymous_ref, target_label=question.subject,
        department_id=question.module.semester.nta_level.programme.department_id,
        details={"status": status},
    )
    db.session.commit()
    flash(f"{question.anonymous_ref} updated without revealing the student’s identity.", "success")
    return redirect(url_for("lecturer.questions", module_id=question.module_id))
