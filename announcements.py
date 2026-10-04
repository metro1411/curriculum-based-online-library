"""Lecturer-to-student module announcements.

All rules for this channel live here so every entry point (the lecturer
announcements page and the module workspace) behaves identically:

* only a lecturer with an approved assignment for the module may post;
* messages are length-limited, de-duplicated and rate-limited;
* delivery reaches exactly the students who can open the module, as an
  in-app notification (email only when the lecturer asks for it);
* publishing and removal are recorded in the department audit log.
"""

from __future__ import annotations

from datetime import timedelta

from extensions import db
from governance import record_audit
from models import Announcement, LecturerAssignment, Notification, User, utcnow_naive
from notifications import notify

TITLE_MAX = 160
BODY_MAX = 2000
DUPLICATE_WINDOW = timedelta(minutes=10)
HOURLY_LIMIT = 10


class AnnouncementError(Exception):
    """A problem the lecturer can fix; the message is shown to them as-is."""


def _department_id(module):
    return module.semester.nta_level.programme.department_id


def module_audience(module):
    """Active students who can open this module.

    Mirrors the student access rule in routes/student.py: same semester and
    programme, and the same academic year when both sides record one.
    """
    programme_id = module.semester.nta_level.programme_id
    query = User.query.filter(
        User.role == "student",
        User.is_active_account.is_(True),
        User.semester_id == module.semester_id,
        User.programme_id == programme_id,
    )
    if module.academic_year_id:
        query = query.filter(db.or_(
            User.academic_year_id.is_(None),
            User.academic_year_id == module.academic_year_id,
        ))
    return query.all()


def lecturer_can_post(lecturer, module):
    return LecturerAssignment.query.filter_by(
        lecturer_id=lecturer.id, module_id=module.id, status="approved"
    ).first() is not None


def target_url(announcement):
    return f"/announcements?module_id={announcement.module_id}#{announcement.anchor}"


def publish(module, author, title, body, *, pinned=False, email=False):
    """Create an announcement and notify its audience. Caller commits."""
    title = " ".join((title or "").split())
    body = (body or "").strip()
    if not lecturer_can_post(author, module):
        raise AnnouncementError("You can only post announcements to modules you are approved to teach.")
    if not module.is_published:
        raise AnnouncementError("Announcements can only be sent for published modules.")
    if not title or not body:
        raise AnnouncementError("An announcement needs both a title and a message.")
    if len(title) > TITLE_MAX or len(body) > BODY_MAX:
        raise AnnouncementError(
            f"Keep the title under {TITLE_MAX} characters and the message under {BODY_MAX}."
        )

    now = utcnow_naive()
    duplicate = Announcement.query.filter(
        Announcement.module_id == module.id,
        Announcement.author_id == author.id,
        Announcement.title == title,
        Announcement.body == body,
        Announcement.created_at >= now - DUPLICATE_WINDOW,
    ).first()
    if duplicate:
        raise AnnouncementError("This announcement was already sent a moment ago.")
    recent = Announcement.query.filter(
        Announcement.author_id == author.id,
        Announcement.created_at >= now - timedelta(hours=1),
    ).count()
    if recent >= HOURLY_LIMIT:
        raise AnnouncementError(
            f"You can send up to {HOURLY_LIMIT} announcements an hour. Please try again later."
        )

    announcement = Announcement(
        module_id=module.id, author_id=author.id, title=title, body=body,
        is_pinned=bool(pinned), emailed=bool(email),
    )
    db.session.add(announcement)
    db.session.flush()

    audience = module_audience(module)
    label = module.code or module.name
    preview = body if len(body) <= 220 else body[:217].rstrip() + "…"
    for student in audience:
        notify(
            student, "announcement", f"{label}: {title}", preview,
            target_url=target_url(announcement),
            optional_email=True, deliver_email=bool(email),
        )
    announcement.recipient_count = len(audience)
    record_audit(
        "announcement.published", "Announcement", target_id=announcement.id,
        target_label=title, department_id=_department_id(module),
        details={"module_id": module.id, "recipients": len(audience),
                 "pinned": announcement.is_pinned, "emailed": announcement.emailed},
    )
    return announcement


def remove(announcement, actor):
    """Delete an announcement and any unread alerts pointing at it. Caller commits."""
    if not lecturer_can_post(actor, announcement.module):
        raise AnnouncementError("You can only remove announcements for modules you teach.")
    Notification.query.filter_by(
        kind="announcement", target_url=target_url(announcement), is_read=False
    ).delete(synchronize_session=False)
    record_audit(
        "announcement.removed", "Announcement", target_id=announcement.id,
        target_label=announcement.title, department_id=_department_id(announcement.module),
        details={"module_id": announcement.module_id},
    )
    db.session.delete(announcement)


def for_modules(module_ids, *, limit=None):
    """Announcements for the given modules, pinned first, newest first."""
    if not module_ids:
        return []
    query = Announcement.query.filter(Announcement.module_id.in_(module_ids)).order_by(
        Announcement.is_pinned.desc(), Announcement.created_at.desc()
    )
    return query.limit(limit).all() if limit else query.all()


def mark_read_for(student):
    """Clear announcement alerts once the student has opened the announcements page."""
    Notification.query.filter_by(user_id=student.id, kind="announcement", is_read=False).update(
        {"is_read": True}, synchronize_session=False
    )


def unread_count(student):
    return Notification.query.filter_by(user_id=student.id, kind="announcement", is_read=False).count()
