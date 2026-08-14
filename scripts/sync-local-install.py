#!/usr/bin/env python3
#
# Re-materializes the LOCAL Claude Code plugin install from this working tree.
#
# Why this exists: when the plugin is installed from a `directory` marketplace
# (i.e. you develop it in-place), Claude Code does NOT run your working tree --
# it runs a SNAPSHOT copied into
#   ~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/
# and `${CLAUDE_PLUGIN_ROOT}` in hooks/hooks.json resolves to that snapshot. The
# install record is version-pinned, so committing to the working tree changes
# nothing the gate actually runs until the version in .claude-plugin/plugin.json
# changes AND the snapshot is refreshed. The failure is silent and confusing:
# the gate keeps enforcing old code, including bugs you already fixed.
#
# Run this after changing plugin code (and bump plugin.json when releasing):
#   python3 scripts/sync-local-install.py            # sync
#   python3 scripts/sync-local-install.py --check    # report drift, change nothing
#   python3 scripts/sync-local-install.py --prune    # also drop superseded snapshots
#
# The global git hook installed by install-git-hook.sh used to sidestep all of
# this by baking in an absolute path to this directory, so it always ran live
# code. As of 0.3.0 it resolves the reviewer at runtime and prefers the pointer
# that review-gate.py writes under ${CLAUDE_PLUGIN_DATA} -- which points at
# whichever install last ran the plugin hook, i.e. the SNAPSHOT. So the two
# wirings now agree on the version in force, and syncing matters for both.
import argparse
import filecmp
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

MARKETPLACE = "claude-code-review-gate"
PLUGIN = "review-gate"
PLUGIN_KEY = f"{PLUGIN}@{MARKETPLACE}"

# What a consumer of the plugin actually needs. Tests, CI config and local tool
# state are deliberately excluded -- the snapshot is a runtime artifact, not a
# mirror of the repo.
PAYLOAD = [
    ".claude-plugin",
    "agents",
    "commands",
    # bin/ holds one compatibility shim, kept so pre-0.3.0 git-hook installs
    # (which baked an absolute path to it) keep resolving. Removal target:
    # 0.5.0. Everything else moved to scripts/ -- a plugin's bin/ is added to
    # the Bash tool's PATH, so anything left here becomes a bare command in
    # every user's shell.
    "bin",
    "scripts",
    "examples",
    "hooks",
    "schemas",
    "skills",
    "CHANGELOG.md",
    "LICENSE",
    "NOTICE",
    "README.md",
]

# Never copy generated/local junk even when it sits inside a payload directory.
EXCLUDE = shutil.ignore_patterns(
    "__pycache__", "*.py[cod]", ".pytest_cache", ".ruff_cache", ".serena",
    ".DS_Store", "Thumbs.db", "settings.local.json",
)


def _repo_root():
    return Path(__file__).resolve().parent.parent


def _plugins_dir():
    # CLAUDE_CONFIG_DIR is the documented override for ~/.claude.
    base = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    return (Path(base) if base else Path.home() / ".claude") / "plugins"


