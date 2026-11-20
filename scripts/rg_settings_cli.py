#!/usr/bin/env python3
"""The command line behind /review-gate:settings.

    rg_settings_cli.py [show] [--repo PATH] [--json]
        every agent x backend model and thinking level and every behavior knob,
        each with where its value came from (env / project / user / default)
    rg_settings_cli.py set <key> <value> [--project | --user] [--repo PATH]
        validate then write one value (default scope: user)
    rg_settings_cli.py validate [--repo PATH]
        check the project and user settings files

Exit 0 on success, 1 on any invalid file or value (nothing is written for an
invalid value), 2 for a usage error. All the rules live in rg_settings.
"""
import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rg_settings  # noqa: E402


def _repo_root(explicit):
    """The repository whose project file applies: --repo, else the git top
    level of the current directory, else None (user file and defaults only)."""
    if explicit:
        return os.path.abspath(explicit)
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    top = (out.stdout or "").strip()
    return top if out.returncode == 0 and top else None


def _fail(message):
    sys.stderr.write("error: %s\n" % message)
    return 1


def _display(value):
    if value is None:
        return "(not passed)"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _describe_files(root):
    user = rg_settings.user_settings_path()
    lines = []
    for label, path in (("project", rg_settings.project_settings_path(root) if root else None),
                        ("user", user)):
        if path is None:
            lines.append("%s file: none (no %s)" % (label, "repository" if label == "project"
                                                     else "home directory"))
        else:
            lines.append("%s file: %s (%s)" % (label, path,
                                               "present" if os.path.isfile(str(path)) else "absent"))
    return lines


def cmd_show(args):
    root = _repo_root(args.repo)
    try:
        settings = rg_settings.load(root)
    except rg_settings.SettingsError as exc:
        return _fail(exc)
    rows = [(key, got) for key, got in settings.entries()]
    if args.json:
        print(json.dumps([{"key": k, "value": got.value, "source": got.source}
                          for k, got in rows], indent=2))
        return 0
    print("review-gate settings (precedence: env > project file > user file > default)")
    for line in _describe_files(root):
        print("  " + line)
    width = max(len(k) for k, _ in rows)
    for key, got in rows:
        print("%-*s = %-34s [%s]" % (width, key, _display(got.value), got.source))
    return 0


def cmd_validate(args):
    root = _repo_root(args.repo)
    bad = 0
    for label, path in (("project", rg_settings.project_settings_path(root) if root else None),
                        ("user", rg_settings.user_settings_path())):
        if path is None:
            continue
        if not os.path.isfile(str(path)):
            print("%s file %s: absent (defaults apply)" % (label, path))
            continue
        try:
            rg_settings._read_file(path, label)
        except rg_settings.SettingsError as exc:
            bad += 1
            sys.stderr.write("INVALID: %s\n" % exc)
        else:
            print("%s file %s: valid" % (label, path))
    return 1 if bad else 0


def cmd_set(args):
    root = _repo_root(args.repo)
    scope = "project" if args.project else "user"
    try:
        # _split_key via parse_cli_value rejects an unknown key first.
        value = rg_settings.parse_cli_value(args.key, args.value)
        rg_settings.validate_value(args.key, value)
        parts = args.key.split(".")
        if parts[0] == "agents" and parts[3] == "model" and parts[2] == "kimchi":
            provider = rg_settings.model_provider(value)
            if provider in rg_settings.BLOCKED_PROVIDERS:
                raise rg_settings.SettingsError(
                    "%s: %r is a %s model and the kimchi backend refuses %s models "
                    "(ADR 0005); pick an open model or set the claude backend's "
                    "entry instead" % (args.key, value, provider,
                                       "/".join(rg_settings.BLOCKED_PROVIDERS)))
        path = rg_settings.set_value(args.key, value, scope=scope, repo_root=root)
    except rg_settings.SettingsError as exc:
        return _fail(exc)
    print("wrote %s = %s to the %s file %s" % (args.key, _display(value), scope, path))
    if parts[0] == "agents" and parts[3] == "model":
        dep = rg_settings.deprecation(value)
        if dep:
            print("warning: %s is deprecated (retires %s); use %s" % (value, dep[1], dep[0]))
    try:
        got = rg_settings.load(root).get(args.key)
    except rg_settings.SettingsError as exc:
        return _fail(exc)
    if got.value != value or not got.source.startswith(scope + ":"):
        print("note: the effective value is still %s [%s]; a higher-precedence source wins"
              % (_display(got.value), got.source))
    else:
        print("effective: %s [%s]" % (_display(got.value), got.source))
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0].startswith("-"):
        argv.insert(0, "show")
    parser = argparse.ArgumentParser(
        prog="rg_settings_cli.py",
        description="Show, set and validate the review-gate agent settings.")
    sub = parser.add_subparsers(dest="cmd")
    p_show = sub.add_parser("show", help="print every effective value and its source")
    p_show.add_argument("--repo", default=None, metavar="PATH")
    p_show.add_argument("--json", action="store_true")
    p_set = sub.add_parser("set", help="set one value")
    p_set.add_argument("key", help="agents.<agent>.<backend>.model|thinking or behavior.<group>.<knob>")
    p_set.add_argument("value")
    scope = p_set.add_mutually_exclusive_group()
    scope.add_argument("--project", action="store_true", help="write <repo>/.ocr/settings.json")
    scope.add_argument("--user", action="store_true", help="write the user file (default)")
    p_set.add_argument("--repo", default=None, metavar="PATH")
    p_val = sub.add_parser("validate", help="validate the settings files")
    p_val.add_argument("--repo", default=None, metavar="PATH")
    args = parser.parse_args(argv)
    return {"show": cmd_show, "set": cmd_set, "validate": cmd_validate}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
