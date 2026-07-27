"""
routes/student.py
------------------
Student-facing routes: dashboard, curriculum navigation (Department ->
Programme -> NTA Level -> Semester -> Module), resource viewing/downloading,
and download history. The AI Learning Assistant lives in routes/ai.py.
"""

from collections import OrderedDict

from flask import (
    Blueprint, render_template, redirect, url_for, flash, abort,
    current_app, request,
)
from flask_login import login_required, current_user
from markupsafe import escape

import ai_engine
from extensions import db
from models import (
    Department, Programme, NtaLevel, Semester, Module, Resource, ResourceView, Download,
    RESOURCE_TYPES, Topic, Announcement, StudentPreference, SavedItem, LearningEvent,
)
from learning import (
    personal_recommendations, record_learning_event, student_insights,
    student_profile_summary,
)
from curriculum import (
    get_department_or_404, get_programme_or_404, get_level_or_404, get_semester_or_404,
    get_module_or_404, build_breadcrumbs, coming_soon_response,
)
from storage_backend import StorageError, read_file_text, send_resource_file

student_bp = Blueprint("student", __name__)


@student_bp.before_request
@login_required
def require_student_role():
    if not current_user.is_student:
        abort(403)


def _student_resource_or_404(resource_id):
    """Return a resource that has been approved for student viewing.

    Lecturer resources are deliberately staged as ``pending`` until the
    lecturer verifies them. Keeping this check in one place prevents a direct
    URL, download link, bookmark request, or preview route from bypassing the
    verification workflow.
    """
    resource = Resource.query.filter_by(
        id=resource_id, verification_status="verified"
    ).first_or_404()
    if not _student_can_access_module(resource.module):
        abort(403)
    return resource


def _student_can_access_module(module):
    """Keep exploration inside the learner's Electrical Engineering programme."""
    if not module:
        return False
    programme = module.semester.nta_level.programme
    if current_user.programme_id:
        return programme.id == current_user.programme_id
    return bool(current_user.department_id and programme.department_id == current_user.department_id)


def _own_department_or_403(department):
    if current_user.department_id and department.id != current_user.department_id:
        abort(403)


def _own_programme_or_403(programme):
    _own_department_or_403(programme.department)
    if current_user.programme_id and programme.id != current_user.programme_id:
        abort(403)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@student_bp.route("/dashboard")
def dashboard():
    recent_views = (
        ResourceView.query.filter_by(student_id=current_user.id)
        .order_by(ResourceView.viewed_at.desc())
        .limit(12)
        .all()
    )
    seen_ids = set()
    continue_learning = []
    for v in recent_views:
        if v.resource_id in seen_ids or v.resource is None:
            continue
        seen_ids.add(v.resource_id)
        continue_learning.append(v)
        if len(continue_learning) >= 4:
            break

    download_rows = (
        Download.query.filter_by(student_id=current_user.id)
        .order_by(Download.downloaded_at.desc())
        .limit(12)
        .all()
    )
    seen_dl = set()
    recent_downloads = []
    for d in download_rows:
        if d.resource_id in seen_dl or d.resource is None:
            continue
        seen_dl.add(d.resource_id)
        recent_downloads.append(d)
        if len(recent_downloads) >= 4:
            break

    already_seen = seen_ids | seen_dl
    recommended = [
        item for item in personal_recommendations(current_user)
        if item["resource"].id not in already_seen
    ][:3]

    saved_resources = [
        item.resource for item in SavedItem.query.filter_by(student_id=current_user.id)
        .order_by(SavedItem.created_at.desc()).limit(4).all()
        if item.resource is not None and item.resource.is_verified
    ]
    saved_count = SavedItem.query.filter_by(student_id=current_user.id).count()
    all_viewed_ids = {
        row.resource_id for row in ResourceView.query.filter_by(student_id=current_user.id).all()
    }
    all_downloaded_ids = {
        row.resource_id for row in Download.query.filter_by(student_id=current_user.id).all()
    }
    ai_questions = LearningEvent.query.filter_by(
        student_id=current_user.id, event_type="ai_question"
    ).count()
    dashboard_metrics = {
        "resources_viewed": len(all_viewed_ids),
        "saved": saved_count,
        "downloads": len(all_downloaded_ids),
        "ai_questions": ai_questions,
    }

    return render_template(
        "student/dashboard.html",
        continue_learning=continue_learning,
        recent_downloads=recent_downloads,
        recommended=recommended,
        saved_resources=saved_resources,
        dashboard_metrics=dashboard_metrics,
        profile_summary=student_profile_summary(current_user),
    )


