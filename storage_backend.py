"""Resource file storage with an optional private Supabase Storage backend.

The local folder is intentionally retained as the default so contributors can
run the application without cloud accounts. When the Supabase settings are
present, every resource is also kept in the configured private bucket and can
survive restarts of an ephemeral web host such as a free Render service.
"""

from __future__ import annotations

import io
import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from flask import current_app, send_file

from config import RESOURCE_UPLOAD_DIR


class StorageError(RuntimeError):
    """Raised when a configured cloud-storage operation cannot complete."""


def cloud_storage_enabled():
    return bool(
        current_app.config.get("SUPABASE_URL")
        and current_app.config.get("SUPABASE_SERVICE_ROLE_KEY")
    )


def _local_path(stored_filename):
    return os.path.join(RESOURCE_UPLOAD_DIR, stored_filename)


def _object_url(stored_filename):
    base_url = current_app.config["SUPABASE_URL"]
    bucket = quote(current_app.config["SUPABASE_STORAGE_BUCKET"], safe="")
    object_name = quote(stored_filename, safe="/")
    return f"{base_url}/storage/v1/object/{bucket}/{object_name}"


def _bucket_url(bucket_name=None):
    base_url = current_app.config["SUPABASE_URL"]
    if bucket_name is None:
        return f"{base_url}/storage/v1/bucket"
    return f"{base_url}/storage/v1/bucket/{quote(bucket_name, safe='')}"


def _headers(mime_type=None):
    key = current_app.config["SUPABASE_SERVICE_ROLE_KEY"]
    headers = {"Authorization": f"Bearer {key}", "apikey": key}
    if mime_type:
        headers["Content-Type"] = mime_type
    return headers


def ensure_storage_bucket():
    """Create the configured private Supabase bucket if it is missing.

    A first public deployment should not fail merely because the storage
    container has not been created manually in the Supabase dashboard. The
    service-role/secret key stays on the server and is used only to provision
    this one private bucket. Existing buckets are left unchanged.
    """
    if not cloud_storage_enabled():
        return

    bucket = current_app.config["SUPABASE_STORAGE_BUCKET"]
    try:
        _request(Request(_bucket_url(bucket), method="HEAD", headers=_headers()))
        return
    except FileNotFoundError:
        pass

    payload = json.dumps({
        "id": bucket,
        "name": bucket,
        "public": False,
        "file_size_limit": current_app.config["MAX_CONTENT_LENGTH"],
    }).encode("utf-8")
    request = Request(
        _bucket_url(), data=payload, method="POST",
        headers=_headers("application/json"),
    )
    try:
        _request(request)
    except StorageError as error:
        raise StorageError(
            f"Could not create the Supabase Storage bucket '{bucket}'."
        ) from error



def _request(request):
    try:
        with urlopen(request, timeout=45) as response:
            return response.read()
    except HTTPError as error:
        if error.code == 404:
            raise FileNotFoundError from error
        raise StorageError(f"Cloud storage returned HTTP {error.code}.") from error
    except (URLError, OSError) as error:
        raise StorageError("Cloud storage could not be reached.") from error


def upload_local_file(local_path, stored_filename, mime_type=None):
    """Copy a locally staged file to private cloud storage when configured."""
    if not cloud_storage_enabled():
        return
    try:
        with open(local_path, "rb") as handle:
            request = Request(
                _object_url(stored_filename), data=handle.read(), method="POST",
                headers={**_headers(mime_type or "application/octet-stream"), "x-upsert": "false"},
            )
        _request(request)
    except FileNotFoundError as error:
        raise StorageError("The configured Supabase Storage bucket was not found.") from error


def stage_uploaded_file(file_storage, stored_filename, mime_type=None):
    """Save an incoming file for extraction and safely mirror it to cloud storage."""
    local_path = _local_path(stored_filename)
    file_storage.save(local_path)
    try:
        upload_local_file(local_path, stored_filename, mime_type)
    except Exception:
        try:
            os.remove(local_path)
        except OSError:
            pass
        raise
    return local_path


def read_file_bytes(stored_filename):
    if cloud_storage_enabled():
        request = Request(_object_url(stored_filename), method="GET", headers=_headers())
        return _request(request)
    with open(_local_path(stored_filename), "rb") as handle:
        return handle.read()


def read_file_text(stored_filename):
    return read_file_bytes(stored_filename).decode("utf-8", errors="ignore")


def send_resource_file(resource, as_attachment=False):
    """Send a resource from whichever backend is active without exposing cloud keys."""
    content = read_file_bytes(resource.stored_filename)
    response = send_file(
        io.BytesIO(content),
        mimetype=resource.mime_type or "application/octet-stream",
        as_attachment=as_attachment,
        download_name=(resource.original_filename or resource.stored_filename),
        conditional=True,
    )
    # Prevent browsers from guessing an executable/HTML type for an uploaded
    # document even if a proxy or old database row contains a bad MIME value.
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def delete_resource_file(stored_filename):
    """Remove a resource from local staging and cloud storage where applicable."""
    local_path = _local_path(stored_filename)
    try:
        os.remove(local_path)
    except FileNotFoundError:
        pass
    except OSError:
        current_app.logger.warning("Could not remove local resource file %s", stored_filename)

    if not cloud_storage_enabled():
        return
    try:
        request = Request(_object_url(stored_filename), method="DELETE", headers=_headers())
        _request(request)
    except FileNotFoundError:
        pass
    except StorageError:
        current_app.logger.warning("Could not remove cloud resource file %s", stored_filename)
