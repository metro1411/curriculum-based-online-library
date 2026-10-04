"""SOMA integration boundary.

SOMA is DIT's authoritative system for official student and registration
records. Smart DIT Learning Hub must never replace it or write to it.

IMPORTANT: no official SOMA API specification has been provided to this
project. This module therefore defines only:

* ``SomaStudentRecord`` - the academic context the hub would *consume*;
* ``SomaAdapter`` - the read-only interface any real adapter must implement;
* ``UnconfiguredSomaAdapter`` - the default, which reports that SOMA is not
  connected instead of pretending;
* ``HttpSomaAdapter`` - a transport shell that refuses to run until the
  real endpoint path, authentication scheme and response mapping are
  supplied from DIT's official documentation;
* ``MockSomaAdapter`` - an in-memory adapter for automated tests and local
  demonstrations only. It is rejected in production.

No endpoint path, credential format, field name or database schema below is
a statement about how SOMA actually works.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class SomaStudentRecord:
    """Academic context the hub can use, as supplied by an authorised adapter."""

    student_id: str
    full_name: str | None = None
    department: str | None = None
    programme: str | None = None
    nta_level: int | None = None
    semester: int | None = None
    academic_year: str | None = None
    registered_module_codes: tuple[str, ...] = field(default_factory=tuple)


class SomaError(Exception):
    """Base class. ``user_message`` is safe to show to staff."""

    status = "error"
    user_message = "SOMA could not provide academic context."

    def __init__(self, detail=None):
        super().__init__(detail or self.user_message)
        self.detail = detail


class SomaNotConfigured(SomaError):
    status = "not_configured"
    user_message = (
        "SOMA integration is not configured. Academic context is supplied internally "
        "until DIT provides an authorised SOMA API."
    )


class SomaAuthenticationError(SomaError):
    status = "auth_failed"
    user_message = "SOMA rejected the configured credentials. Check the SOMA integration settings."


class SomaUnavailable(SomaError):
    status = "unavailable"
    user_message = "SOMA is currently unreachable. Existing academic context was kept unchanged."


class SomaRecordNotFound(SomaError):
    status = "not_found"
    user_message = "SOMA has no record for this student identifier."


class SomaContractError(SomaError):
    status = "contract_error"
    user_message = "The SOMA response could not be interpreted. No academic context was changed."


class SomaAdapter(ABC):
    """Read-only interface. Implementations must not modify SOMA records."""

    name = "abstract"
    read_only = True

    @abstractmethod
    def get_student_record(self, student_identifier: str) -> SomaStudentRecord:
        """Return the student's academic context or raise a ``SomaError``."""

    @abstractmethod
    def health_check(self) -> dict:
        """Return ``{"status": str, "message": str}`` without raising."""


class UnconfiguredSomaAdapter(SomaAdapter):
    name = "none"

    def __init__(self, reason=None):
        self.reason = reason or SomaNotConfigured.user_message

    def get_student_record(self, student_identifier):
        raise SomaNotConfigured(self.reason)

    def health_check(self):
        return {"status": "not_configured", "message": self.reason}


class MockSomaAdapter(SomaAdapter):
    """In-memory adapter for tests and clearly labelled DEMO environments."""

    name = "mock"

    def __init__(self, records=None, *, failure=None):
        self.records = {str(key): value for key, value in (records or {}).items()}
        self.failure = failure  # a SomaError subclass to raise, for failure testing

    def get_student_record(self, student_identifier):
        if self.failure is not None:
            raise self.failure()
        record = self.records.get(str(student_identifier))
        if record is None:
            raise SomaRecordNotFound()
        if isinstance(record, dict):
            record = SomaStudentRecord(
                student_id=str(student_identifier),
                full_name=record.get("full_name"),
                department=record.get("department"),
                programme=record.get("programme"),
                nta_level=record.get("nta_level"),
                semester=record.get("semester"),
                academic_year=record.get("academic_year"),
                registered_module_codes=tuple(record.get("registered_module_codes") or ()),
            )
        return record

    def health_check(self):
        if self.failure is not None:
            return {"status": self.failure.status, "message": self.failure.user_message}
        return {"status": "ok", "message": "DEMO mock adapter - not connected to the real SOMA."}


