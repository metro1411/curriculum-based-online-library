"""
routes/ai.py
------------
The DIT AI Learning Assistant: chat page, the AJAX "ask" endpoint, and
conversation history management. All heavy lifting (retrieval + Gemini
call + fallback) lives in ai_engine.py; this module wires it to the
student's session and the database.
"""

from flask import (
    Blueprint, render_template, request, jsonify, redirect, url_for, flash, abort,
)
from flask_login import login_required, current_user

import ai_engine
from extensions import db
from models import (
    Module, Resource, Semester, AIConversation, AIMessage, StudentPreference,
    AIAnswerFeedback, utcnow,
)
from learning import record_learning_event
from academic_activity import is_academic_question

ai_bp = Blueprint("ai", __name__, url_prefix="/ai")


@ai_bp.before_request
@login_required
def require_student_role():
    if not current_user.is_student:
        abort(403)


def _active_modules():
    if current_user.programme_id:
        modules = (Module.query.join(Semester).filter(
            Semester.nta_level.has(programme_id=current_user.programme_id),
            Module.is_active.is_(True),
            Module.publication_status == "published",
        ).order_by(Module.display_order).all())
        return [
            module for module in modules
            if (not current_user.semester_id or module.semester_id == current_user.semester_id)
            and (
                not current_user.academic_year_id
                or not module.academic_year_id
                or module.academic_year_id == current_user.academic_year_id
            )
        ]
    active_semester = Semester.query.filter_by(is_active=True).first()
    return sorted(active_semester.modules, key=lambda m: m.display_order) if active_semester else []


def _student_can_use_module(module):
    """AI context stays inside the student's Electrical Engineering programme."""
    if not module:
        return False
    if current_user.programme_id:
        return (
            module.is_published
            and module.semester.nta_level.programme_id == current_user.programme_id
            and (not current_user.semester_id or module.semester_id == current_user.semester_id)
            and (
                not current_user.academic_year_id
                or not module.academic_year_id
                or module.academic_year_id == current_user.academic_year_id
            )
        )
    return bool(current_user.semester_id and module.semester_id == current_user.semester_id)


def _sources_payload(sources):
    return [
        {
            "id": resource.id,
            "title": resource.title,
            "type": resource.type_label,
            "verified": resource.is_verified,
            "url": url_for("student.resource_view", resource_id=resource.id),
        }
        for resource in sources
    ]


def _web_sources_payload(sources):
    return [
        {"title": source["title"], "url": source["url"]}
        for source in (sources or [])
        if source.get("title") and source.get("url")
    ]


def _make_title(question):
    question = " ".join(question.split())
    if len(question) <= 60:
        return question
    return question[:60].rsplit(" ", 1)[0] + "…"


@ai_bp.route("")
def assistant():
    modules = _active_modules()
    conversations = (
        AIConversation.query.filter_by(student_id=current_user.id)
        .order_by(AIConversation.updated_at.desc())
        .all()
    )

    active_conversation = None
    conversation_id = request.args.get("conversation_id", type=int)
    if conversation_id:
        active_conversation = AIConversation.query.filter_by(
            id=conversation_id, student_id=current_user.id
        ).first()

    selected_module = None
    selected_resource = None
    selected_mode = request.args.get("mode", ai_engine.DEFAULT_MODE)

    if active_conversation:
        selected_module = active_conversation.module
        selected_mode = active_conversation.mode
    else:
        module_id = request.args.get("module_id", type=int)
        resource_id = request.args.get("resource_id", type=int)
        if resource_id:
            selected_resource = db.session.get(Resource, resource_id)
            if selected_resource and selected_resource.is_verified and _student_can_use_module(selected_resource.module):
                selected_module = selected_resource.module
                selected_mode = "ask_resource"
            else:
                selected_resource = None
        elif module_id:
            selected_module = db.session.get(Module, module_id)

    if selected_mode not in ai_engine.MODE_LABELS:
        selected_mode = ai_engine.DEFAULT_MODE

    chat_messages = []
    if active_conversation:
        for m in active_conversation.messages:
            archive_sources, web_sources = ai_engine.split_sources(m.sources_json)
            chat_messages.append({
                "id": m.id,
                "role": m.role,
                "content_html": m.content_html or ai_engine.render_markdown(m.content),
                "content": m.content,
                "sources": archive_sources,
                "web_sources": web_sources,
                "general_guidance": m.general_guidance,
            })

    preference = StudentPreference.query.filter_by(student_id=current_user.id).first()
    if preference is None:
        preference = StudentPreference(student_id=current_user.id)
        db.session.add(preference)
        db.session.commit()

    return render_template(
        "student/ai_assistant.html",
        ai_available=ai_engine.is_available(),
        modules=modules,
        modes=ai_engine.MODE_LABELS,
        conversations=conversations,
        active_conversation=active_conversation,
        selected_module=selected_module,
        selected_resource=selected_resource,
        selected_mode=selected_mode,
        chat_messages=chat_messages,
        preference=preference,
    )


