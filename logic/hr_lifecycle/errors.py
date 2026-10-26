"""errors.py -- the one error type the hr_lifecycle logic raises. Screens show `message`; the API maps `code` to a status."""
from __future__ import annotations


class HrError(Exception):
    """code: not_found (404), refused (409: the action is not allowed in this state), invalid (422: bad input),
    pii (422: a note looked like personal data), unavailable (503: SharePoint could not be reached; nothing is wrong with the request)."""

    STATUS = {"not_found": 404, "refused": 409, "invalid": 422, "pii": 422, "unavailable": 503}

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    @property
    def status(self) -> int:
        return self.STATUS.get(self.code, 422)