class HttpSomaAdapter(SomaAdapter):
    """HTTP transport shell for a future, officially documented SOMA API.

    It runs only when *all* of the following are supplied by DIT:
    the base URL, credentials, the student-record endpoint path
    (``SOMA_STUDENT_RECORD_PATH``) and a ``response_mapper`` written against
    the official response schema. Until then it raises ``SomaNotConfigured``.
    """

    name = "http"

    def __init__(self, *, base_url, api_key=None, client_id=None, client_secret=None,
                 student_record_path=None, timeout=10, response_mapper=None, auth_headers=None):
        self.base_url = (base_url or "").rstrip("/") + "/"
        self.api_key = api_key
        self.client_id = client_id
        self.client_secret = client_secret
        self.student_record_path = student_record_path
        self.timeout = timeout
        self.response_mapper = response_mapper
        # How credentials are presented is defined by the official API, so the
        # caller must provide the header builder; nothing is assumed here.
        self.auth_headers = auth_headers

    def missing_requirements(self):
        missing = []
        if self.base_url == "/":
            missing.append("SOMA_API_BASE_URL")
        if not (self.api_key or (self.client_id and self.client_secret)):
            missing.append("SOMA_API_KEY or SOMA_CLIENT_ID/SOMA_CLIENT_SECRET")
        if not self.student_record_path:
            missing.append("SOMA_STUDENT_RECORD_PATH (from the official API specification)")
        if self.auth_headers is None:
            missing.append("authentication scheme (from the official API specification)")
        if self.response_mapper is None:
            missing.append("response mapping (from the official API specification)")
        return missing

    def get_student_record(self, student_identifier):
        missing = self.missing_requirements()
        if missing:
            raise SomaNotConfigured("SOMA integration is incomplete. Missing: " + "; ".join(missing) + ".")
        path = self.student_record_path.replace("{student_id}", quote(str(student_identifier), safe=""))
        payload = self._get_json(path)
        try:
            record = self.response_mapper(payload)
        except Exception as error:
            raise SomaContractError(str(error)) from error
        if not isinstance(record, SomaStudentRecord):
            raise SomaContractError("Response mapper did not return a SomaStudentRecord.")
        return record

    def health_check(self):
        missing = self.missing_requirements()
        if missing:
            return {"status": "not_configured", "message": "Missing: " + "; ".join(missing)}
        return {"status": "configured", "message": "HTTP adapter configured; connectivity is checked on sync."}

    def _get_json(self, path):
        headers = {"Accept": "application/json", **(self.auth_headers(self) or {})}
        request = Request(urljoin(self.base_url, path.lstrip("/")), headers=headers, method="GET")
        try:
            with urlopen(request, timeout=self.timeout) as response:
                body = response.read()
        except HTTPError as error:
            if error.code in (401, 403):
                raise SomaAuthenticationError(f"HTTP {error.code}") from error
            if error.code == 404:
                raise SomaRecordNotFound() from error
            raise SomaUnavailable(f"HTTP {error.code}") from error
        except (URLError, OSError, TimeoutError) as error:
            raise SomaUnavailable(str(error)) from error
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise SomaContractError("Response was not valid JSON.") from error


# Register an official response mapper / auth header builder here once DIT
# publishes the SOMA API contract. Left empty on purpose.
RESPONSE_MAPPER = None
AUTH_HEADERS = None


def get_adapter(config):
    """Build the adapter selected by configuration. Never raises."""
    choice = (config.get("SOMA_ADAPTER") or "none").strip().lower()
    if choice == "mock":
        if config.get("IS_PRODUCTION"):
            return UnconfiguredSomaAdapter("The DEMO mock SOMA adapter is disabled in production.")
        return MockSomaAdapter(_load_mock_records(config.get("SOMA_MOCK_DATA_FILE")))
    if choice == "http":
        return HttpSomaAdapter(
            base_url=config.get("SOMA_API_BASE_URL"),
            api_key=config.get("SOMA_API_KEY") or None,
            client_id=config.get("SOMA_CLIENT_ID") or None,
            client_secret=config.get("SOMA_CLIENT_SECRET") or None,
            student_record_path=config.get("SOMA_STUDENT_RECORD_PATH") or None,
            timeout=config.get("SOMA_TIMEOUT_SECONDS") or 10,
            response_mapper=RESPONSE_MAPPER,
            auth_headers=AUTH_HEADERS,
        )
    return UnconfiguredSomaAdapter()


def _load_mock_records(path):
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}
