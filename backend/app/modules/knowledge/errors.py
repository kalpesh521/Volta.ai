"""Business errors for the knowledge API. Handled by the AppError envelope."""
from fastapi import status

from app.core.exceptions import AppError


class KnowledgeNotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "knowledge_not_found"

    def __init__(self, message: str = "Document not found.") -> None:
        super().__init__(message)


class KnowledgeRejectedError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    code = "knowledge_rejected"

    def __init__(self, message: str) -> None:
        super().__init__(message)


class DocumentConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "document_conflict"

    def __init__(self, message: str) -> None:
        super().__init__(message)


class DocumentTooLargeError(AppError):
    status_code = status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
    code = "document_too_large"

    def __init__(self, message: str) -> None:
        super().__init__(message)
