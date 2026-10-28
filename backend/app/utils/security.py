from __future__ import annotations

import os


def provider_status() -> dict[str, dict[str, str]]:
    """Report whether each external provider looks configured, WITHOUT ever
    returning the credential value itself."""

    def _status(*env_vars: str) -> str:
        return "configured" if all(os.environ.get(v) for v in env_vars) else "not_configured"

    return {
        "llm": {
            "status": _status("OPENAI_API_KEY"),
            "detail": "OpenAI-compatible endpoint used for parsing/extraction/classification",
        },
        "exa": {
            "status": _status("EXA_API_KEY"),
            "detail": "Optional Exa search source",
        },
        "mailerlite": {
            "status": _status(
                "MAILERLITE_API_TOKEN", "MAILERLITE_SENDER_EMAIL", "MAILERLITE_SENDER_NAME"
            ),
            "detail": "Required for live sending (default EMAIL_PROVIDER)",
        },
        "gmail": {
            "status": _status("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD"),
            "detail": "Legacy/optional -- only used if EMAIL_PROVIDER=gmail is set explicitly",
        },
        "webclaw": {
            "status": "configured" if os.environ.get("WEBCLAW_API_KEY") else "optional",
            "detail": "Directory-crawl source; has a working fallback with no key",
        },
    }
