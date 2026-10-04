"""Small, reusable learning-analytics helpers.

The application records only purposeful learning events (resource study,
downloads and AI requests).  These helpers aggregate that first-party data
for the student and lecturer dashboards without tracking browser behaviour.
"""

from collections import Counter
from datetime import datetime, timedelta, timezone

from extensions import db
from models import (
    utcnow_naive, LearningEvent, ResourceView, Topic, AIAnswerFeedback, AIMessage, AIConversation,
    AcademicQuestion, Resource, User, Module, Semester, NtaLevel, Programme,
)


def utcnow():
    # SQLite returns naive datetimes for this project, so analytics uses the
    # same representation for safe Python-side date comparisons.
    return utcnow_naive()


def record_learning_event(student_id, module_id, event_type, *, topic_id=None,
                          duration_minutes=0, detail=None, qualifies_for_streak=False):
    """Store one intentional learning action.  Callers commit with their action."""
    if not student_id or not module_id:
        return None
    event = LearningEvent(
        student_id=student_id,
        module_id=module_id,
        topic_id=topic_id,
        event_type=event_type,
        duration_minutes=max(0, int(duration_minutes or 0)),
        detail=(detail or "")[:240] or None,
        qualifies_for_streak=bool(qualifies_for_streak),
    )
    db.session.add(event)
    return event


def _day_key(value):
    return value.astimezone(timezone.utc).date().isoformat() if value.tzinfo else value.date().isoformat()


def student_insights(student_id, module):
    """Return presentation-ready, privacy-safe personal metrics for one module."""
    today = utcnow().date()
    # SQLite returns naive datetimes in this application, so use a naive
    # boundary for safe comparison while retaining UTC-only date semantics.
    start = datetime.combine(today - timedelta(days=27), datetime.min.time())
    events = (LearningEvent.query.filter(
        LearningEvent.student_id == student_id,
        LearningEvent.module_id == module.id,
    ).order_by(LearningEvent.created_at.desc()).all())
    recent = [e for e in events if e.created_at >= start]
    views = (ResourceView.query.join(Resource)
             .filter(ResourceView.student_id == student_id, Resource.module_id == module.id)
             .all())
    topics = Topic.query.filter_by(
        module_id=module.id, is_published=True
    ).order_by(Topic.display_order, Topic.id).all()
    topic_counts = Counter(e.topic_id for e in events if e.topic_id)
    question_count = sum(
        1 for e in events
        if e.event_type in {"academic_ai_question", "lecturer_question"}
    )
    days = {_day_key(e.created_at) for e in events if e.qualifies_for_streak}

    # Build a compact 28-day timeline and a four-week heat map row.
    daily = Counter(_day_key(e.created_at) for e in recent)
    timeline = []
    for offset in range(27, -1, -1):
        day = today - timedelta(days=offset)
        timeline.append({"label": day.strftime("%d %b"), "count": daily.get(day.isoformat(), 0)})
    max_daily = max([item["count"] for item in timeline] or [1])
    for item in timeline:
        item["height"] = max(6, round(item["count"] / max_daily * 100)) if item["count"] else 4
        item["intensity"] = min(4, item["count"])
    for index, item in enumerate(timeline):
        item["chart_x"] = round(index / max(1, len(timeline) - 1) * 100, 2)
        item["chart_y"] = 94 if not item["count"] else round(94 - (item["count"] / max_daily * 78), 2)

    # Streak is intentionally based on dates with deliberate study actions.
    streak = 0
    cursor = today
    while cursor.isoformat() in days:
        streak += 1
        cursor -= timedelta(days=1)

    topic_progress = []
    for topic in topics:
        actions = topic_counts.get(topic.id, 0)
        # Four meaningful interactions signals a complete self-study pass;
        # it is a transparent engagement signal, not a graded mark.
        percent = min(100, actions * 25)
        topic_progress.append({"topic": topic, "actions": actions, "percent": percent})

    total_minutes = sum(e.duration_minutes for e in events)
    active_days = len(days)
    completed = sum(1 for item in topic_progress if item["percent"] >= 100)
    mastery = round(min(100, (completed / max(1, len(topics))) * 45 + min(total_minutes, 180) / 180 * 35 + min(question_count, 12) / 12 * 20))
    weak_topics = sorted(topic_progress, key=lambda item: (item["actions"], item["topic"].display_order))[:2]
    next_topic = next((item for item in topic_progress if item["percent"] < 100), None)
    activity_counts = {
        "study": sum(
            1 for event in recent
            if event.event_type in {"resource_study_qualified", "topic_study"}
        ),
        "ai": sum(1 for event in recent if event.event_type == "academic_ai_question"),
        "downloads": sum(1 for event in recent if event.event_type == "resource_download"),
    }
    activity_total = sum(activity_counts.values())
    activity_breakdown = [
        {
            "key": key, "label": label, "count": count,
            "percent": round(count / activity_total * 100) if activity_total else 0,
        }
        for key, label, count in (
            ("study", "Focused study", activity_counts["study"]),
            ("ai", "AI support", activity_counts["ai"]),
            ("downloads", "Downloads", activity_counts["downloads"]),
        )
    ]

    return {
        "events": events,
        "total_minutes": total_minutes,
        "active_days": active_days,
        "streak": streak,
        "questions": question_count,
        "resources_viewed": len({view.resource_id for view in views}),
        "downloads": sum(1 for event in events if event.event_type == "resource_download"),
        "mastery": mastery,
        "completed_topics": completed,
        "topic_progress": topic_progress,
        "weak_topics": weak_topics,
        "next_topic": next_topic,
        "timeline": timeline,
        "activity_breakdown": activity_breakdown,
        "recent_actions": events[:6],
    }


