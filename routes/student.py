"""
routes/student.py
------------------
Student-facing routes: dashboard, curriculum navigation (Department ->
Programme -> NTA Level -> Semester -> Module), resource viewing/downloading,
and download history. The AI Learning Assistant lives in routes/ai.py.
"""

from collections import OrderedDict
import secrets

from flask import (
    Blueprint, render_template, redirect, url_for, flash, abort,
    current_app, request,
)
from flask_login import login_required, current_user
from markupsafe import escape

import academic_context
import ai_engine
import announcements
from extensions import db
from models import (
    Department, Programme, NtaLevel, Semester, Module, Resource, ResourceView, Download,
    RESOURCE_TYPES, Topic, StudentPreference, SavedItem, LearningEvent,
    LecturerAssignment, AcademicQuestion, QUESTION_STATUSES, ResourceStudySession, User, utcnow,
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
from academic_activity import is_academic_question
from notifications import notify

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
    if not module.is_published:
        return False
    # Registered modules (e.g. a carried-over module from another semester)
    # are always part of the learner's curriculum context.
    if academic_context.is_registered(current_user, module):
        return True
    if current_user.programme_id and programme.id != current_user.programme_id:
        return False
    if current_user.semester_id and module.semester_id != current_user.semester_id:
        return False
    if (
        current_user.academic_year_id
        and module.academic_year_id
        and module.academic_year_id != current_user.academic_year_id
    ):
        return False
    return bool(
        current_user.programme_id
        or (current_user.department_id and programme.department_id == current_user.department_id)
    )


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
    # Count in the database instead of loading every row: this page is the
    # first thing students open, often on mobile data.
    viewed_count = db.session.query(db.func.count(db.distinct(ResourceView.resource_id))).filter(
        ResourceView.student_id == current_user.id
    ).scalar()
    downloaded_count = db.session.query(db.func.count(db.distinct(Download.resource_id))).filter(
        Download.student_id == current_user.id
    ).scalar()
    academic_questions = LearningEvent.query.filter(
        LearningEvent.student_id == current_user.id,
        LearningEvent.event_type.in_(["academic_ai_question", "lecturer_question"]),
    ).count()
    dashboard_metrics = {
        "resources_viewed": viewed_count,
        "saved": saved_count,
        "downloads": downloaded_count,
        "ai_questions": academic_questions,
    }

    return render_template(
        "student/dashboard.html",
        latest_announcements=announcements.for_modules(
            [module.id for module in _accessible_semester_modules()], limit=3
        ),
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
    modules = [module for module in modules if _student_can_access_module(module)]
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

    saved = SavedItem.query.filter_by(student_id=current_user.id).order_by(SavedItem.created_at.desc()).limit(5).all()
    return render_template(
        "student/learning_insights.html", module=module, modules=modules,
        insights=student_insights(current_user.id, module), preference=preference,
        announcements=announcements.for_modules([module.id], limit=3), saved=saved,
    )


def _accessible_semester_modules():
    modules = list(current_user.semester.modules) if current_user.semester else []
    return [module for module in modules if _student_can_access_module(module)]


@student_bp.route("/announcements")
def announcements_page():
    modules = _accessible_semester_modules()
    module_ids = [module.id for module in modules]
    selected_module_id = request.args.get("module_id", type=int)
    if selected_module_id not in module_ids:
        selected_module_id = None
    items = announcements.for_modules([selected_module_id] if selected_module_id else module_ids)
    # Opening the page is what "reading" an announcement means.
    announcements.mark_read_for(current_user)
    db.session.commit()
    return render_template(
        "student/announcements.html", modules=modules,
        selected_module_id=selected_module_id, announcements=items,
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
    topic = Topic.query.filter_by(id=topic_id, is_published=True).first_or_404()
    if not _student_can_access_module(topic.module):
        abort(403)
    record_learning_event(
        current_user.id, topic.module_id, "topic_study", topic_id=topic.id,
        duration_minutes=0, detail=topic.title,
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
    modules_list = sorted(
        [module for module in semester.modules if _student_can_access_module(module)],
        key=lambda m: (m.module_type != "core", m.display_order),
    )
    if not modules_list:
        return coming_soon_response(
            "student", semester.label, "semester",
            url_for("student.semesters", dept_slug=department.slug, prog_slug=programme.slug,
                    level_number=level.level_number),
        )
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
    topics = Topic.query.filter_by(
        module_id=module.id, is_published=True
    ).order_by(Topic.display_order, Topic.id).all()
    grouped = OrderedDict()
    for key, label in RESOURCE_TYPES:
        items = [r for r in resources if r.resource_type == key]
        if items:
            grouped[label] = items
    topic_groups = []
    assigned_resource_ids = set()
    for topic in topics:
        albums = OrderedDict()
        for key, label in RESOURCE_TYPES:
            items = [
                resource for resource in resources
                if resource.topic_id == topic.id and resource.resource_type == key
            ]
            if items:
                albums[label] = items
                assigned_resource_ids.update(resource.id for resource in items)
        topic_groups.append({"topic": topic, "albums": albums})
    unassigned_albums = OrderedDict()
    for key, label in RESOURCE_TYPES:
        items = [
            resource for resource in resources
            if resource.id not in assigned_resource_ids and resource.resource_type == key
        ]
        if items:
            unassigned_albums[label] = items

    breadcrumbs = build_breadcrumbs(
        "student", department=department, programme=programme, level=level,
        semester=semester, module=module,
    )
    return render_template(
        "student/module_resources.html",
        module=module, department=department, programme=programme, level=level,
        semester=semester, grouped=grouped, topics=topics, total_count=len(resources),
        topic_groups=topic_groups, unassigned_albums=unassigned_albums,
        breadcrumbs=breadcrumbs,
        module_announcements=announcements.for_modules([module.id], limit=3),
    )


# ---------------------------------------------------------------------------
# Resource view / download
# ---------------------------------------------------------------------------

@student_bp.route("/resource/<int:resource_id>")
def resource_view(resource_id):
    resource = _student_resource_or_404(resource_id)
    db.session.add(ResourceView(student_id=current_user.id, resource_id=resource.id))
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
        study_start_url=url_for("student.start_resource_study", resource_id=resource.id),
        study_heartbeat_template=url_for(
            "student.heartbeat_resource_study", token="STUDY_TOKEN"
        ),
    )


@student_bp.route("/resource/<int:resource_id>/study/start", methods=["POST"])
def start_resource_study(resource_id):
    resource = _student_resource_or_404(resource_id)
    session_row = ResourceStudySession(
        token=secrets.token_urlsafe(32),
        student_id=current_user.id,
        resource_id=resource.id,
        module_id=resource.module_id,
        last_heartbeat_at=utcnow(),
    )
    db.session.add(session_row)
    db.session.commit()
    return {
        "ok": True,
        "token": session_row.token,
        "active_seconds": session_row.active_seconds,
        "qualified": False,
    }


@student_bp.route("/resource/study/<token>/heartbeat", methods=["POST"])
def heartbeat_resource_study(token):
    session_row = ResourceStudySession.query.filter_by(
        token=token, student_id=current_user.id
    ).first_or_404()
    if session_row.qualified_at:
        return {
            "ok": True,
            "active_seconds": session_row.active_seconds,
            "qualified": True,
        }
    now = utcnow()
    last = session_row.last_heartbeat_at
    comparable_now = now
    if last and last.tzinfo is None:
        comparable_now = now.replace(tzinfo=None)
    elapsed = int((comparable_now - last).total_seconds()) if last else 0
    # The browser only sends heartbeats while visible and recently active.
    # Capping each interval prevents delayed/background requests from granting
    # unearned study time.
    credited = min(45, elapsed) if 10 <= elapsed <= 120 else 0
    session_row.active_seconds = min(600, session_row.active_seconds + credited)
    session_row.last_heartbeat_at = comparable_now
    if session_row.active_seconds >= 600 and session_row.qualified_at is None:
        session_row.qualified_at = comparable_now
        record_learning_event(
            current_user.id,
            session_row.module_id,
            "resource_study_qualified",
            duration_minutes=10,
            detail=session_row.resource.title,
            qualifies_for_streak=True,
        )
    db.session.commit()
    return {
        "ok": True,
        "active_seconds": session_row.active_seconds,
        "qualified": bool(session_row.qualified_at),
    }


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
        current_user.id, resource.module_id, "resource_download", duration_minutes=0,
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


# ---------------------------------------------------------------------------
# Anonymous student-to-lecturer questions
# ---------------------------------------------------------------------------

@student_bp.route("/questions", methods=["GET", "POST"])
def questions():
    modules = [
        module for module in (current_user.semester.modules if current_user.semester else [])
        if _student_can_access_module(module)
    ]
    module_ids = {module.id for module in modules}
    if request.method == "POST":
        module_id = request.form.get("module_id", type=int)
        module = next((item for item in modules if item.id == module_id), None)
        topic_id = request.form.get("topic_id", type=int)
        topic = Topic.query.filter_by(
            id=topic_id, module_id=module.id, is_published=True
        ).first() if module and topic_id else None
        lecturer_id = request.form.get("lecturer_id", type=int)
        approved_assignments = (
            LecturerAssignment.query.join(User, LecturerAssignment.lecturer_id == User.id)
            .filter(
                LecturerAssignment.module_id == module_id,
                LecturerAssignment.status == "approved",
                User.is_active_account.is_(True),
            )
            .all()
        ) if module else []
        assignment = next(
            (item for item in approved_assignments if item.lecturer_id == lecturer_id),
            approved_assignments[0] if len(approved_assignments) == 1 else None,
        )
        subject = " ".join((request.form.get("subject") or "").split())
        body = (request.form.get("body") or "").strip()
        if module is None:
            flash("Choose a module from your current curriculum.", "error")
        elif topic_id and topic is None:
            flash("Choose a topic that belongs to the selected module.", "error")
        elif assignment is None:
            flash("Choose an approved lecturer for this module.", "error")
        elif not 4 <= len(subject) <= 180:
            flash("Write a short, clear question subject.", "error")
        elif not 15 <= len(body) <= 4000:
            flash("Explain your academic question in 15–4,000 characters.", "error")
        elif not is_academic_question(f"{subject} {body}", has_module_context=True):
            flash("Please submit a genuine academic question related to your learning.", "error")
        else:
            item = AcademicQuestion(
                student_id=current_user.id,
                module_id=module.id,
                topic_id=topic.id if topic else None,
                lecturer_id=assignment.lecturer_id,
                subject=subject,
                body=body,
                status="new",
                academic_verified=True,
            )
            db.session.add(item)
            db.session.flush()
            record_learning_event(
                current_user.id,
                module.id,
                "lecturer_question",
                topic_id=topic.id if topic else None,
                detail=f"Private question · {topic.title if topic else module.name}",
                qualifies_for_streak=True,
            )
            notify(
                assignment.lecturer,
                "academic_question",
                f"New anonymous question in {module.code or module.name}",
                f"A student submitted private question {item.anonymous_ref}. "
                "Their identity is intentionally hidden.",
                target_url="/lecturer/questions",
            )
            db.session.commit()
            flash(
                f"Question {item.anonymous_ref} was sent privately. Your identity is hidden.",
                "success",
            )
            return redirect(url_for("student.questions", submitted=item.anonymous_ref))

    assignments = (
        LecturerAssignment.query.join(User, LecturerAssignment.lecturer_id == User.id)
        .filter(
            LecturerAssignment.module_id.in_(module_ids or [-1]),
            LecturerAssignment.status == "approved",
            User.is_active_account.is_(True),
        )
        .all()
    )
    topics = Topic.query.filter(
        Topic.module_id.in_(module_ids or [-1]), Topic.is_published.is_(True)
    ).order_by(Topic.display_order).all()
    items = (
        AcademicQuestion.query.filter_by(student_id=current_user.id)
        .order_by(AcademicQuestion.updated_at.desc())
        .all()
    )
    return render_template(
        "student/questions.html",
        questions=items,
        modules=modules,
        assignments=assignments,
        topics=topics,
        statuses=dict(QUESTION_STATUSES),
    )
