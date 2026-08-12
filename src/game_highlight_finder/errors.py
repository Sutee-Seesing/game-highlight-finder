"""Expected application errors and stable error categories."""

from __future__ import annotations

from enum import StrEnum


class ErrorCategory(StrEnum):
    CONFIGURATION = "configuration"
    DEPENDENCY = "dependency"
    SOURCE = "source/input"
    VALIDATION = "validation"
    STORAGE = "storage"
    COST = "cost"
    BUDGET = "budget"
    PROVIDER = "provider"
    INTERNAL = "internal"


class AppError(Exception):
    """An expected, human-actionable application error."""

    def __init__(
        self,
        category: ErrorCategory,
        message: str,
        *,
        hint: str | None = None,
        exit_code: int = 2,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.message = message
        self.hint = hint
        self.exit_code = exit_code


class ConfigError(AppError):
    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(ErrorCategory.CONFIGURATION, message, hint=hint)


class DependencyError(AppError):
    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(ErrorCategory.DEPENDENCY, message, hint=hint)


class SourceError(AppError):
    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(ErrorCategory.SOURCE, message, hint=hint)


class ValidationError(AppError):
    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(ErrorCategory.VALIDATION, message, hint=hint)


class StorageError(AppError):
    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(ErrorCategory.STORAGE, message, hint=hint)


class CostGateError(AppError):
    """A paid-call cost or provider contract check failed closed."""

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(ErrorCategory.COST, message, hint=hint)


class BudgetExceededError(CostGateError):
    """The hard monthly budget cannot safely accommodate a reservation."""

    def __init__(
        self,
        message: str = "Monthly hard budget would be exceeded.",
        *,
        hint: str | None = None,
    ) -> None:
        super().__init__(message, hint=hint)
        self.category = ErrorCategory.BUDGET


class CostIntegrityError(CostGateError):
    """Persisted actual billing exceeded the conservative reservation."""


class CostSafetyHoldError(CostGateError):
    """New reservations are blocked until an overage is explicitly acknowledged."""


class ProviderContractError(CostGateError):
    """A provider-neutral contract or exact registry lookup failed."""

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message, hint=hint)
        self.category = ErrorCategory.PROVIDER
