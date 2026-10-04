"""
routes/api.py
-------------
Versioned JSON endpoints for curriculum, resources and AI-assisted learning.
The session-protected endpoints provide a clean boundary for future
institutional integrations.
"""

from flask import Blueprint, abort, jsonify, request, url_for
from flask_login import login_required, current_user

import ai_engine
from academic_activity import is_academic_question
from extensions import db
from models import (
    CurriculumVersion, Department, Programme, NtaLevel, Semester, Module, Resource,
    LecturerAssignment, StudentModuleRegistration,
)
from learning import record_learning_event

api_bp = Blueprint("api", __name__, url_prefix="/api/v1")


def _visible_module_query():
    """Scope module data to the signed-in user's learning or teaching space."""
    if current_user.is_student:
        if not current_user.programme_id:
            return Module.query.filter(Module.id == -1)
        query = Module.query.join(Semester).join(NtaLevel).filter(
            NtaLevel.programme_id == current_user.programme_id,
            Module.is_active.is_(True),
            Module.publication_status == "published",
        )
        if current_user.semester_id:
            query = query.filter(Module.semester_id == current_user.semester_id)
        if current_user.academic_year_id:
            query = query.filter(Module.academic_year_id == current_user.academic_year_id)
        registered = _registered_module_ids()
        if registered:
            query = Module.query.filter(db.or_(
                Module.id.in_([m.id for m in query.all()]),
                db.and_(Module.id.in_(registered), Module.publication_status == "published",
                        Module.is_active.is_(True)),
            ))
        return query
    if current_user.is_lecturer:
        return Module.query.join(LecturerAssignment).filter(
            LecturerAssignment.lecturer_id == current_user.id,
            LecturerAssignment.status == "approved",
        )
    if current_user.is_department_head:
        return Module.query.join(Semester).join(NtaLevel).join(Programme).filter(
            Programme.department_id == current_user.department_id
        )
    if current_user.is_admin:
        return Module.query
    abort(403)


def _registered_module_ids():
    return [row.module_id for row in StudentModuleRegistration.query.filter_by(
        student_id=current_user.id, status="registered")]


def _visible_resource_query():
    """Scope resources and keep unverified lecturer uploads private."""
    if current_user.is_student:
        if not current_user.programme_id:
            return Resource.query.filter(Resource.id == -1)
        query = Resource.query.join(Module).join(Semester).join(NtaLevel).filter(
            NtaLevel.programme_id == current_user.programme_id,
            Resource.verification_status == "verified",
            Module.is_active.is_(True),
            Module.publication_status == "published",
        )
        if current_user.semester_id:
            query = query.filter(Module.semester_id == current_user.semester_id)
        if current_user.academic_year_id:
            query = query.filter(Module.academic_year_id == current_user.academic_year_id)
        registered = _registered_module_ids()
        if registered:
            query = Resource.query.join(Module).filter(
                Resource.verification_status == "verified",
                db.or_(Resource.id.in_([r.id for r in query.all()]),
                       db.and_(Module.id.in_(registered), Module.publication_status == "published",
                               Module.is_active.is_(True))),
            )
        return query
    if current_user.is_lecturer:
        return Resource.query.join(LecturerAssignment).filter(
            LecturerAssignment.lecturer_id == current_user.id,
            LecturerAssignment.status == "approved",
        )
    if current_user.is_department_head:
        return Resource.query.join(Module).join(Semester).join(NtaLevel).join(Programme).filter(
            Programme.department_id == current_user.department_id
        )
    if current_user.is_admin:
        return Resource.query
    abort(403)


@api_bp.route("/curriculum")
@login_required
def curriculum():
    """Full nested curriculum tree, active flags included."""
    payload = []
    departments = Department.query.order_by(Department.display_order)
    if current_user.is_student and current_user.department_id:
        departments = departments.filter(Department.id == current_user.department_id)
    elif current_user.is_department_head and current_user.department_id:
        departments = departments.filter(Department.id == current_user.department_id)
    for dept in departments.all():
        dept_json = {
            "id": dept.id, "name": dept.name, "slug": dept.slug, "is_active": dept.is_active,
            "programmes": [],
        }
        for prog in dept.programmes:
            prog_json = {
                "id": prog.id, "name": prog.name, "slug": prog.slug, "is_active": prog.is_active,
                "nta_levels": [],
            }
            for level in prog.nta_levels:
                level_json = {
                    "id": level.id, "level_number": level.level_number,
                    "is_active": level.is_active, "semesters": [],
                }
                for sem in level.semesters:
                    level_json["semesters"].append({
                        "id": sem.id, "semester_number": sem.semester_number,
                        "is_active": sem.is_active,
                        "modules": [{"id": m.id, "name": m.name, "code": m.code,
                                     "module_type": m.module_type,
                                     "academic_year": m.academic_year.label if m.academic_year else None,
                                     "publication_status": m.publication_status}
                                    for m in sem.modules],
                    })
                prog_json["nta_levels"].append(level_json)
            dept_json["programmes"].append(prog_json)
        payload.append(dept_json)
    return jsonify(departments=payload)


