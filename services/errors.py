"""The one error shape every tool and route returns: {"error": {"code", "stage", "message"}}."""

from __future__ import annotations

from typing import Any, Optional

STATUS_BY_CODE = {
    "not_found": 404,
    "conflict": 409,
    "invalid": 400,
    "unauthorized": 401,
    "forbidden": 403,
    "preflight_failed": 503,
    "pipeline_failed": 500,
    "service_unavailable": 503,
}


class ApiError(Exception):
    def __init__(self, code: str, message: str, stage: Optional[str] = None) -> None:
        if code not in STATUS_BY_CODE:
            raise ValueError(f"unknown error code {code!r}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.stage = stage

    @property
    def status_code(self) -> int:
        return STATUS_BY_CODE[self.code]

    def to_dict(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "stage": self.stage, "message": self.message}}


def not_found(what: str) -> ApiError:
    return ApiError("not_found", f"{what} not found.")


def conflict(message: str) -> ApiError:
    return ApiError("conflict", message)


def invalid(message: str) -> ApiError:
    return ApiError("invalid", message)


def forbidden(message: str) -> ApiError:
    return ApiError("forbidden", message)
