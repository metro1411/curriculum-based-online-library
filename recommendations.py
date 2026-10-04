"""Curriculum-aware study recommendations ("academic fitness").

Recommendations compare a student's *recorded learning activity* across the
modules in their current curriculum position. They never claim to measure
grades, ability or assessment performance, because the hub does not hold
that data. Each recommendation carries the evidence it was based on so the
student can see exactly why it was made.
"""

from __future__ import annotations

from collections import Counter
from datetime import timedelta

from models import LearningEvent, Resource, ResourceView, Topic, utcnow_naive

WINDOW_DAYS = 14
EVIDENCE_NOTE = (
    "Based only on your recorded study activity in Smart DIT (resource study, downloads, "
    "questions and topic study). It does not reflect marks or assessment results."
)


def curriculum_recommendations(student, *, limit=3):
    import academic_context

    modules, from_registration = academic_context.current_modules(student)
    if not modules:
        return []
    since = utcnow_naive() - timedelta(days=WINDOW_DAYS)
    module_ids = [m.id for m in modules]

    events = LearningEvent.query.filter(
        LearningEvent.student_id == student.id,
        LearningEvent.module_id.in_(module_ids),
        LearningEvent.created_at >= since,
    ).all()
    views = (ResourceView.query.join(Resource)
             .filter(ResourceView.student_id == student.id, Resource.module_id.in_(module_ids),
                     ResourceView.viewed_at >= since).all())
    activity = Counter(event.module_id for event in events)
    for view in views:
        activity[view.resource.module_id] += 1
    questions = Counter(e.module_id for e in events
                        if e.event_type in {"academic_ai_question", "lecturer_question"})
    topic_activity = Counter(e.topic_id for e in events if e.topic_id)

    def resources_for(module):
        return [r for r in module.resources if r.is_verified]

    recommendations = []
    total = sum(activity.values())
    for module in sorted(modules, key=lambda m: (activity.get(m.id, 0), m.display_order)):
        available = resources_for(module)
        if not available:
            continue
        own = activity.get(module.id, 0)
        others = [activity.get(m.id, 0) for m in modules if m.id != module.id]
        average_other = round(sum(others) / len(others), 1) if others else 0
        topic = _focus_topic(module, topic_activity)
        if total == 0:
            message = (f"No study activity has been recorded for your current modules in the last "
                       f"{WINDOW_DAYS} days. {module.name} has {len(available)} verified lecturer "
                       f"resource{'s' if len(available) != 1 else ''} to start with.")
        elif others and own < 0.5 * average_other:
            message = (f"You have low recent activity in {module.name} compared with your other current "
                       f"modules ({own} vs an average of {average_other} study actions in {WINDOW_DAYS} days).")
            message += (f" Consider reviewing the lecturer resources for {topic.title}." if topic
                        else " Consider reviewing its verified lecturer resources.")
        else:
            continue
        recommendations.append({
            "module": module,
            "topic": topic,
            "message": message,
            "evidence": {
                "window_days": WINDOW_DAYS,
                "module_actions": own,
                "average_other_modules": average_other,
                "questions_asked": questions.get(module.id, 0),
                "verified_resources": len(available),
                "curriculum_basis": "registered modules" if from_registration else "current semester modules",
            },
        })
        if len(recommendations) >= limit:
            break
    return recommendations


def _focus_topic(module, topic_activity):
    """The published topic with lecturer resources that the student has studied least."""
    topics = Topic.query.filter_by(module_id=module.id, is_published=True).order_by(Topic.display_order).all()
    with_resources = [t for t in topics if any(r.is_verified for r in t.resources)]
    pool = with_resources or topics
    if not pool:
        return None
    return min(pool, key=lambda t: (topic_activity.get(t.id, 0), t.display_order))


def to_json(items):
    return [{
        "module_id": item["module"].id,
        "module": item["module"].name,
        "module_code": item["module"].code,
        "topic": item["topic"].title if item["topic"] else None,
        "message": item["message"],
        "evidence": item["evidence"],
    } for item in items]