@api_bp.route("/programmes")
@login_required
def programmes():
    department_id = request.args.get("department_id", type=int)
    query = Programme.query
    if current_user.is_student and current_user.programme_id:
        query = query.filter(Programme.id == current_user.programme_id)
    elif current_user.is_department_head and current_user.department_id:
        query = query.filter(Programme.department_id == current_user.department_id)
    if department_id:
        query = query.filter_by(department_id=department_id)
    return jsonify(programmes=[
        {"id": p.id, "name": p.name, "slug": p.slug, "department_id": p.department_id,
         "is_active": p.is_active}
        for p in query.order_by(Programme.display_order).all()
    ])


@api_bp.route("/modules")
@login_required
def modules():
    semester_id = request.args.get("semester_id", type=int)
    query = _visible_module_query()
    if semester_id and not current_user.is_student:
        query = query.filter_by(semester_id=semester_id)
    return jsonify(modules=[
        {"id": m.id, "name": m.name, "code": m.code, "semester_id": m.semester_id,
         "module_type": m.module_type, "type_label": m.type_label,
         "credits": m.credits_label,
         "prerequisites": [{"id": p.id, "code": p.code, "name": p.name} for p in m.prerequisites],
         "curriculum_version": m.curriculum_version.label if m.curriculum_version else None,
         "provenance": m.provenance,
         "academic_year": m.academic_year.label if m.academic_year else None,
         "publication_status": m.publication_status,
         "resource_count": sum(
             1 for resource in m.resources
             if not current_user.is_student or resource.is_verified
         )}
        for m in query.order_by(Module.display_order).all()
    ])


def _resource_summary(r):
    module = r.module
    level = module.semester.nta_level
    return {
        "id": r.id,
        "title": r.title,
        "description": r.description,
        "learning_objectives": r.learning_objectives,
        "lecturer_remarks": r.lecturer_remarks,
        "topic": r.topic.title if r.topic else None,
        "curriculum": {
            "department": level.programme.department.name,
            "programme": level.programme.name,
            "nta_level": level.level_number,
            "year_label": level.year_label,
            "semester": module.semester.semester_number,
            "module": module.name,
            "module_code": module.code,
            "academic_year": module.academic_year.label if module.academic_year else None,
        },
        "resource_type": r.resource_type,
        "type_label": r.type_label,
        "module_id": r.module_id,
        "verification_status": r.verification_status,
        "is_external": r.is_external,
        "external_url": r.external_url,
        "file_size_bytes": r.file_size_bytes,
        "created_at": r.created_at.isoformat(),
        "view_url": url_for("student.resource_view", resource_id=r.id),
    }


@api_bp.route("/resources")
@login_required
def resources():
    module_id = request.args.get("module_id", type=int)
    query = _visible_resource_query()
    if module_id:
        query = query.filter_by(module_id=module_id)
    items = query.order_by(Resource.created_at.desc()).limit(200).all()
    return jsonify(resources=[_resource_summary(r) for r in items])


@api_bp.route("/resources/<int:resource_id>")
@login_required
def resource_detail(resource_id):
    r = _visible_resource_query().filter(Resource.id == resource_id).first_or_404()
    return jsonify(resource=_resource_summary(r))


