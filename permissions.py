"""Central role-based access rules.

Blueprints already guard whole areas with a role check in ``before_request``.
The helpers here express the finer, cross-cutting rules in one place so the
same answer is given by HTML routes, JSON endpoints and tests:

* students read published curriculum and their own data only;
* lecturers manage resources they uploaded, inside modules they are approved
  to teach, and never change curriculum structure;
* heads of department upload the prospectus (which decides every module)
  and manage their own department's lecturers and students.
"""

from __future__ import annotations


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
    if user.is_department_head:
        return module.semester.nta_level.programme.department_id == user.department_id
    return lecturer_teaches_module(user, module.id)