@student_bp.route("/learning-insights", methods=["GET", "POST"])
def learning_insights():
    modules = list(current_user.semester.modules) if current_user.semester else []
    module_id = request.values.get("module_id", type=int)
    module = next((item for item in modules if item.id == module_id), None) if module_id else None
    if module is None:
        module = next((item for item in modules if item.name == "Control Engineering"), modules[0] if modules else None)
    if module is None:
        abort(404)

    preference = StudentPreference.query.filter_by(student_id=current_user.id).first()
    if preference is None:
        preference = StudentPreference(student_id=current_user.id)
        db.session.add(preference)
        db.session.commit()
    if request.method == "POST":
        goal = request.form.get("weekly_goal_minutes", type=int)
        if goal and 15 <= goal <= 1200:
            preference.weekly_goal_minutes = goal
            db.session.commit()
            flash("Your weekly study goal has been updated.", "success")
        else:
            flash("Choose a weekly goal between 15 and 1,200 minutes.", "error")
        return redirect(url_for("student.learning_insights", module_id=module.id))

    announcements = Announcement.query.filter_by(module_id=module.id).order_by(
        Announcement.is_pinned.desc(), Announcement.created_at.desc()).limit(3).all()
    saved = SavedItem.query.filter_by(student_id=current_user.id).order_by(SavedItem.created_at.desc()).limit(5).all()
    return render_template(
        "student/learning_insights.html", module=module, modules=modules,
        insights=student_insights(current_user.id, module), preference=preference,
        announcements=announcements, saved=saved,
    )


@student_bp.route("/resource/<int:resource_id>/save", methods=["POST"])
def save_resource(resource_id):
    resource = _student_resource_or_404(resource_id)
    existing = SavedItem.query.filter_by(student_id=current_user.id, resource_id=resource.id).first()
    if existing:
        db.session.delete(existing)
        flash("Resource removed from your saved items.", "info")
    else:
        db.session.add(SavedItem(student_id=current_user.id, resource_id=resource.id, label=resource.title))
        flash("Resource saved for later review.", "success")
    db.session.commit()
    return redirect(request.referrer or url_for("student.resource_view", resource_id=resource.id))


@student_bp.route("/search")
def search():
    query = (request.args.get("q") or "").strip()
    resources = []
    modules = []
    if query:
        like = f"%{query}%"
        resource_query = Resource.query.join(Module).join(Semester).join(NtaLevel).filter(
            Resource.verification_status == "verified"
        )
        module_query = Module.query.join(Semester).join(NtaLevel)
        if current_user.programme_id:
            resource_query = resource_query.filter(NtaLevel.programme_id == current_user.programme_id)
            module_query = module_query.filter(NtaLevel.programme_id == current_user.programme_id)
        resources = resource_query.filter(db.or_(
            Resource.title.ilike(like), Resource.description.ilike(like),
        )).order_by(Resource.created_at.desc()).limit(24).all()
        modules = module_query.filter(db.or_(
            Module.name.ilike(like), Module.description.ilike(like)
        )).all()
    return render_template("student/search.html", query=query, resources=resources, modules=modules)


@student_bp.route("/topic/<int:topic_id>/study", methods=["POST"])
def study_topic(topic_id):
    topic = Topic.query.get_or_404(topic_id)
    record_learning_event(
        current_user.id, topic.module_id, "topic_study", topic_id=topic.id,
        duration_minutes=15, detail=topic.title,
    )
    db.session.commit()
    flash(f"Study activity recorded for {topic.title}.", "success")
    return redirect(url_for("student.learning_insights", module_id=topic.module_id))


# ---------------------------------------------------------------------------
# Curriculum navigation
# ---------------------------------------------------------------------------

@student_bp.route("/archive")
def departments():
    depts = Department.query.filter_by(id=current_user.department_id).all() if current_user.department_id else []
    return render_template("student/departments.html", departments=depts)


