"""Envelope schemas used for OpenAPI documentation."""
from typing import Any, Dict, Generic, Optional, TypeVar, Union

from pydantic import BaseModel, Field

T = TypeVar("T")


class ErrorDetail(BaseModel):
    code: str = Field(examples=["QUOTA_EXCEEDED"])
    message: str = Field(examples=["Monthly face search quota exceeded"])
    details: Dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    success: bool = False
    error: ErrorDetail
    meta: Dict[str, Any] = Field(default_factory=dict)


class SuccessResponse(BaseModel, Generic[T]):
    success: bool = True
    data: Optional[T] = None
    meta: Dict[str, Any] = Field(default_factory=dict)


class Page(BaseModel):
    total: int
    limit: int
    offset: int


def error_responses(*codes: int) -> Dict[Union[int, str], Dict[str, Any]]:
    names = {400: "Bad request / invalid image", 401: "Authentication required or token invalid/expired/revoked",
             402: "Subscription expired or inactive", 403: "Permission denied / suspended / insufficient scope",
             404: "Not found", 409: "Conflict", 422: "Validation error / no face / multiple faces",
             429: "Rate limit or quota exceeded", 500: "Internal error", 503: "Dependency unavailable"}
    return {c: {"model": ErrorResponse, "description": names.get(c, "Error")} for c in codes}


AUTH_ERRORS = error_responses(401, 402, 403, 429)
