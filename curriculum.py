"""
curriculum.py
-------------
Shared helpers for walking the
Department -> Programme -> NtaLevel -> Semester -> Module
hierarchy. Used by both the student "browse the archive" routes and the
lecturer "upload wizard" routes, so the two flows stay in lock-step and a
future change to the hierarchy only needs to happen once.
"""

from flask import url_for, render_template

from models import Department, Programme, NtaLevel, Semester, Module


def get_department_or_404(slug):
    return Department.query.filter_by(slug=slug).first_or_404()


def get_programme_or_404(department, slug):
    return Programme.query.filter_by(department_id=department.id, slug=slug).first_or_404()


def get_level_or_404(programme, level_number):
    return NtaLevel.query.filter_by(
        programme_id=programme.id, level_number=level_number
    ).first_or_404()


def get_semester_or_404(level, semester_number):
    return Semester.query.filter_by(
        nta_level_id=level.id, semester_number=semester_number
    ).first_or_404()


def get_module_or_404(module_id):
    return Module.query.get_or_404(module_id)


_ENDPOINTS = {
    "student": {
        "root": "student.departments",
        "department": "student.programmes",
        "programme": "student.levels",
        "level": "student.semesters",
        "semester": "student.modules",
    },
    "lecturer": {
        "root": "lecturer.upload_departments",
        "department": "lecturer.upload_programmes",
        "programme": "lecturer.upload_levels",
        "level": "lecturer.upload_semesters",
        "semester": "lecturer.upload_modules",
    },
}

_ROOT_LABEL = {
    "student": "Academic Archive",
    "lecturer": "Upload Wizard",
}


def build_breadcrumbs(section, department=None, programme=None, level=None,
                       semester=None, module=None):
    """Return a list of (label, url_or_None) tuples for a breadcrumb trail."""
    ep = _ENDPOINTS[section]
    crumbs = [(_ROOT_LABEL[section], url_for(ep["root"]))]

    if department:
        crumbs.append((department.name, url_for(ep["department"], dept_slug=department.slug)))
    if programme:
        crumbs.append((programme.name, url_for(
            ep["programme"], dept_slug=department.slug, prog_slug=programme.slug)))
    if level:
        crumbs.append((level.label, url_for(
            ep["level"], dept_slug=department.slug, prog_slug=programme.slug,
            level_number=level.level_number)))
    if semester:
        crumbs.append((semester.label, url_for(
            ep["semester"], dept_slug=department.slug, prog_slug=programme.slug,
            level_number=level.level_number, semester_number=semester.semester_number)))
    if module:
        crumbs.append((module.name, None))

    return crumbs


def coming_soon_response(section, entity_label, entity_kind, back_url):
    """Render the shared 'Coming Soon' page for an inactive curriculum node."""
    return render_template(
        "coming_soon.html",
        entity_label=entity_label,
        entity_kind=entity_kind,
        back_url=back_url,
        section=section,
    )
