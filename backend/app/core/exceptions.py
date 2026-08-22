"""
Custom exception types + centralized handlers.

Every error response (validation, auth, business-rule) is normalized to:
    {"success": false, "error": {"code": "...", "message": "..."}}
so frontend error handling never has to branch on FastAPI's default shape
vs. a custom one.
"""
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded


class AppError(Exception):
    """Base class for all deliberate, business-level API errors."""

    status_code: int = status.HTTP_400_BAD_REQUEST
    code: str = "bad_request"

    def __init__(
        self,
        message: str,
        code: str | None = None,
        status_code: int | None = None,
        details: dict | None = None,
    ):
        self.message = message
        self.details = details or {}
        if code:
            self.code = code
        if status_code:
            self.status_code = status_code
        super().__init__(message)


class InvalidCredentialsError(AppError):
    """Used for BOTH 'no such user' and 'wrong password' - never distinguish
    the two to a client, otherwise the login endpoint becomes a user-enumeration oracle."""

    status_code = status.HTTP_401_UNAUTHORIZED
    code = "invalid_credentials"

    def __init__(self, message: str = "Incorrect email or password"):
        super().__init__(message)


class InvalidTokenError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "invalid_token"

    def __init__(self, message: str = "Invalid or expired token"):
        super().__init__(message)


class ForbiddenError(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "forbidden"

    def __init__(self, message: str = "You do not have access to this resource"):
        super().__init__(message)


class EmailAlreadyRegisteredError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "email_already_registered"

    def __init__(self, message: str = "An account with this email already exists"):
        super().__init__(message)


class OAuthAccountLinkingRequiredError(AppError):
    """Raised when a Google login matches an existing password account but
    Google did NOT report the email as verified - we refuse to auto-link and
    ask the user to confirm ownership via their existing password instead."""

    status_code = status.HTTP_403_FORBIDDEN
    code = "oauth_link_confirmation_required"

    def __init__(
        self,
        message: str = (
            "An account with this email already exists. Please log in with your "
            "password to link your Google account."
        ),
        link_token: str | None = None,
    ):
        super().__init__(message, details={"link_token": link_token} if link_token else {})


class OnboardingNotFoundError(AppError):
    """Raised when a step that depends on the system record is called before step 1."""
    status_code = status.HTTP_404_NOT_FOUND
    code = "onboarding_not_found"

    def __init__(self, message: str = "Onboarding not started. Complete the system step first."):
        super().__init__(message)


class SystemTypeConflictError(AppError):
    """Raised when battery is sent for On-grid, or grid is sent for Off-grid."""
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    code = "system_type_conflict"

    def __init__(self, message: str = "This step is not applicable for the chosen system type."):
        super().__init__(message)


class OnboardingAlreadyCompleteError(AppError):
    """Raised on POST /onboarding/complete when already complete."""
    status_code = status.HTTP_409_CONFLICT
    code = "onboarding_already_complete"

    def __init__(self, message: str = "Onboarding is already complete."):
        super().__init__(message)


def _error_body(code: str, message: str, details: dict | None = None) -> dict:
    return {"success": False, "error": {"code": code, "message": message, **(details or {})}}


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body(exc.code, exc.message, exc.details),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Flatten pydantic's verbose error list into one human-readable message.
        first = exc.errors()[0] if exc.errors() else {}
        field = ".".join(str(loc) for loc in first.get("loc", []) if loc != "body")
        message = f"{field}: {first.get('msg')}" if field else "Invalid request payload"
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=_error_body("validation_error", message),
        )

    @app.exception_handler(RateLimitExceeded)
    async def rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content=_error_body("rate_limited", "Too many requests, please try again later"),
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        # Last-resort catch-all so we never leak stack traces/internal details to clients.
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_error_body("internal_error", "An unexpected error occurred"),
        )
