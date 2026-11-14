"""Backend profiles for the review gate: which harness CLI reviews a push.

The gate has ONE core and several PROFILES. A profile owns everything that is
specific to a harness (how its headless CLI is found and launched, its model
pins, its env scrub rules, its hooks and installer, where it keeps state, how
a block is surfaced). The core owns everything else (range resolution,
chunking, the reviewer pipeline, the verdict).

This module is the selection half of that split, kept PURE on purpose: no
import of review-gate.py, no subprocess, no network. Inputs are passed in, so
every precedence rule is testable without a harness installed.

Selection precedence, highest first:

  1. OCR_BACKEND env var            explicit; an unusable choice FAILS CLOSED
  2. "backend" in .ocr/config.json  explicit per-repo pin; same fail-closed rule
  3. harness hint (OCR_HARNESS)     set by a hook adapter that knows its caller
  4. harness env sniffing           CLAUDECODE* -> claude, KIMCHI_*/PI_* -> kimchi
  5. exactly one backend CLI found  on PATH
  6. the default                    kimchi (this build's historical behaviour)

Steps 3-6 are AUTO-DETECTION. Auto-detection only ever returns a profile that
can actually launch a review in this build (`launch_ready`); a signal for a
profile that cannot launch yet is skipped with a note rather than selected,
so a half-built profile never changes what happens to a push. Steps 1-2 are
the opposite: the user named a backend, so if it is unknown or not launchable
the gate raises BackendError and the caller blocks (a gate that quietly
reviews with a different model than the one asked for is not a gate).
"""
import json
import os
import shutil
from collections import namedtuple
from pathlib import Path

# name: the profile id used in OCR_BACKEND / config.
# cli: the executable looked up on PATH for step 5.
# env_markers: env var names / prefixes whose presence says "this process was
#   launched by that harness" (step 4). Prefix entries end with "*".
# launch_ready: False while the profile's launch + output-parsing half is
#   still being ported; see PROFILES below.
Profile = namedtuple("Profile", "name cli env_markers launch_ready")

PROFILES = {
    "kimchi": Profile(
        name="kimchi",
        cli="kimchi",
        env_markers=("KIMCHI_*", "PI_CODING_AGENT*"),
        launch_ready=True,
    ),
    # Selection knows about claude so the precedence rules are final, but the
    # claude launch path (argv, env scrub, `--output-format json` parsing,
    # CLAUDE_PLUGIN_DATA state root) is ported in a later slice. Until then an
    # EXPLICIT claude choice fails closed and auto-detect skips it.
    "claude": Profile(
        name="claude",
        cli="claude",
        env_markers=("CLAUDECODE", "CLAUDE_CODE_*"),
        launch_ready=False,
    ),
}

DEFAULT_BACKEND = "kimchi"

Selection = namedtuple("Selection", "name reason notes")


class BackendError(Exception):
    """An explicit backend choice that cannot be honoured. The gate fails
    closed on it: it never substitutes a different backend."""


def known_backends():
    return tuple(PROFILES)


def _normalise(value):
    return str(value).strip().lower()


def _check_explicit(raw, source):
    """Validate an explicitly requested backend; return its profile."""
    name = _normalise(raw)
    profile = PROFILES.get(name)
    if profile is None:
        raise BackendError(
            f"unknown backend {raw!r} from {source}; known backends: "
            + ", ".join(known_backends()))
    if not profile.launch_ready:
        raise BackendError(
            f"backend {name!r} (from {source}) is selected but its launch path is "
            f"not implemented in this build yet; refusing to review with a "
            f"different backend. Unset it to auto-detect, or choose one of: "
            + ", ".join(n for n, p in PROFILES.items() if p.launch_ready))
    return profile


