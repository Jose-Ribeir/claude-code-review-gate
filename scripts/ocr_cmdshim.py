"""Windows .cmd / .bat launcher shims and the worker prompt (stdlib only).

On Windows an npm-installed CLI is a batch shim (``claude.CMD``) in front of
the real executable. A batch file is run by cmd.exe, which ends the command
line at the first newline in any argument: a multi-line ``-p`` prompt reaches
the CLI truncated ("Unknown command: /review-gate:review" was what surfaced).

Two defences, both no-ops everywhere but Windows batch launches:

* ``unwrap`` resolves an npm-style shim to the executable it launches, so
  there is no cmd.exe layer to mangle an argument in the first place;
* ``prompt_for`` flattens newlines in a prompt when the binary is STILL a
  batch file (an unrecognised shim), so the worst case is a flat prompt rather
  than a mangled one.
"""
import os
import re
import sys

_BATCH_EXTS = (".cmd", ".bat")
# npm's shim line:  "%dp0%\node_modules\pkg\bin\tool.exe"   %*
_NPM_EXE = re.compile(r'"%dp0%[\\/]+([^"%]+?\.exe)"', re.IGNORECASE)


def is_batch(path):
    return bool(path) and str(path).lower().endswith(_BATCH_EXTS)


def unwrap(path):
    """The native executable behind an npm .cmd shim, else ``path`` unchanged.

    Only a shim whose target is a plain ``.exe`` next to it is followed; a
    node-script shim, an unreadable file or a missing target all fall back to
    the shim itself (and ``prompt_for`` then protects the prompt).
    """
    if sys.platform != "win32" or not is_batch(path):
        return path
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read(8192)
    except OSError:
        return path
    match = _NPM_EXE.search(text)
    if not match:
        return path
    target = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(path)),
                                           match.group(1)))
    return target if os.path.isfile(target) else path


def prompt_for(binary, prompt):
    """``prompt`` safe to pass as one argument to ``binary``.

    A batch launcher cannot carry a newline in an argument (see module doc), so
    newline runs become single spaces there; any other binary gets the prompt
    untouched.
    """
    if sys.platform == "win32" and is_batch(binary) and prompt and "\n" in prompt:
        return re.sub(r"\s*\n\s*", " ", prompt)
    return prompt
