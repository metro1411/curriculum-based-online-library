"""Central role-based access rules.

Blueprints already guard whole areas with a role check in ``before_request``.
The helpers here express the finer, cross-cutting rules in one place so the
same answer is given by HTML routes, JSON endpoints and tests:

* students read published curriculum and their own data only;
* lecturers manage resources they uploaded, inside modules they are approved
  to teach, and never change curriculum structure;
* heads of department manage their own department's modules and lecturers;
* curriculum administrators run the prospectus workflow institution-wide.
"""

from __future__ import annotations

from functools import wraps

from flask import abort
from flask_login import current_user, login_required

ROLE_LABELS = {
    "student": "Student",
    "lecturer": "Lecturer",
    "department_head": "Head of Department",
    "admin": "Curriculum Administrator",
}


def require_roles(*roles):
    """Decorator for a single view: sign-in required and role must match."""
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if current_user.role not in roles:
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator


def can_manage_curriculum(user):
    """Only curriculum administrators may create, publish or archive versions."""
    return bool(user and getattr(user, "is_authenticated", False) and user.is_admin)


def can_view_curriculum_versions(user):
    return bool(user and getattr(user, "is_authenticated", False)
                and (user.is_admin or user.is_department_head))


def lecturer_teaches_module(user, module_id):
    from models import LecturerAssignment
    if not user or not user.is_lecturer or not module_id:
        return False
    return LecturerAssignment.query.filter_by(
        lecturer_id=user.id, module_id=module_id, status="approved"
    ).first() is not None


def can_edit_resource(user, resource):
    """A lecturer edits only their own uploads in a module they still teach.

    Another lecturer's material is protected even when both teach the same
    module; the department head resolves ownership changes.
    """
    if not user or resource is None or not user.is_lecturer:
        return False
    return resource.uploaded_by_id == user.id and lecturer_teaches_module(user, resource.module_id)


def can_view_module_students(user, module):
    """Lecturers see the class list only for modules they are approved to teach."""
    if not user or module is None:
        return False
    if user.is_admin:
        return True
    if user.is_department_head:
        return module.semester.nta_level.programme.department_id == user.department_id
    return lecturer_teaches_module(user, module.id)
