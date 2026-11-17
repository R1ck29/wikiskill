"""Sort CLI failures into kinds that say whether waiting helps and whether the whole run must stop.

    kind          meaning                                              retry   stop the run
    auth          not logged in, credentials rejected                  no      yes
    usage_limit   the plan's usage limit (wait for its reset)          no      yes
    billing       no credit, account on hold                           no      yes
    cost_cap      the cost cap of this one call was reached            no      yes
    bad_option    the CLI rejected an argument (another version)       no      yes
    bad_model     the model name cannot be used                        no      yes
    not_found     the command is not installed                         no      no
    rate_limit    short-term rate limit (HTTP 429)                     yes     no
    overloaded    server-side trouble (5xx, 529)                       yes     no
    timeout       the call ran longer than allowed                     yes     no
    incomplete    the output stopped before a result                   yes     no
    unknown       anything else                                        yes     no

Structured fields decide first, the wording second - except for usage limits: they arrive as HTTP 429
with the category `rate_limit` (measured: "You've hit your 5-hour limit · resets 5:10am"), so their
wording is checked before everything else. Unrecognised failures stay `unknown` and are retried, as
every failure was before this table existed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

RETRYABLE = frozenset({"rate_limit", "overloaded", "timeout", "incomplete", "unknown"})
FATAL = frozenset({"auth", "usage_limit", "billing", "cost_cap", "bad_option", "bad_model"})
KINDS = RETRYABLE | FATAL | {"not_found"}

# error categories Claude Code puts on assistant events and system/api_retry events
_CATEGORIES = {
    "authentication_failed": "auth", "oauth_org_not_allowed": "auth", "cloud_credential_error": "auth",
    "billing_error": "billing", "account_on_hold": "billing",
    "model_not_found": "bad_model",
    "rate_limit": "rate_limit", "overloaded": "overloaded", "server_error": "overloaded",
}
_USAGE_LIMIT = re.compile(r"usage limit|hit your [\w\- ]{0,40}limit|limit reached|limit will reset", re.IGNORECASE)
_WORDING = [(kind, re.compile(pattern, re.IGNORECASE)) for kind, pattern in (
    ("auth", r"not logged in|/login|invalid api key|authenticat|unauthorized|\b40[13]\b|oauth token"),
    ("billing", r"credit balance|billing|payment required|\b402\b"),
    ("bad_model", r"model_not_found|model\b[^.\n]{0,80}\b(?:not supported|not found|does not exist|not available)"
                  r"|invalid model"),
    ("rate_limit", r"\b429\b|rate.?limit|too many requests"),
    ("overloaded", r"overloaded|\b(?:500|502|503|504|529)\b|server error"),
)]
_BAD_OPTION = re.compile(r"unknown (?:option|argument)|unexpected argument|unrecognized (?:option|argument)"
                         r"|invalid value [^\n]{0,80} for '?-", re.IGNORECASE)
_SUBTYPES = {"error_max_budget_usd": "cost_cap"}


@dataclass(frozen=True)
class CallError:
    kind: str
    message: str

    @property
    def retryable(self) -> bool:
        return self.kind in RETRYABLE

    @property
    def fatal(self) -> bool:
        return self.kind in FATAL


def classify(message: str, category: str | None = None, status: int | None = None,
             subtype: str | None = None) -> CallError:
    """The kind of a failure a CLI reported in its output (`message` is what it said)."""
    return CallError(_kind(message, category, status, subtype), message)


def _kind(message: str, category: str | None, status: int | None, subtype: str | None) -> str:
    if subtype in _SUBTYPES:
        return _SUBTYPES[subtype]
    if _USAGE_LIMIT.search(message):
        return "usage_limit"
    if category in _CATEGORIES:
        return _CATEGORIES[category]
    if isinstance(status, int) and not isinstance(status, bool):
        if status in (401, 403):
            return "auth"
        if status == 402:
            return "billing"
        if status == 429:
            return "rate_limit"
        if status >= 500:
            return "overloaded"
    return next((kind for kind, pattern in _WORDING if pattern.search(message)), "unknown")


def classify_exit(returncode: int, stderr: str) -> CallError:
    """The kind of a failure when the CLI exited without usable output (`stderr` is all there is)."""
    text = stderr.strip()[-2000:]
    if _BAD_OPTION.search(text):
        return CallError("bad_option", text)
    return classify(text)
