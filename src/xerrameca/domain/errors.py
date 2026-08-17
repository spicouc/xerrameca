from __future__ import annotations


class XerramecaError(Exception):
    """Base domain error with an HTTP-friendly status code."""

    status_code = 400

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class NotFoundError(XerramecaError):
    status_code = 404


class ForbiddenError(XerramecaError):
    status_code = 403


class ConflictError(XerramecaError):
    status_code = 409


class LockedError(XerramecaError):
    status_code = 423


class ValidationError(XerramecaError):
    status_code = 422


class ProviderUnavailableError(XerramecaError):
    status_code = 503