def lecturer_insights(module, *, lecturer_id=None, days=30):
    """Aggregate module learning activity for a lecturer's assigned workspace."""
    since = utcnow() - timedelta(days=days - 1)
    events = (LearningEvent.query.filter(
        LearningEvent.module_id == module.id,
        LearningEvent.created_at >= since,
    ).order_by(LearningEvent.created_at.desc()).all())
    all_events = LearningEvent.query.filter_by(module_id=module.id).all()
    enrolled_ids = {
        student.id for student in User.query.filter_by(
            role="student", semester_id=module.semester_id, is_active_account=True
        ).all()
    }
    all_views = (ResourceView.query.join(Resource)
                 .filter(Resource.module_id == module.id).all())
    recent_views = [view for view in all_views if view.viewed_at >= since]
    topics = Topic.query.filter_by(
        module_id=module.id, is_published=True
    ).order_by(Topic.display_order, Topic.id).all()
    student_ids = {e.student_id for e in all_events} | {view.student_id for view in all_views}
    active_ids = {e.student_id for e in events} | {view.student_id for view in recent_views}
    active_ids &= enrolled_ids or active_ids
    by_day = Counter(_day_key(e.created_at) for e in events)
    minutes_by_day = Counter()
    for event in events:
        minutes_by_day[_day_key(event.created_at)] += event.duration_minutes

    timeline = []
    for offset in range(days - 1, -1, -1):
        day = utcnow().date() - timedelta(days=offset)
        timeline.append({
            "label": day.strftime("%d %b"),
            "students": by_day.get(day.isoformat(), 0),
            "minutes": minutes_by_day.get(day.isoformat(), 0),
        })
    max_students = max([x["students"] for x in timeline] or [1])
    for point in timeline:
        point["height"] = max(5, round(point["students"] / max_students * 100)) if point["students"] else 4

    topic_events = Counter(e.topic_id for e in all_events if e.topic_id)
    topic_rows = [{"topic": topic, "actions": topic_events.get(topic.id, 0)} for topic in topics]
    most_studied = sorted(topic_rows, key=lambda x: x["actions"], reverse=True)[:4]
    least_studied = sorted(topic_rows, key=lambda x: x["actions"])[:4]
    # Exact AI prompts are private to the student. Lecturer-facing patterns
    # come only from questions deliberately submitted to that lecturer.
    question_query = AcademicQuestion.query.filter_by(module_id=module.id)
    if lecturer_id:
        question_query = question_query.filter_by(lecturer_id=lecturer_id)
    private_questions = question_query.all()
    question_topics = Counter(
        question.topic.title if question.topic else question.subject
        for question in private_questions
    )
    repeated_questions = question_topics.most_common(4)
    enrolled = len(enrolled_ids) or len(student_ids)
    at_risk = max(0, enrolled - len(active_ids))
    resources_total = Resource.query.filter_by(module_id=module.id).count()
    resources_opened = len({view.resource_id for view in all_views})
    resource_counts = Counter(view.resource_id for view in all_views)
    resource_lookup = {view.resource_id: view.resource for view in all_views}
    popular_resources = sorted(
        [
            {"resource": resource_lookup[resource_id], "views": count}
            for resource_id, count in resource_counts.items()
            if resource_lookup.get(resource_id) is not None
        ],
        key=lambda item: item["views"], reverse=True,
    )[:4]
    active_days = len({_day_key(event.created_at) for event in events} | {
        _day_key(view.viewed_at) for view in recent_views
    })
    feedback_rows = (AIAnswerFeedback.query.join(AIMessage).join(AIConversation)
                     .filter(AIConversation.module_id == module.id).all())
    ratings = [row.rating for row in feedback_rows if row.rating]

    return {
        "enrolled": enrolled,
        "active": len(active_ids),
        "at_risk": at_risk,
        "engagement_rate": round((len(active_ids) / enrolled) * 100) if enrolled else 0,
        "study_minutes": sum(e.duration_minutes for e in events),
        "average_study_minutes": round(sum(e.duration_minutes for e in events) / max(1, len(active_ids))),
        "resource_adoption": round((resources_opened / resources_total) * 100) if resources_total else 0,
        "active_days": active_days,
        "topic_coverage": round((len({event.topic_id for event in all_events if event.topic_id}) / len(topics)) * 100) if topics else 0,
        "questions": len(private_questions),
        "average_rating": round(sum(ratings) / len(ratings), 1) if ratings else None,
        "unclear_answers": sum(1 for row in feedback_rows if row.is_unclear),
        "timeline": timeline,
        "most_studied": most_studied,
        "least_studied": least_studied,
        "repeated_questions": repeated_questions,
        "topic_rows": topic_rows,
        "popular_resources": popular_resources,
        "events": events,
    }


