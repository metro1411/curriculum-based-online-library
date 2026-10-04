"""Response optimisations for slow and metered mobile connections.

* ``asset_url()`` adds a content hash to CSS/JS URLs, so browsers can keep
  them for a year and still pick up every deploy immediately.
* Text responses (HTML, CSS, JS, JSON, SVG) are gzip-compressed when the
  browser supports it, typically a 70–80% saving on mobile data.
* Signed-in HTML is marked ``private`` so shared proxies never store a
  student's or lecturer's pages.

Standard library only: no extra dependency for the deployment to install.
"""

from __future__ import annotations

import gzip
import hashlib
import os

from flask import request, url_for
from flask_login import current_user

COMPRESSIBLE_TYPES = {
    "text/html", "text/css", "text/plain", "text/javascript", "application/javascript",
    "application/json", "application/manifest+json", "image/svg+xml",
}
MIN_COMPRESS_BYTES = 1024
ONE_YEAR = 31536000

_versions: dict[str, tuple[float, str]] = {}


def _asset_version(static_folder, filename):
    path = os.path.join(static_folder, filename)
    try:
        modified = os.path.getmtime(path)
    except OSError:
        return None
    cached = _versions.get(filename)
    if cached is None or cached[0] != modified:
        with open(path, "rb") as handle:
            cached = (modified, hashlib.sha256(handle.read()).hexdigest()[:12])
        _versions[filename] = cached
    return cached[1]


def _should_compress(response):
    if response.status_code != 200:
        return False
    # Files (static assets) report as streamed but are safe to read whole;
    # genuinely streamed generators are left alone.
    if response.is_streamed and not response.direct_passthrough:
        return False
    if "Content-Encoding" in response.headers or response.mimetype not in COMPRESSIBLE_TYPES:
        return False
    if "gzip" not in request.headers.get("Accept-Encoding", "").lower():
        return False
    length = response.calculate_content_length()
    return length is None or length >= MIN_COMPRESS_BYTES


def init_app(app):
    def asset_url(filename):
        version = _asset_version(app.static_folder, filename)
        if version is None:
            return url_for("static", filename=filename)
        return url_for("static", filename=filename, v=version)

    app.jinja_env.globals["asset_url"] = asset_url

    @app.after_request
    def optimise_response(response):
        if request.path.startswith("/static/") and request.args.get("v") and response.status_code == 200:
            response.cache_control.public = True
            response.cache_control.max_age = ONE_YEAR
            response.cache_control.immutable = True
        elif response.mimetype == "text/html" and current_user.is_authenticated:
            response.cache_control.private = True
            response.cache_control.no_cache = True

        if _should_compress(response):
            response.direct_passthrough = False
            data = response.get_data()
            if len(data) >= MIN_COMPRESS_BYTES:
                response.set_data(gzip.compress(data, compresslevel=6))
                response.headers["Content-Encoding"] = "gzip"
                etag, weak = response.get_etag()
                if etag and not weak:
                    # The compressed body differs byte-for-byte; mark the tag weak
                    # (as nginx does) so revalidation still matches.
                    response.set_etag(etag, weak=True)
        response.vary.add("Accept-Encoding")
        return response