@ai_bp.route("/ask", methods=["POST"])
def ask():
    if not ai_engine.is_available():
        return jsonify(ok=False, error=ai_engine.UNAVAILABLE_MESSAGE), 503

    data = request.get_json(silent=True) or {}
    question = (data.get("message") or "").strip()
    mode = data.get("mode") or ai_engine.DEFAULT_MODE
    module_id = data.get("module_id")
    resource_id = data.get("resource_id")
    conversation_id = data.get("conversation_id")
    response_style = data.get("response_style") or "guided"
    if response_style not in {"guided", "simple", "detailed", "exam"}:
        response_style = "guided"

    if not question:
        return jsonify(ok=False, error="Please type a question or request first."), 400
    if len(question) > 4000:
        return jsonify(ok=False, error="That message is too long. Please shorten it."), 400

    module = db.session.get(Module, module_id) if module_id else None
    resource = db.session.get(Resource, resource_id) if resource_id else None
    if resource and not resource.is_verified:
        return jsonify(ok=False, error="That resource is not available for student study yet."), 404
    if resource and module is None:
        module = resource.module
    if resource and module and resource.module_id != module.id:
        return jsonify(ok=False, error="The selected resource does not belong to this module."), 400
    if module and not _student_can_use_module(module):
        return jsonify(ok=False, error="Choose a module from your current academic semester."), 403

    conversation = None
    history = []
    if conversation_id:
        conversation = AIConversation.query.filter_by(
            id=conversation_id, student_id=current_user.id
        ).first()
        if conversation:
            history = [{"role": m.role, "content": m.content} for m in conversation.messages]

    preference = StudentPreference.query.filter_by(student_id=current_user.id).first()
    if preference is None:
        preference = StudentPreference(student_id=current_user.id)
        db.session.add(preference)
    preference.response_style = response_style

    result = ai_engine.ask(
        mode=mode, question=question, module=module, resource=resource,
        history=history, student=current_user, response_style=response_style,
    )

    if not result["ok"]:
        return jsonify(ok=False, error=result["error"]), 503

    if conversation is None:
        conversation = AIConversation(
            student_id=current_user.id,
            module_id=module.id if module else None,
            title=_make_title(question),
            mode=mode,
        )
        db.session.add(conversation)
        db.session.flush()
    else:
        conversation.mode = mode
        if module is not None:
            conversation.module_id = module.id

    db.session.add(AIMessage(conversation_id=conversation.id, role="user", content=question))
    assistant_message = AIMessage(
        conversation_id=conversation.id, role="assistant",
        content=result["answer_text"], content_html=result["answer_html"],
        sources_json=ai_engine.sources_to_json(result["sources"], result.get("web_sources")),
        general_guidance=result["general_guidance"],
    )
    db.session.add(assistant_message)
    event_module = module or next(iter(_active_modules()), None)
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
        conversation_id=conversation.id,
        conversation_title=conversation.title,
        answer_html=result["answer_html"],
        message_id=assistant_message.id,
        sources=_sources_payload(result["sources"]),
        web_sources=_web_sources_payload(result.get("web_sources")),
        general_guidance=result["general_guidance"],
        web_grounded=result.get("web_grounded", False),
        followups=result.get("followups", []),
    )


