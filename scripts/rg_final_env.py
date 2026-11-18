"""Credential policy shared by final-review workers and dynamic checks.

``preserve_model_auth=False`` is the untrusted repository-test/probe policy.
``preserve_model_auth=True`` additionally retains only explicitly named model
backend credentials needed by a trusted selector or independent review worker.
Provider-write credentials are never retained in either mode.
"""
from __future__ import annotations

import os
import re


_PROVIDER_PARTS = ("GITLAB", "GITHUB", "GLAB_", "GH_", "PUBLICATION", "PUBLISH")
_SENSITIVE_PARTS = (
    "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "COOKIE", "PRIVATE_KEY",
    "API_KEY", "ASKPASS", "AUTHORIZATION",
)
# Keep this list explicit: prefix allowances used to retain messaging/session
# tokens alongside actual inference credentials.
_MODEL_AUTH_NAMES = frozenset((
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "KIMI_API_KEY",
    "OPENAI_API_KEY",
))
_ALWAYS_REMOVE = frozenset((
    "SSH_AUTH_SOCK", "GIT_ASKPASS", "SSH_ASKPASS", "GIT_CONFIG_COUNT",
    "GIT_CONFIG_PARAMETERS", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
    "GIT_PREFIX", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE",
))
_SANDBOX_ONLY_REMOVE = frozenset(("OCR_TEST_SELECTOR_CMD", "KIMCHI_BIN"))
_URI_USERINFO = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s/?#]*@")


def _valid_entry(key, value):
    return (
        isinstance(key, str) and bool(key) and "=" not in key and "\0" not in key
        and isinstance(value, str) and "\0" not in value
    )


def scrub_environment(environ=None, *, preserve_model_auth=False):
    """Return a scrubbed copy of *environ* without mutating the input.

    Malformed entries are discarded rather than passed to ``subprocess``.
    URI values containing userinfo are treated as credentials even when their
    variable name (for example ``CI_REPOSITORY_URL``) is otherwise innocent.
    """
    source = os.environ if environ is None else environ
    child = {key: value for key, value in dict(source).items()
             if _valid_entry(key, value)}
    for key in list(child):
        upper = key.upper()
        name_parts = set(upper.split("_"))
        provider = any(part in upper for part in _PROVIDER_PARTS)
        secret = any(part in upper for part in _SENSITIVE_PARTS) or "AUTH" in name_parts
        model_auth = preserve_model_auth and upper in _MODEL_AUTH_NAMES
        git_hook = upper.startswith((
            "GIT_TRACE", "GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_",
        ))
        sandbox_only = not preserve_model_auth and upper in _SANDBOX_ONLY_REMOVE
        credential_url = bool(_URI_USERINFO.search(child[key]))
        if (upper in _ALWAYS_REMOVE or git_hook or sandbox_only or provider
                or credential_url or (secret and not model_auth)):
            child.pop(key, None)
    child["RG_FINAL_CREDENTIAL_FREE"] = "1"
    return child
