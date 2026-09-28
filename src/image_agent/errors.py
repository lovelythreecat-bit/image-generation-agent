import re

from pydantic import ValidationError

from .models import ErrorInfo


def redact(message):
    text = str(message)
    text = re.sub(r"data:[^\s\"']+", "[image redacted]", text, flags=re.I)
    text = re.sub(
        r"(?i)(bearer\s+|(?:api[_-]?key|token|authorization)\s*[=:]\s*)[^\s,;]+",
        r"\1[redacted]",
        text,
    )
    text = re.sub(r"sk-[\w-]+|[A-Za-z0-9+/]{64,}={0,2}", "[redacted]", text)
    text = re.sub(
        r"[A-Za-z]:[/\\][^\s\"']*|(?<!\w)/(?:[^/\s]+/)+[^\s\"']*", "[path redacted]", text
    )
    return text[:500]


class AgentError(Exception):
    kind = "validation"
    code = "validation"

    def __init__(self, message, *, code=None, kind=None, status_code=None):
        self.message = redact(message)
        self.kind = kind or self.kind
        self.code = code or self.code
        self.status_code = status_code
        super().__init__(self.message)


class ConfigurationError(AgentError):
    kind = code = "configuration"


class InputImageError(AgentError):
    kind = code = "input"


class StaleAnalysisError(AgentError):
    kind = code = "stale_analysis"


class ProviderError(AgentError):
    def __init__(self, message, *, kind="protocol", code=None, status_code=None):
        super().__init__(
            message, kind=kind, code=code or f"provider_{kind}", status_code=status_code
        )


class OutputError(AgentError):
    kind = code = "output"

    def __init__(self, message, *, result=None):
        super().__init__(message)
        self.result = result


def make_error_info(error):
    if isinstance(error, ValidationError):
        return ErrorInfo(
            code="validation",
            kind="validation",
            message=redact(
                "; ".join(
                    f"{'.'.join(map(str, e['loc']))}: {e['type']}"
                    for e in error.errors(include_input=False, include_context=False)
                )
            ),
        )
    return ErrorInfo(
        code=error.code,
        kind=error.kind,
        message=redact(error.message),
        status_code=error.status_code,
        retryable=error.kind == "transport"
        or (error.kind == "http" and error.status_code in (429, 502, 503, 504)),
    )