def read_repo_backend(repo_root):
    """The "backend" value pinned in <repo>/.ocr/config.json.

    Returns (value_or_None, notes). A missing file or key is not an error. An
    unreadable or malformed file is reported as a note and treated as "no
    pin" for THIS lookup only: the rest of the config machinery already fails
    closed on a corrupt config where it matters, and refusing every push
    because a config key is unparseable would turn a typo into an outage.
    A pin whose VALUE is wrong is a different matter and is rejected by
    select_backend.
    """
    if not repo_root:
        return None, []
    path = Path(repo_root) / ".ocr" / "config.json"
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, []
    except OSError as exc:
        return None, [f"repo config {path} unreadable ({exc}); no backend pin read"]
    try:
        data = json.loads(raw)
    except ValueError:
        return None, [f"repo config {path} is not valid JSON; no backend pin read"]
    if not isinstance(data, dict) or "backend" not in data:
        return None, []
    value = data["backend"]
    if not isinstance(value, str) or not value.strip():
        return None, [f"repo config {path}: 'backend' must be a non-empty string; ignored"]
    return value, []


def _env_says(env, profile):
    """Does the environment look like it was launched by this harness?"""
    for marker in profile.env_markers:
        if marker.endswith("*"):
            prefix = marker[:-1]
            if any(k.upper().startswith(prefix) and env.get(k) for k in env):
                return True
        elif env.get(marker):
            return True
    return False


def select_backend(env=None, repo_root=None, which=None):
    """Pick the backend for this push. Returns a Selection(name, reason, notes).

    env: mapping to read (default os.environ). which: callable like
    shutil.which (injectable for tests). Raises BackendError only for an
    explicit choice (OCR_BACKEND or the repo pin) that cannot be honoured.
    """
    env = os.environ if env is None else env
    which = shutil.which if which is None else which
    notes = []

    # 1. env override
    override = env.get("OCR_BACKEND", "").strip()
    if override:
        profile = _check_explicit(override, "OCR_BACKEND")
        return Selection(profile.name, "OCR_BACKEND override", notes)

    # 2. repo pin
    pinned, pin_notes = read_repo_backend(repo_root)
    notes += pin_notes
    if pinned is not None:
        profile = _check_explicit(pinned, ".ocr/config.json 'backend'")
        return Selection(profile.name, "repo config pin", notes)

    # 3. harness hint from the adapter that fired the hook
    hint = env.get("OCR_HARNESS", "").strip()
    if hint:
        profile = PROFILES.get(_normalise(hint))
        if profile is None:
            notes.append(f"OCR_HARNESS={hint!r} is not a known backend; ignored")
        elif not profile.launch_ready:
            notes.append(f"OCR_HARNESS names {profile.name}, which cannot launch a review yet; skipped")
        else:
            return Selection(profile.name, "harness hint (OCR_HARNESS)", notes)

    # 4. environment sniffing
    sniffed = [p for p in PROFILES.values() if _env_says(env, p)]
    ready = [p for p in sniffed if p.launch_ready]
    for p in sniffed:
        if not p.launch_ready:
            notes.append(f"environment looks like {p.name}, which cannot launch a review yet; skipped")
    if len(ready) == 1:
        return Selection(ready[0].name, f"auto-detected from the {ready[0].name} environment", notes)
    if len(ready) > 1:
        notes.append("environment matches more than one backend; falling through")

    # 5. exactly one launchable CLI on PATH
    present = [p for p in PROFILES.values() if p.launch_ready and which(p.cli)]
    if len(present) == 1:
        return Selection(present[0].name, f"only the {present[0].name} CLI is on PATH", notes)

    # 6. default
    return Selection(DEFAULT_BACKEND, "default", notes)


def _main(argv):
    """`python ocr_profile.py [repo]` -- print the selection (used by doctor)."""
    repo = argv[1] if len(argv) > 1 else None
    try:
        sel = select_backend(repo_root=repo)
    except BackendError as exc:
        print(f"backend: ERROR {exc}")
        return 1
    print(f"backend: {sel.name} ({sel.reason})")
    for note in sel.notes:
        print(f"  note: {note}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_main(sys.argv))