@student_bp.route("/archive/<dept_slug>")
def programmes(dept_slug):
    department = get_department_or_404(dept_slug)
    _own_department_or_403(department)
    if not department.is_active:
        return coming_soon_response(
            "student", department.name, "department", url_for("student.departments")
        )
    progs = Programme.query.filter_by(department_id=department.id).order_by(
        Programme.display_order
    ).all()
    breadcrumbs = build_breadcrumbs("student", department=department)
    return render_template(
        "student/programmes.html", department=department, programmes=progs,
        breadcrumbs=breadcrumbs,
    )


@student_bp.route("/archive/<dept_slug>/<prog_slug>")
def levels(dept_slug, prog_slug):
    department = get_department_or_404(dept_slug)
    programme = get_programme_or_404(department, prog_slug)
    _own_programme_or_403(programme)
    if not department.is_active:
        return coming_soon_response(
            "student", department.name, "department", url_for("student.departments")
        )
    if not programme.is_active:
        return coming_soon_response(
            "student", programme.name, "programme",
            url_for("student.programmes", dept_slug=department.slug),
        )
    levels_list = programme.nta_levels  # already ordered by level_number
    breadcrumbs = build_breadcrumbs("student", department=department, programme=programme)
    return render_template(
        "student/levels.html", department=department, programme=programme,
        levels=levels_list, breadcrumbs=breadcrumbs,
    )


@student_bp.route("/archive/<dept_slug>/<prog_slug>/level/<int:level_number>")
def semesters(dept_slug, prog_slug, level_number):
    department = get_department_or_404(dept_slug)
    programme = get_programme_or_404(department, prog_slug)
    _own_programme_or_403(programme)
    level = get_level_or_404(programme, level_number)
    if not (department.is_active and programme.is_active):
        return coming_soon_response(
            "student", programme.name, "programme",
            url_for("student.programmes", dept_slug=department.slug),
        )
    if not level.is_active:
        return coming_soon_response(
            "student", level.label, "NTA level",
            url_for("student.levels", dept_slug=department.slug, prog_slug=programme.slug),
        )
    breadcrumbs = build_breadcrumbs(
        "student", department=department, programme=programme, level=level
    )
    return render_template(
        "student/semesters.html", department=department, programme=programme, level=level,
        semesters=level.semesters, breadcrumbs=breadcrumbs,
    )


@student_bp.route(
    "/archive/<dept_slug>/<prog_slug>/level/<int:level_number>/semester/<int:semester_number>"
)
def modules(dept_slug, prog_slug, level_number, semester_number):
    department = get_department_or_404(dept_slug)
    programme = get_programme_or_404(department, prog_slug)
    _own_programme_or_403(programme)
    level = get_level_or_404(programme, level_number)
    semester = get_semester_or_404(level, semester_number)

    if not (department.is_active and programme.is_active and level.is_active):
        return coming_soon_response(
            "student", level.label, "NTA level",
            url_for("student.levels", dept_slug=department.slug, prog_slug=programme.slug),
        )
    if not semester.is_active or not semester.modules:
        return coming_soon_response(
            "student", semester.label, "semester",
            url_for("student.semesters", dept_slug=department.slug, prog_slug=programme.slug,
                     level_number=level.level_number),
        )

    breadcrumbs = build_breadcrumbs(
        "student", department=department, programme=programme, level=level, semester=semester
    )
    modules_list = sorted(semester.modules, key=lambda m: m.display_order)
    resource_counts = {
        m.id: sum(1 for resource in m.resources if resource.is_verified)
        for m in modules_list
    }
    return render_template(
        "student/modules.html", department=department, programme=programme, level=level,
        semester=semester, modules=modules_list, resource_counts=resource_counts,
        breadcrumbs=breadcrumbs,
    )


# ---------------------------------------------------------------------------
# Module resource listing
# ---------------------------------------------------------------------------

@student_bp.route("/module/<int:module_id>")
def module_resources(module_id):
    module = get_module_or_404(module_id)
    if not _student_can_access_module(module):
        abort(403)
    semester = module.semester
    level = semester.nta_level
    programme = level.programme
    department = programme.department

    resources = (
        Resource.query.filter_by(module_id=module.id, verification_status="verified")
        .order_by(Resource.created_at.desc())
        .all()
    )
    topics = Topic.query.filter_by(module_id=module.id).order_by(Topic.display_order).all()
    grouped = OrderedDict()
    for key, label in RESOURCE_TYPES:
        items = [r for r in resources if r.resource_type == key]
        if items:
            grouped[label] = items

    breadcrumbs = build_breadcrumbs(
        "student", department=department, programme=programme, level=level,
        semester=semester, module=module,
    )
    return render_template(
        "student/module_resources.html",
        module=module, department=department, programme=programme, level=level,
        semester=semester, grouped=grouped, topics=topics, total_count=len(resources),
        breadcrumbs=breadcrumbs,
    )


