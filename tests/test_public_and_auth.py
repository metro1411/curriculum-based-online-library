"""Public pages, security headers, sign-in and sign-out."""

from conftest import LECTURER, STUDENT


def test_landing_and_health(client):
    assert client.get("/").status_code == 200
    health = client.get("/health")
    assert health.status_code == 200
    assert health.get_json() == {"status": "ok"}
    assert "frame-ancestors 'none'" in health.headers["Content-Security-Policy"]


def test_protected_pages_redirect_to_the_single_login(client):
    for path in ("/dashboard", "/lecturer/dashboard", "/department"):
        response = client.get(path)
        assert response.status_code == 302
        assert "/login?next=" in response.headers["Location"]


def test_one_login_routes_each_role_to_its_workspace(app):
    for (identifier, password), destination in ((STUDENT, "/dashboard"), (LECTURER, "/lecturer/dashboard")):
        response = app.test_client().post("/login", data={"identifier": identifier, "password": password})
        assert response.status_code == 302
        assert response.headers["Location"].endswith(destination)


def test_wrong_password_is_rejected(client):
    response = client.post("/login", data={"identifier": STUDENT[0], "password": "wrong-password"})
    assert response.status_code == 200
    assert b"Incorrect ID number, email or password" in response.data


def test_login_honours_safe_next_and_ignores_external_next(app):
    safe = app.test_client().post("/login", data={
        "identifier": STUDENT[0], "password": STUDENT[1], "next": "/downloads",
    })
    assert safe.headers["Location"].endswith("/downloads")
    external = app.test_client().post("/login", data={
        "identifier": STUDENT[0], "password": STUDENT[1], "next": "https://evil.example/",
    })
    assert external.headers["Location"].endswith("/dashboard")


def test_legacy_role_login_urls_redirect_and_keep_working(client):
    page = client.get("/student/login?next=/downloads")
    assert page.status_code == 307
    assert page.headers["Location"].endswith("/login?next=/downloads")
    # 307 preserves POST, so old forms still sign in.
    posted = client.post("/lecturer/login", data={"identifier": LECTURER[0], "password": LECTURER[1]},
                         follow_redirects=True)
    assert posted.status_code == 200
    assert b"Welcome back" in posted.data


def test_logout_is_safe_without_a_session(client):
    response = client.get("/logout")
    assert response.status_code == 302
    assert "next=" not in response.headers["Location"]


def test_registration_page_has_no_inline_script(client):
    page = client.get("/register")
    assert page.status_code == 200
    assert b"<script>" not in page.data
    assert page.data.count(b'name="department_id"') == 1
