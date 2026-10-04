"""Role-based navigation shared by the sidebar, mobile drawer and command palette.

Each role's menu is declared once here. Templates never repeat nav markup or
hand-write "is this link active?" conditions; they read the resolved
``NavSection`` list that ``app_navigation()`` injects into every template.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from flask import request, url_for


@dataclass(frozen=True)
class NavItem:
    label: str
    endpoint: str
    icon: str
    # Other endpoints that belong to this destination, e.g. every step of the
    # curriculum drill-down highlights "Learning library". A trailing "*"
    # matches an endpoint prefix.
    matches: tuple[str, ...] = ()
    # Shown in the command palette under the label.
    hint: str = ""
    # Name of a context value holding a count to show as a badge.
    badge: str | None = None
    # Stable handle for templates that place an item somewhere specific.
    key: str | None = None

    def is_active(self, endpoint: str | None) -> bool:
        if not endpoint:
            return False
        for pattern in (self.endpoint, *self.matches):
            if pattern.endswith("*"):
                if endpoint.startswith(pattern[:-1]):
                    return True
            elif endpoint == pattern:
                return True
        return False


@dataclass(frozen=True)
class NavSection:
    label: str
    items: tuple[NavItem, ...] = field(default_factory=tuple)


_ACCOUNT = NavSection("Account", (
    NavItem("Notifications", "notifications.index", "bell", key="notifications",
            hint="Replies, approvals and reminders", badge="unread_notification_count"),
    NavItem("Profile & settings", "profile.settings", "users", key="profile",
            hint="Your details, password and preferences"),
))

_STUDENT = (
    NavSection("Learn", (
        NavItem("Dashboard", "student.dashboard", "home", hint="Your learning overview"),
        NavItem("Learning library", "student.departments", "grid",
                matches=("student.programmes", "student.levels", "student.semesters",
                         "student.modules", "student.module_resources", "student.resource_view"),
                hint="Browse modules and resources"),
        NavItem("DIT AI", "ai.assistant", "sparkles", matches=("ai.*",),
                hint="Explain, solve, quiz and revise"),
        NavItem("Announcements", "student.announcements_page", "megaphone",
                hint="Updates from your lecturers", badge="unread_announcements"),
        NavItem("Ask a lecturer", "student.questions", "help-circle",
                hint="Private questions to your lecturers"),
    )),
    NavSection("Progress", (
        NavItem("Insights", "student.learning_insights", "bar-chart",
                hint="Streaks, goals and topic mastery"),
        NavItem("Downloads", "student.downloads", "download", hint="Files saved for offline study"),
        NavItem("Search", "student.search", "search", hint="Search this semester's materials"),
    )),
    _ACCOUNT,
)

_LECTURER = (
    NavSection("Teach", (
        NavItem("Dashboard", "lecturer.dashboard", "home", hint="Engagement for your module"),
        NavItem("Module workspace", "lecturer.workspace", "layers",
                matches=("lecturer.module_content",), hint="Claim modules and manage topics"),
        NavItem("Upload resource", "lecturer.upload_departments", "upload",
                matches=("lecturer.upload_*",), hint="Publish a new learning resource"),
        NavItem("Manage resources", "lecturer.manage_resources", "folder",
                matches=("lecturer.edit_resource",), hint="Edit, verify or remove resources"),
    )),
    NavSection("Students", (
        NavItem("Announcements", "lecturer.announcements_page", "megaphone",
                hint="Message every student in a module"),
        NavItem("Student questions", "lecturer.questions", "help-circle",
                hint="Anonymous questions awaiting your answer"),
    )),
    _ACCOUNT,
)

_DEPARTMENT_HEAD = (
    NavSection("Department", (
        NavItem("Overview", "department.dashboard", "home", hint="Department health at a glance"),
        NavItem("Curriculum", "department.curriculum", "layers",
                matches=("department.edit_module",), hint="Academic years and modules"),
        NavItem("Students", "department.students", "award",
                matches=("department.student_*",), hint="Placement and registered modules"),
        NavItem("Lecturers", "department.lecturers", "users",
                matches=("department.lecturer_requests",), hint="Verification and access"),
        NavItem("Module claims", "department.module_claims", "check-circle",
                hint="Approve teaching assignments"),
        NavItem("Audit log", "department.audit_log", "shield-check",
                hint="Sensitive actions history"),
    )),
    _ACCOUNT,
)


_ADMIN = (
    NavSection("Curriculum", (
        NavItem("Overview", "admin.dashboard", "home", hint="Live prospectus and SOMA status"),
        NavItem("Prospectus", "admin.prospectus", "upload",
                matches=("admin.prospectus_*",), hint="Upload the prospectus for every department"),
        NavItem("Students", "admin.students", "award",
                matches=("admin.student_*",), hint="Placement and registered modules"),
        NavItem("Audit log", "admin.audit_log", "shield-check", hint="Curriculum and role changes"),
    )),
    _ACCOUNT,
)


def sections_for(user) -> tuple[NavSection, ...]:
    if not getattr(user, "is_authenticated", False):
        return ()
    if user.is_admin:
        return _ADMIN
    if user.is_department_head:
        return _DEPARTMENT_HEAD
    if user.is_lecturer:
        return _LECTURER
    if user.is_student:
        return _STUDENT
    return ()


def search_target(user) -> dict | None:
    """Where free text typed into the command palette is searched, if anywhere."""
    if user.is_student:
        return {"url": url_for("student.search"), "label": "Search learning materials"}
    if user.is_lecturer:
        return {"url": url_for("lecturer.manage_resources"), "label": "Search your resources"}
    return None


def role_label(user) -> str:
    if user.is_admin:
        return "Curriculum Administrator"
    if user.is_department_head:
        return "Head of Department"
    if user.is_lecturer:
        return "Lecturer"
    return "Student"


def app_navigation(user, counts: dict[str, int] | None = None) -> dict:
    """Resolve the current user's menu into plain dicts for templates and JS."""
    counts = counts or {}
    endpoint = request.endpoint
    sections = []
    by_key = {}
    active = None
    for section in sections_for(user):
        entries = []
        for item in section.items:
            resolved = {
                "label": item.label,
                "url": url_for(item.endpoint),
                "icon": item.icon,
                "hint": item.hint,
                "active": item.is_active(endpoint),
                "badge": counts.get(item.badge, 0) if item.badge else 0,
                "section": section.label,
            }
            if item.key:
                by_key[item.key] = resolved
            if resolved["active"] and active is None:
                active = resolved
            entries.append(resolved)
        sections.append({"label": section.label, "entries": entries, "is_account": section is _ACCOUNT})
    return {
        "sections": sections,
        "primary": [section for section in sections if not section["is_account"]],
        "items": by_key,
        "active": active,
        "search": search_target(user),
    }
