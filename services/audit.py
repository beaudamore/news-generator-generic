"""Audit log: one row per state-changing action, plus the same line on the process logger."""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from services import logging_setup, tenancy
from services.db import session
from services.models import AuditEvent

LOGGER = logging.getLogger("news_pipeline")


def _dump(value: Any) -> Optional[str]:
    if value is None:
        return None
    return json.dumps(value, default=str, sort_keys=True)


def record(
    *,
    tenant_id: Optional[int],
    actor: str,
    action: str,
    object_type: str,
    object_id: Optional[str] = None,
    before: Any = None,
    after: Any = None,
    ip: Optional[str] = None,
    correlation_id: Optional[str] = None,
) -> None:
    if actor == "system":
        actor = tenancy.current_actor()
    ip = ip or logging_setup.client_ip_var.get()
    correlation_id = correlation_id or logging_setup.correlation_id_var.get()
    with session() as db:
        db.add(
            AuditEvent(
                tenant_id=tenant_id,
                actor=actor,
                action=action,
                object_type=object_type,
                object_id=object_id,
                before=_dump(before),
                after=_dump(after),
                ip=ip,
                correlation_id=correlation_id,
            )
        )
        db.commit()
    LOGGER.info("[audit] tenant=%s actor=%s %s %s/%s", tenant_id, actor, action, object_type, object_id,
                extra={"event": "audit", "actor": actor, "action": action, "object_type": object_type, "object_id": object_id, "tenant_id": tenant_id})