def personal_recommendations(student):
    """Return transparent, curriculum-scoped recommendations for one learner.

    The recommendations intentionally use first-party learning activity rather
    than opaque profiling. They are safe to show in a dashboard and give the
    learner a clear reason for every suggested next action.
    """
    if not student or not student.programme_id:
        return []
    programme_modules = (Module.query.join(Semester).join(NtaLevel)
                         .filter(
                             NtaLevel.programme_id == student.programme_id,
                             Module.is_active.is_(True),
                             Module.publication_status == "published",
                         )
                         .order_by(Module.display_order).all())
    programme_modules = [
        module for module in programme_modules
        if (not student.semester_id or module.semester_id == student.semester_id)
        and (
            not student.academic_year_id
            or not module.academic_year_id
            or module.academic_year_id == student.academic_year_id
        )
    ]
    if not programme_modules:
        return []
    viewed_ids = {
        row.resource_id for row in ResourceView.query.filter_by(student_id=student.id).all()
    }
    events = LearningEvent.query.filter_by(student_id=student.id).all()
    module_actions = Counter(event.module_id for event in events)
    recommendations = []
    weakest = sorted(programme_modules, key=lambda module: (module_actions.get(module.id, 0), module.display_order))
    for module in weakest:
        unseen = next((resource for resource in module.resources
                       if resource.is_verified and resource.id not in viewed_ids), None)
        if unseen:
            reason = "Start with this verified resource to strengthen a module with less recent study activity."
            if module.semester_id == student.semester_id:
                reason = "Continue your current learning pathway with a new verified resource."
            recommendations.append({"resource": unseen, "module": module, "reason": reason, "action": "Study now"})
        if len(recommendations) >= 3:
            break
    if not recommendations and student.semester:
        for module in student.semester.modules:
            resource = next((item for item in module.resources if item.is_verified), None)
            if resource:
                recommendations.append({
                    "resource": resource, "module": module,
                    "reason": "A verified resource from your current semester.", "action": "Open resource",
                })
                if len(recommendations) >= 3:
                    break
    return recommendations


