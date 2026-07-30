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
from models import Department, Programme, NtaLevel, Semester, Module, Resource, LecturerAssignment
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
    abort(403)


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
         "academic_year": m.academic_year.label if m.academic_year else None,
         "publication_status": m.publication_status,
         "resource_count": sum(
             1 for resource in m.resources
             if not current_user.is_student or resource.is_verified
         )}
        for m in query.order_by(Module.display_order).all()
    ])


def _resource_summary(r):
    return {
        "id": r.id,
        "title": r.title,
        "description": r.description,
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
        sources=[{"id": r.id, "title": r.title, "type": r.type_label} for r in result["sources"]],
    )