@api_bp.route("/ai/ask", methods=["POST"])
@login_required
def ai_ask():
    """JSON API mirror of the interactive AI assistant, for future
    programmatic / SOMA-side integration. Session-authenticated for now."""
    if not current_user.is_student:
        return jsonify(ok=False, error="Only student accounts may use the AI assistant."), 403
    if not ai_engine.is_available():
        return jsonify(ok=False, error=ai_engine.UNAVAILABLE_MESSAGE), 503

    data = request.get_json(silent=True) or {}
    question = (data.get("question") or "").strip()
    mode = data.get("mode") or ai_engine.DEFAULT_MODE
    module_id = data.get("module_id")

    if not question:
        return jsonify(ok=False, error="A 'question' field is required."), 400
    if len(question) > 4000:
        return jsonify(ok=False, error="That question is too long. Please shorten it."), 400

    module = (
        _visible_module_query().filter(Module.id == module_id).first()
        if module_id else None
    )
    if module_id and module is None:
        return jsonify(ok=False, error="The requested module was not found."), 404
    result = ai_engine.ask(mode=mode, question=question, module=module, student=current_user)

    if not result["ok"]:
        return jsonify(ok=False, error=result["error"]), 503

    event_module = module or _visible_module_query().order_by(Module.display_order).first()
    if event_module and is_academic_question(
        question, has_module_context=bool(module)
    ):
        record_learning_event(
            current_user.id,
            event_module.id,
            "academic_ai_question",
            duration_minutes=0,
            detail=(
                f"Private academic question · {module.name}"
                if module else "Private general academic question"
            ),
            qualifies_for_streak=True,
        )
        db.session.commit()

    return jsonify(
        ok=True,
        answer_text=result["answer_text"],
        general_guidance=result["general_guidance"],
        context_label=result.get("context_label"),
        context_label_text=result.get("context_label_text"),
        curriculum_context=result.get("curriculum_context"),
        curriculum_sources=result.get("curriculum_sources", []),
        prospectus_sources=result.get("prospectus_sources", []),
        sources=[{"id": r.id, "title": r.title, "type": r.type_label} for r in result["sources"]],
    )


@api_bp.route("/me/academic-context")
@login_required
def my_academic_context():
    """The signed-in student's curriculum position and where it came from."""
    if not current_user.is_student:
        return jsonify(ok=False, error="Academic context is available for student accounts."), 403
    import academic_context
    ctx = academic_context.describe_context(current_user)
    return jsonify(
        ok=True,
        department=ctx["department"].name if ctx["department"] else None,
        programme=ctx["programme"].name if ctx["programme"] else None,
        nta_level=ctx["level"].level_number if ctx["level"] else None,
        year_label=ctx["level"].year_label if ctx["level"] else None,
        semester=ctx["semester"].semester_number if ctx["semester"] else None,
        academic_year=ctx["academic_year"].label if ctx["academic_year"] else None,
        source=ctx["source"],
        missing=ctx["missing"],
        modules_from_registration=ctx["modules_from_registration"],
        modules=[{"id": m.id, "code": m.code, "name": m.name} for m in ctx["modules"]],
    )


@api_bp.route("/me/recommendations")
@login_required
def my_recommendations():
    if not current_user.is_student:
        return jsonify(ok=False, error="Recommendations are available for student accounts."), 403
    import recommendations
    items = recommendations.curriculum_recommendations(current_user)
    return jsonify(ok=True, note=recommendations.EVIDENCE_NOTE, recommendations=recommendations.to_json(items))


@api_bp.route("/curriculum/versions")
@login_required
def curriculum_versions():
    """Administrators and HODs see every version; others see published ones only."""
    query = CurriculumVersion.query.order_by(CurriculumVersion.created_at.desc())
    if not (current_user.is_admin or current_user.is_department_head):
        query = query.filter(CurriculumVersion.status == "published")
    return jsonify(versions=[{
        "id": v.id, "label": v.label, "academic_year": v.academic_year_label,
        "status": v.status, "is_demo": v.is_demo,
        "published_at": v.published_at.isoformat() if v.published_at else None,
        "modules": Module.query.filter_by(curriculum_version_id=v.id).count(),
    } for v in query.all()])


@api_bp.route("/curriculum/versions/<int:version_id>")
@login_required
def curriculum_version_detail(version_id):
    version = db.get_or_404(CurriculumVersion, version_id)
    privileged = current_user.is_admin or current_user.is_department_head
    if not privileged and version.status != "published":
        abort(404)
    payload = {
        "id": version.id, "label": version.label, "academic_year": version.academic_year_label,
        "status": version.status, "is_demo": version.is_demo,
    }
    if current_user.is_admin:
        payload["entries"] = [{
            "id": e.id, "programme": e.programme.name if e.programme else None,
            "department": e.programme.department_name if e.programme else None,
            "nta_level": e.nta_level, "semester": e.semester_number, "module_code": e.module_code,
            "module_name": e.module_name, "credits": str(e.credits) if e.credits is not None else None,
            "prerequisites": e.prerequisite_list, "review_status": e.review_status, "origin": e.origin,
        } for e in version.entries]
    return jsonify(version=payload)