def student_profile_summary(student):
    """Personal, non-competitive streak and achievement signals for a learner."""
    events = LearningEvent.query.filter_by(student_id=student.id).order_by(LearningEvent.created_at.asc()).all()
    days = sorted({_day_key(event.created_at) for event in events if event.qualifies_for_streak})
    day_set = set(days)
    today = utcnow().date()
    streak = 0
    cursor = today
    while cursor.isoformat() in day_set:
        streak += 1
        cursor -= timedelta(days=1)
    longest = 0
    run = 0
    previous = None
    for day_text in days:
        day = datetime.fromisoformat(day_text).date()
        if previous and (day - previous).days == 1:
            run += 1
        else:
            run = 1
        longest = max(longest, run)
        previous = day
    viewed = len({row.resource_id for row in ResourceView.query.filter_by(student_id=student.id).all()})
    ai_questions = sum(1 for event in events if event.event_type == "academic_ai_question")
    achievements = []
    if viewed >= 1:
        achievements.append({"title": "Learning Explorer", "detail": "Opened your first verified learning resource."})
    if streak >= 3:
        achievements.append({"title": "Three-day rhythm", "detail": "Studied on three consecutive days."})
    if longest >= 7:
        achievements.append({"title": "Revision consistency", "detail": "Built a seven-day learning streak."})
    if ai_questions >= 5:
        achievements.append({"title": "Curious learner", "detail": "Used the AI tutor to deepen your studies."})
    return {
        "current_streak": streak,
        "longest_streak": longest,
        "active_days": len(day_set),
        "achievements": achievements,
    }


def department_insights(department_id, *, days=30):
    """Aggregate Electrical Engineering learning signals for the HOD only."""
    since = utcnow() - timedelta(days=days - 1)
    module_ids = [row[0] for row in (
        db.session.query(Module.id).join(Semester).join(NtaLevel).join(Programme)
        .filter(Programme.department_id == department_id).all()
    )]
    students = User.query.filter_by(role="student", department_id=department_id, is_active_account=True).all()
    lecturers = User.query.filter_by(role="lecturer", department_id=department_id, is_active_account=True).all()
    events = LearningEvent.query.filter(LearningEvent.module_id.in_(module_ids or [-1])).all()
    recent_events = [event for event in events if event.created_at >= since]
    active_students = {event.student_id for event in recent_events}
    risk_students = [student for student in students if student.id not in active_students]
    resources = Resource.query.filter(Resource.module_id.in_(module_ids or [-1])).all()
    academic_questions = AcademicQuestion.query.filter(
        AcademicQuestion.module_id.in_(module_ids or [-1])
    ).all()
    difficult_topics = Counter()
    for question in academic_questions:
        difficult_topics[
            question.topic.title if question.topic else question.subject
        ] += 1
    return {
        "students": len(students),
        "lecturers": len(lecturers),
        "active_students": len(active_students),
        "at_risk_students": len(risk_students),
        "at_risk_names": [student.full_name for student in risk_students[:5]],
        "resources": len(resources),
        "verified_resources": sum(1 for resource in resources if resource.is_verified),
        "academic_questions": len(academic_questions),
        "study_minutes": sum(event.duration_minutes for event in recent_events),
        "difficult_topics": difficult_topics.most_common(5),
    }
