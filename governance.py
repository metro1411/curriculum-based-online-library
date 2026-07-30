"""Small governance helpers shared by privileged workflows."""

from __future__ import annotations

import json

from flask import has_request_context, request
from flask_login import current_user

from extensions import db
from models import AuditLog


def record_audit(action, target_type, *, target_id=None, target_label=None,
                 department_id=None, details=None, actor=None):
    """Stage a privacy-safe audit entry in the current transaction."""
    if actor is None and has_request_context() and current_user.is_authenticated:
        actor = current_user
    ip_address = None
    if has_request_context():
        ip_address = (request.remote_addr or "")[:64] or None
    row = AuditLog(
        actor_id=getattr(actor, "id", None),
        department_id=department_id or getattr(actor, "department_id", None),
        action=(action or "")[:80],
        target_type=(target_type or "")[:80],
        target_id=str(target_id)[:80] if target_id is not None else None,
        target_label=(target_label or "")[:240] or None,
        details_json=json.dumps(details or {}, ensure_ascii=False, sort_keys=True)[:4000],
        ip_address=ip_address,
    )
    db.session.add(row)
    return row