@ai_bp.route("/regenerate", methods=["POST"])
def regenerate():
    """Replace the most recent answer without duplicating the user's prompt."""
    data = request.get_json(silent=True) or {}
    conversation_id = data.get("conversation_id")
    response_style = data.get("response_style") or "guided"
    if response_style not in {"guided", "simple", "detailed", "exam"}:
        response_style = "guided"

    conversation = AIConversation.query.filter_by(
        id=conversation_id, student_id=current_user.id
    ).first_or_404()
    messages = list(conversation.messages)
    if len(messages) < 2 or messages[-1].role != "assistant":
        return jsonify(ok=False, error="There is no answer available to regenerate."), 400
    user_index = next((index for index in range(len(messages) - 2, -1, -1)
                       if messages[index].role == "user"), None)
    if user_index is None:
        return jsonify(ok=False, error="The original question could not be found."), 400

    module = conversation.module
    if module and not _student_can_use_module(module):
        return jsonify(ok=False, error="This conversation is outside your current academic context."), 403
    question = messages[user_index].content
    history = [{"role": message.role, "content": message.content}
               for message in messages[:user_index]]
    result = ai_engine.ask(
        mode=conversation.mode, question=question, module=module, history=history,
        student=current_user, response_style=response_style,
    )
    if not result["ok"]:
        return jsonify(ok=False, error=result["error"]), 503

    assistant_message = messages[-1]
    AIAnswerFeedback.query.filter_by(message_id=assistant_message.id).delete()
    assistant_message.content = result["answer_text"]
    assistant_message.content_html = result["answer_html"]
    assistant_message.sources_json = ai_engine.sources_to_json(result["sources"], result.get("web_sources"))
    assistant_message.general_guidance = result["general_guidance"]
    conversation.updated_at = utcnow()
    db.session.commit()

    return jsonify(
        ok=True,
        answer_html=result["answer_html"],
        message_id=assistant_message.id,
        sources=_sources_payload(result["sources"]),
        web_sources=_web_sources_payload(result.get("web_sources")),
        general_guidance=result["general_guidance"],
        web_grounded=result.get("web_grounded", False),
        followups=result.get("followups", []),
    )


@ai_bp.route("/conversations/<int:conversation_id>/rename", methods=["POST"])
def rename_conversation(conversation_id):
    data = request.get_json(silent=True) or {}
    title = " ".join((data.get("title") or "").split())
    if not 3 <= len(title) <= 100:
        return jsonify(ok=False, error="Use a title between 3 and 100 characters."), 400
    conversation = AIConversation.query.filter_by(
        id=conversation_id, student_id=current_user.id
    ).first_or_404()
    conversation.title = title
    db.session.commit()
    return jsonify(ok=True, title=conversation.title)


@ai_bp.route("/feedback", methods=["POST"])
def feedback():
    data = request.get_json(silent=True) or {}
    try:
        message_id = int(data.get("message_id"))
    except (TypeError, ValueError):
        message_id = None
    try:
        rating = int(data.get("rating")) if data.get("rating") is not None else None
    except (TypeError, ValueError):
        rating = None
    unclear = bool(data.get("is_unclear")) if hasattr(data, "get") else False
    note = (data.get("note") or "").strip()[:1000] if hasattr(data, "get") else ""
    message = AIMessage.query.get_or_404(message_id)
    if message.conversation.student_id != current_user.id or message.role != "assistant":
        abort(403)
    if rating not in (None, 1, 2, 3, 4, 5):
        return jsonify(ok=False, error="Rating must be between 1 and 5."), 400
    db.session.add(AIAnswerFeedback(
        message_id=message.id, student_id=current_user.id, rating=rating,
        is_unclear=unclear, note=note or None,
    ))
    db.session.commit()
    return jsonify(ok=True)


@ai_bp.route("/conversations/<int:conversation_id>/delete", methods=["POST"])
def delete_conversation(conversation_id):
    conversation = AIConversation.query.filter_by(
        id=conversation_id, student_id=current_user.id
    ).first_or_404()
    db.session.delete(conversation)
    db.session.commit()
    flash("Conversation deleted.", "info")
    return redirect(url_for("ai.assistant"))