# ---------------------------------------------------------------------------
# Resource view / download
# ---------------------------------------------------------------------------

@student_bp.route("/resource/<int:resource_id>")
def resource_view(resource_id):
    resource = _student_resource_or_404(resource_id)
    db.session.add(ResourceView(student_id=current_user.id, resource_id=resource.id))
    record_learning_event(
        current_user.id, resource.module_id, "resource_study", duration_minutes=10,
        detail=resource.title,
    )
    db.session.commit()

    module = resource.module
    semester = module.semester
    level = semester.nta_level
    programme = level.programme
    department = programme.department

    breadcrumbs = build_breadcrumbs(
        "student", department=department, programme=programme, level=level,
        semester=semester, module=module,
    )
    already_downloaded = (
        Download.query.filter_by(student_id=current_user.id, resource_id=resource.id).first()
        is not None
    )
    is_saved = SavedItem.query.filter_by(student_id=current_user.id, resource_id=resource.id).first() is not None

    preview_kind = "none"
    preview_html = None
    if resource.is_external:
        preview_kind = "external"
    elif resource.stored_filename:
        ext = resource.file_extension
        if ext == "pdf":
            preview_kind = "pdf"
        elif ext in ("txt", "md"):
            try:
                raw_text = read_file_text(resource.stored_filename)
                preview_html = (
                    ai_engine.render_markdown(raw_text) if ext == "md"
                    else f"<pre>{escape(raw_text)}</pre>"
                )
                preview_kind = "text"
            except (OSError, StorageError, FileNotFoundError):
                preview_kind = "none"
        elif ext in ("mp4", "webm"):
            preview_kind = "video"

    return render_template(
        "student/resource_view.html", resource=resource, module=module,
        department=department, programme=programme, level=level, semester=semester,
        breadcrumbs=breadcrumbs, already_downloaded=already_downloaded,
        preview_kind=preview_kind, preview_html=preview_html, is_saved=is_saved,
    )


@student_bp.route("/resource/<int:resource_id>/file")
def resource_file(resource_id):
    resource = _student_resource_or_404(resource_id)
    if not resource.stored_filename:
        abort(404)
    try:
        return send_resource_file(resource)
    except FileNotFoundError:
        abort(404)
    except StorageError:
        current_app.logger.exception("Unable to retrieve resource %s", resource.id)
        abort(503)


@student_bp.route("/resource/<int:resource_id>/download")
def resource_download(resource_id):
    resource = _student_resource_or_404(resource_id)
    if not resource.stored_filename:
        flash("This is an external reference link, not a downloadable file. Use "
              "\u201cOpen External Resource\u201d instead.", "info")
        return redirect(url_for("student.resource_view", resource_id=resource.id))

    db.session.add(Download(student_id=current_user.id, resource_id=resource.id))
    record_learning_event(
        current_user.id, resource.module_id, "resource_download", duration_minutes=2,
        detail=resource.title,
    )
    db.session.commit()

    try:
        return send_resource_file(resource, as_attachment=True)
    except FileNotFoundError:
        current_app.logger.error("Missing file on disk for resource %s", resource.id)
        flash("This file could not be found on the server. Please contact your lecturer.", "error")
        return redirect(url_for("student.resource_view", resource_id=resource.id))
    except StorageError:
        current_app.logger.exception("Unable to retrieve resource %s", resource.id)
        flash("The file service is temporarily unavailable. Please try again shortly.", "error")
        return redirect(url_for("student.resource_view", resource_id=resource.id))


@student_bp.route("/downloads")
def downloads():
    records = (
        Download.query.filter_by(student_id=current_user.id)
        .order_by(Download.downloaded_at.desc())
        .all()
    )
    seen = set()
    unique_downloads = []
    for d in records:
        if d.resource_id in seen or d.resource is None:
            continue
        seen.add(d.resource_id)
        unique_downloads.append(d)
    return render_template("student/downloads.html", downloads=unique_downloads)
