"""Domain exceptions mapped to the standard error envelope."""
from typing import Any, Dict, Optional


class AppError(Exception):
    status_code: int = 400
    code: str = "BAD_REQUEST"
    default_message: str = "Bad request"

    def __init__(self, message: str = "", code: Optional[str] = None,
                 status_code: Optional[int] = None, details: Optional[Dict[str, Any]] = None,
                 headers: Optional[Dict[str, str]] = None):
        self.message = message or self.default_message
        if code:
            self.code = code
        if status_code:
            self.status_code = status_code
        self.details = details or {}
        self.headers = headers
        super().__init__(self.message)


# --- auth ---
class AuthRequired(AppError):
    status_code, code, default_message = 401, "AUTH_REQUIRED", "Authentication required"

class InvalidToken(AppError):
    status_code, code, default_message = 401, "INVALID_TOKEN", "Invalid authentication token"

class TokenExpired(AppError):
    status_code, code, default_message = 401, "TOKEN_EXPIRED", "Token has expired"

class TokenRevoked(AppError):
    status_code, code, default_message = 401, "TOKEN_REVOKED", "Token has been revoked"

class InvalidApiKey(AppError):
    status_code, code, default_message = 401, "INVALID_API_KEY", "Invalid API key"

class PermissionDenied(AppError):
    status_code, code, default_message = 403, "PERMISSION_DENIED", "You do not have permission to perform this action"

class AdminOnly(AppError):
    status_code, code, default_message = 403, "ADMIN_ONLY", "Administrator access required"

class InsufficientScope(AppError):
    status_code, code, default_message = 403, "INSUFFICIENT_SCOPE", "API key does not have the required scope"


# --- users / packages ---
class UserNotFound(AppError):
    status_code, code, default_message = 404, "USER_NOT_FOUND", "User not found"

class UserSuspended(AppError):
    status_code, code, default_message = 403, "USER_SUSPENDED", "User account is suspended"

class PackageNotFound(AppError):
    status_code, code, default_message = 404, "PACKAGE_NOT_FOUND", "Package not found"

class PackageExpired(AppError):
    status_code, code, default_message = 402, "PACKAGE_EXPIRED", "Subscription has expired"

class SubscriptionInactive(AppError):
    status_code, code, default_message = 402, "SUBSCRIPTION_INACTIVE", "Subscription is not active"


# --- quota / rate limit ---
class QuotaExceeded(AppError):
    status_code, code, default_message = 429, "QUOTA_EXCEEDED", "Quota exceeded"

class UploadLimitExceeded(QuotaExceeded):
    code, default_message = "UPLOAD_LIMIT_EXCEEDED", "Monthly face upload quota exceeded"

class SearchLimitExceeded(QuotaExceeded):
    code, default_message = "SEARCH_LIMIT_EXCEEDED", "Monthly face search quota exceeded"

class StorageLimitExceeded(QuotaExceeded):
    code, default_message = "STORAGE_LIMIT_EXCEEDED", "Storage quota exceeded"

class RateLimitExceeded(AppError):
    status_code, code, default_message = 429, "RATE_LIMIT_EXCEEDED", "Rate limit exceeded"


# --- faces ---
class InvalidImage(AppError):
    status_code, code, default_message = 400, "INVALID_IMAGE", "Invalid or unsupported image"

class NoFaceDetected(AppError):
    status_code, code, default_message = 422, "NO_FACE_DETECTED", "No face detected in image"

class MultipleFacesDetected(AppError):
    status_code, code, default_message = 422, "MULTIPLE_FACES_DETECTED", "Multiple faces detected in image"

class DuplicateImage(AppError):
    status_code, code, default_message = 409, "DUPLICATE_IMAGE", "This image has already been uploaded"

class FaceNotFound(AppError):
    status_code, code, default_message = 404, "FACE_NOT_FOUND", "Face not found"

class PersonNotFound(AppError):
    status_code, code, default_message = 404, "PERSON_NOT_FOUND", "Person not found"

class SearchFailed(AppError):
    status_code, code, default_message = 500, "SEARCH_FAILED", "Face search failed"

class EmbeddingFailed(AppError):
    status_code, code, default_message = 500, "EMBEDDING_FAILED", "Failed to generate face embedding"

class StorageFailed(AppError):
    status_code, code, default_message = 503, "STORAGE_FAILED", "Failed to store file"


# --- generic ---
class NotFound(AppError):
    status_code, code, default_message = 404, "NOT_FOUND", "Resource not found"

class Conflict(AppError):
    status_code, code, default_message = 409, "CONFLICT", "Resource conflict"

class ValidationFailed(AppError):
    status_code, code, default_message = 422, "VALIDATION_ERROR", "Validation failed"

class ServiceUnavailable(AppError):
    status_code, code, default_message = 503, "SERVICE_UNAVAILABLE", "Service unavailable"
