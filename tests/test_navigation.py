"""Sidebar, top bar breadcrumbs and command palette."""

import re


def _sidebar(html):
    return html[html.index('<aside class="sidebar"'):html.index("</aside>")]


def test_signed_out_pages_use_the_public_header(client):
    html = client.get("/").get_data(as_text=True)
    assert 'class="public-header"' in html
    assert 'class="sidebar"' not in html
    assert "data-palette" not in html


def test_student_sidebar_lists_student_destinations_and_marks_active(student):
    html = student.get("/learning-insights").get_data(as_text=True)
    sidebar = _sidebar(html)
    for label in ("Dashboard", "Learning library", "DIT AI", "Ask a lecturer", "Insights", "Downloads"):
        assert label in sidebar
    assert "Upload resource" not in sidebar
    active = re.findall(r'class="sidebar__link is-active"[^>]*data-nav-label="([^"]+)"', sidebar)
    assert active == ["Insights"]


def test_library_stays_active_across_the_curriculum_drill_down(student, seeded):
    html = student.get(f"/module/{seeded['module_id']}").get_data(as_text=True)
    assert re.findall(r'class="sidebar__link is-active"[^>]*data-nav-label="([^"]+)"', _sidebar(html)) == ["Learning library"]
    # The full curriculum trail moves into the top bar.
    crumbs = html[html.index('class="crumbs"'):html.index("</nav>", html.index('class="crumbs"'))]
    assert "Learning library" in crumbs
    assert 'aria-current="page"' in crumbs


def test_lecturer_sidebar_and_palette_search_target(lecturer):
    html = lecturer.get("/lecturer/resources").get_data(as_text=True)
    sidebar = _sidebar(html)
    assert "Upload resource" in sidebar and "Student questions" in sidebar
    assert "Learning library" not in sidebar
    assert 'action="/lecturer/resources"' in html  # palette free text searches own resources


def test_student_palette_searches_the_library(student):
    html = student.get("/dashboard").get_data(as_text=True)
    assert 'action="/search"' in html
    assert html.count("data-palette-option") >= 8


def test_head_of_department_navigation(hod):
    html = hod.get("/department/lecturer-requests").get_data(as_text=True)
    sidebar = _sidebar(html)
    assert "Curriculum" in sidebar and "Audit log" in sidebar
    # Verification requests live under "Lecturers".
    assert re.findall(r'class="sidebar__link is-active"[^>]*data-nav-label="([^"]+)"', sidebar) == ["Lecturers"]
    assert "data-palette-search" not in html  # HOD palette only jumps between pages