def _version(repo):
    manifest = json.loads(
        (repo / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    version = str(manifest.get("version", "")).strip()
    if not version:
        sys.exit("plugin.json has no version -- refusing to guess.")
    return version


def _head_sha(repo):
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(repo),
            capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip() if out.returncode == 0 else None
    except Exception:
        return None


def _dirty(repo):
    """True if the working tree differs from HEAD (so the snapshot won't match
    the recorded sha). Not an error -- that is the normal state while iterating
    -- but worth saying out loud so the recorded sha is not over-trusted."""
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain"], cwd=str(repo),
            capture_output=True, text=True, timeout=10,
        )
        return bool(out.stdout.strip())
    except Exception:
        return False


def _differs(src, dst):
    """Recursive content comparison. filecmp.dircmp with shallow=False is not
    used directly because it only compares the files common to both sides at
    each level; we need added/removed entries to count as drift too."""
    if not dst.exists():
        return True
    cmp = filecmp.dircmp(str(src), str(dst), ignore=[
        "__pycache__", ".pytest_cache", ".ruff_cache", ".serena",
    ])
    if cmp.left_only or cmp.right_only or cmp.funny_files:
        return True
    _, mismatch, errors = filecmp.cmpfiles(
        str(src), str(dst), cmp.common_files, shallow=False
    )
    if mismatch or errors:
        return True
    return any(_differs(src / d, dst / d) for d in cmp.common_dirs)


def _copy_payload(repo, dest):
    """Write the payload into a sibling temp dir, then swap it into place, so an
    interrupted copy cannot leave a half-populated snapshot that the gate would
    happily run."""
    staging = dest.parent / f".{dest.name}.staging"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    for name in PAYLOAD:
        src = repo / name
        if not src.exists():
            continue
        if src.is_dir():
            shutil.copytree(src, staging / name, ignore=EXCLUDE)
        else:
            shutil.copy2(src, staging / name)

    retired = dest.parent / f".{dest.name}.retired"
    shutil.rmtree(retired, ignore_errors=True)
    if dest.exists():
        os.replace(dest, retired)
    try:
        os.replace(staging, dest)
    except OSError:
        if retired.exists() and not dest.exists():
            os.replace(retired, dest)  # put the old snapshot back
        raise
    shutil.rmtree(retired, ignore_errors=True)


def _update_record(plugins_dir, install_path, version, sha):
    """Point the install record at the snapshot we just wrote.

    Rewritten in place rather than regenerated: this file also records every
    OTHER installed plugin, and clobbering it would uninstall them.
    """
    reg = plugins_dir / "installed_plugins.json"
    if not reg.exists():
        return "no installed_plugins.json -- plugin not installed for this user?"
    data = json.loads(reg.read_text(encoding="utf-8"))
    entries = data.get("plugins", {}).get(PLUGIN_KEY)
    if not entries:
        return f"no install record for {PLUGIN_KEY} -- install the plugin first."
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    for entry in entries:
        entry["installPath"] = str(install_path)
        entry["version"] = version
        entry["lastUpdated"] = stamp
        if sha:
            entry["gitCommitSha"] = sha
    reg.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="report drift and exit 1 if out of date; change nothing")
    ap.add_argument("--prune", action="store_true",
                    help="remove cached snapshots of other versions after syncing")
    args = ap.parse_args(argv)

    repo = _repo_root()
    version = _version(repo)
    plugins_dir = _plugins_dir()
    cache = plugins_dir / "cache" / MARKETPLACE / PLUGIN
    dest = cache / version

    if args.check:
        stale = [
            name for name in PAYLOAD
            if (repo / name).exists() and (
                _differs(repo / name, dest / name) if (repo / name).is_dir()
                else not (dest / name).exists()
                or not filecmp.cmp(repo / name, dest / name, shallow=False)
            )
        ]
        if not dest.exists():
            print(f"OUT OF DATE: no snapshot at {dest}")
            return 1
        if stale:
            print("OUT OF DATE: local install differs from working tree:")
            for name in stale:
                print(f"  {name}")
            return 1
        print(f"up to date: {dest}")
        return 0

    _copy_payload(repo, dest)
    sha = _head_sha(repo)
    warning = _update_record(plugins_dir, dest, version, sha)

    print(f"Synced local plugin install:\n  from : {repo}\n  to   : {dest}")
    if sha:
        print(f"  sha  : {sha}{' (working tree is dirty)' if _dirty(repo) else ''}")
    if warning:
        print(f"WARNING: {warning}")

    if args.prune:
        for old in sorted(cache.iterdir()) if cache.exists() else []:
            if old.is_dir() and old.name != version:
                shutil.rmtree(old, ignore_errors=True)
                print(f"  pruned superseded snapshot: {old.name}")

    print("\nRestart Claude Code for the refreshed plugin to be picked up.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
