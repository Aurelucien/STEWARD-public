"""Stable typed failures for the provider-neutral LLM Context Layer."""

from ..errors import StewardError


class LLMContextError(StewardError):
    code = "LLM_CONTEXT_INVALID"
    exit_code = 2

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


class LLMContextRequestError(LLMContextError):
    code = "LLM_CONTEXT_REQUEST_INVALID"


class LLMContextInvariantError(LLMContextError):
    code = "LLM_CONTEXT_INVARIANT_VIOLATION"


class LLMContextCanonicalError(LLMContextError):
    code = "LLM_CONTEXT_CANONICAL_INVARIANT_VIOLATION"


class LLMModelCallError(LLMContextError):
    code = "LLM_MODEL_CALL_FAILED"


class LLMOutputParseError(LLMContextError):
    code = "LLM_OUTPUT_PARSE_INVALID"

    def __init__(self, code: str, message: str | None = None, *, failure_subtype: str | None = None) -> None:
        self.failure_subtype = failure_subtype
        super().__init__(code, message)


class LLMOutputValidationError(LLMContextError):
    code = "LLM_OUTPUT_VALIDATION_INVALID"


class LLMUnsupportedTaskDomainError(LLMContextError):
    code = "LLM_TASK_DOMAIN_UNSUPPORTED"
