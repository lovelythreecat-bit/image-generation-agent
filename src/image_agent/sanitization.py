"""Sanitize diagnostic messages, never evidence IDs or verbatim user quotes."""

import re


def redact(message):
    text = str(message)
    text = re.sub(r"data:[^\s\"']+", "[image redacted]", text, flags=re.I)
    text = re.sub(
        r"(?i)(bearer\s+|(?:api[_-]?key|token|authorization)[\"']?\s*[=:]\s*[\"']?)[^\s,;\"']+",
        r"\1[redacted]",
        text,
    )
    text = re.sub(r"sk-[\w-]+|[A-Za-z0-9+/]{64,}={0,2}", "[redacted]", text)
    text = re.sub(
        r"[A-Za-z]:[/\\][^\s\"']*|(?<!\w)/(?:[^/\s]+/)+[^\s\"']*", "[path redacted]", text
    )
    return text[:500]
