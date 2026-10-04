"""Mobile performance and reliability: caching, compression, offline support."""

import gzip
import re


def test_static_assets_are_versioned_and_cached_for_a_year(client):
    html = client.get("/").get_data(as_text=True)
    css = re.search(r'href="(/static/css/tokens\.css\?v=[0-9a-f]+)"', html)
    js = re.search(r'src="(/static/js/main\.js\?v=[0-9a-f]+)"', html)
    assert css and js
    response = client.get(css.group(1))
    assert response.status_code == 200
    cache = response.headers["Cache-Control"]
    assert "max-age=31536000" in cache and "immutable" in cache


def test_text_responses_are_gzipped(client):
    response = client.get("/", headers={"Accept-Encoding": "gzip, deflate, br"})
    assert response.headers.get("Content-Encoding") == "gzip"
    assert "Accept-Encoding" in response.headers.get("Vary", "")
    assert b"Learn with clarity" in gzip.decompress(response.data)


def test_static_css_and_js_are_gzipped(client):
    for path in ("/static/css/learning.css", "/static/js/main.js"):
        plain = client.get(path)
        packed = client.get(path, headers={"Accept-Encoding": "gzip"})
        assert packed.headers.get("Content-Encoding") == "gzip", path
        assert gzip.decompress(packed.data) == plain.data
        assert len(packed.data) < len(plain.data) / 2


def test_uncompressed_when_client_does_not_ask(client):
    response = client.get("/")
    assert "Content-Encoding" not in response.headers
    assert b"Learn with clarity" in response.data


def test_signed_in_pages_are_private(student):
    cache = student.get("/dashboard").headers.get("Cache-Control", "")
    assert "private" in cache and "no-cache" in cache


def test_private_resource_files_are_not_publicly_cacheable(student, seeded):
    response = student.get(f"/resource/{seeded['resource_id']}/file")
    if response.status_code == 200:
        assert "private" in response.headers.get("Cache-Control", "")


def test_service_worker_and_offline_page(client, student):
    worker = client.get("/sw.js")
    assert worker.status_code == 200
    assert worker.mimetype == "text/javascript"
    assert "no-cache" in worker.headers["Cache-Control"]
    assert b"OFFLINE_URL" in worker.data

    # The offline page is cached on devices, so it must never contain user data.
    offline = student.get("/offline")
    assert offline.status_code == 200
    assert b"You're offline" in offline.data
    assert b"Student Account" not in offline.data


def test_manifest_is_linked(client):
    html = client.get("/").get_data(as_text=True)
    assert 'rel="manifest"' in html
    assert client.get("/static/manifest.json").status_code == 200
