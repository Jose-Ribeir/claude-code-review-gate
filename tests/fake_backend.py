"""A fake headless backend CLI: stands in for `claude -p ...` and `kimchi -p ...`.

The end-to-end backend tests put an executable named `claude` (and/or
`kimchi`) on PATH and drive the real gate through it, so the code under test
is the part a stub reviewer (OCR_STUB_WORKER / OCR_REVIEWER_CMD) bypasses: CLI
discovery, backend selection, argv, env scrub, the canary, spawn and the
per-backend output parsing.

The review content itself is NOT re-invented here. This script translates the
gate's `-p <prompt>` call back into the argv tests/stub_reviewer.py already
understands (range, --paths-file, --resolve, --docs-summary), runs it, and
re-wraps its reply the way the named CLI would print it. Every STUB_* knob of
stub_reviewer.py therefore works unchanged.

Windows caveat: the fake is a `.cmd` wrapper, and cmd.exe ends a batch command
at the first newline inside an argument. The gate's incremental ("partial
re-review") prompt carries multi-line text, so on Windows a fake only sees the
arguments of a repo's FIRST review; tests that assert on launch argv therefore
push once per repo. (A real npm-installed `claude.cmd` has the same limit.)

Invoked as `fake_backend.py <flavor> <cli argv...>`, flavor `claude` or
`kimchi`; the wrappers made by install() pass the flavor.

  FAKE_TRACE      file: one JSON line per invocation (flavor, argv, cwd, the
                  env names a launch test cares about). Absent -> no trace.
  FAKE_SCENARIO   "" (default) delegate to the stub reviewer;
                  auth     print the CLI's logged-out error (FAKE_AUTH_RC, default 1)
                  notloggedin  print the CLI's "Not logged in" error (rc 1)
                  garbage  a well-formed stream whose text holds no verdict
                  crash    exit 3 with a stderr message and no stdout
                  rawtext  plain text with no JSON stream at all, exit 0
                  native-timeout  stream credential-like tails, spawn a pipe-holding
                                  child, and sleep until the native deadline
  FAKE_MODELS     comma-separated ids `kimchi --list-models` prints (default: every
                  kimchi default pin of the settings layer)
  FAKE_REJECT_SUFFIX  comma-separated thinking levels (`high,max`): a kimchi
                  `--model <id>:<level>` with one of them exits 2 with kimchi's
                  "invalid model" error instead of answering
  FAKE_VERSION    the text `--version` prints (default: the flavor's fixed string)
  FAKE_CANARY_LEAK  1: the deny-extension canary's write SUCCEEDS (a sandbox that
                  did not engage), so the gate must deny
  FAKE_MANIFEST_COPY  file: a copy of the review manifest (the --paths-file the
                  prompt names) is written there, for tests that read the manifest
  FAKE_ENVELOPE   "json": claude flavor answers with the single
                  `--output-format json` result object instead of a stream.
"""
import json
import os
import re
import stat
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_STUB = os.path.join(_HERE, "stub_reviewer.py")

_ENV_NAMES = ("OCR_IN_REVIEW", "OCR_KIMI_ARGS", "OCR_CLAUDE_ARGS", "OCR_KIMI_EXTRA_ARGS", "CLAUDE_CODE_DISABLE_CLAUDE_MDS",
              "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_ENTRYPOINT",
              "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDECODE",
              "KIMCHI_SESSION_ID", "KIMCHI_PERMISSIONS", "PI_ENTRYPOINT")

_AUTH_TEXT = {"claude": "Invalid API key · Please run /login",
              "kimchi": "OAuth session expired. Please run /login"}


def install(shim_dir, flavor, python=None):
    """Create an executable named `flavor` in shim_dir that runs this script
    with that flavor. Returns the path. A `.cmd` on Windows (shutil.which
    resolves it through PATHEXT, and CreateProcess runs it), a `#!/bin/sh`
    script elsewhere."""
    python = python or sys.executable
    os.makedirs(str(shim_dir), exist_ok=True)
    me = os.path.abspath(__file__)
    if sys.platform == "win32":
        path = os.path.join(str(shim_dir), flavor + ".cmd")
        with open(path, "w", encoding="ascii", newline="\r\n") as fh:
            fh.write(f'@echo off\n"{python}" "{me}" {flavor} %*\n')
    else:
        path = os.path.join(str(shim_dir), flavor)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f'#!/bin/sh\nexec "{python}" "{me}" {flavor} "$@"\n')
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def read_trace(path):
    """The invocations recorded in FAKE_TRACE, as dicts (empty if none)."""
    try:
        with open(str(path), encoding="utf-8") as fh:
            return [json.loads(ln) for ln in fh if ln.strip()]
    except OSError:
        return []


def _stub_argv(prompt):
    """The tests/stub_reviewer.py argv equivalent to the gate's prompt."""
    if prompt.startswith("Summarize this repository's documentation"):
        return ["--docs-summary", "tip"]
    out = []
    rs = re.search(r'--resolve "([^"]+)"', prompt)
    pf = re.search(r'--paths-file "([^"]+)"', prompt)
    rg = re.search(r"--range (\S+)", prompt)
    if rs:
        out += ["--resolve", rs.group(1)]
    elif pf:
        out += ["--paths-file", pf.group(1)]
    if rg:
        out.append(rg.group(1))
    return out


def _wrap(flavor, payload):
    """`payload` (the reviewer's reply text) as the CLI's stdout."""
    if flavor == "claude":
        result = {"type": "result", "subtype": "success", "is_error": False,
                  "result": payload, "num_turns": 2, "duration_ms": 10,
                  "duration_api_ms": 5, "total_cost_usd": 0.01, "session_id": "fake"}
        if os.environ.get("FAKE_ENVELOPE") == "json":
            return json.dumps(result) + "\n"
        events = [
            {"type": "system", "subtype": "init", "session_id": "fake"},
            {"type": "assistant", "session_id": "fake",
             "message": {"role": "assistant",
                         "content": [{"type": "text", "text": payload}]}},
            result,
        ]
    else:
        message = {"role": "assistant", "content": [{"type": "text", "text": payload}]}
        events = [{"type": "turn_start"},
                  {"type": "message_end", "message": message},
                  {"type": "turn_end", "message": message}]
    return "".join(json.dumps(e) + "\n" for e in events)


_DEFAULT_MODELS = ("kimi-k3.1-pro,deepseek-v4.1-flash-1017,glm-5.3,glm-5.3-flash,"
                   "deepseek-v4-flash-0731,minimax-m3,gemma-4-31b")


def main(argv):
    flavor, cli = argv[1], argv[2:]
    if flavor == "kimchi" and cli == ["--list-models"]:
        ids = os.environ.get("FAKE_MODELS", _DEFAULT_MODELS)
        sys.stdout.write("Available models:\n" + "".join(
            f"  kimchi-dev/{m.strip()}\n" for m in ids.split(",") if m.strip()))
        return 0
    if cli == ["--version"]:
        if os.environ.get("FAKE_VERSION"):
            sys.stdout.write(os.environ["FAKE_VERSION"] + "\n")
            return 0
        sys.stdout.write("2.1.0 (Claude Code)\n" if flavor == "claude" else "kimchi 1.34.3\n")
        return 0
    if cli == ["--help"]:
        sys.stdout.write(
            "-p --mode rpc --no-session --no-approve --approve --no-auto-update "
            "--model --mcp-config --allow-tool --deny-tool -e --plan\n"
        )
        return 0
    if flavor == "kimchi" and "--model" in cli and cli.index("--model") + 1 < len(cli):
        model = cli[cli.index("--model") + 1]
        level = model.split(":", 1)[1] if ":" in model else ""
        if level and level in [x.strip() for x in os.environ.get("FAKE_REJECT_SUFFIX", "").split(",") if x.strip()]:
            sys.stderr.write(f"error: invalid model '{model}': unknown thinking level '{level}'\n")
            return 2
    rpc_id = None
    rpc_mode = "--mode" in cli and cli[cli.index("--mode") + 1] == "rpc"
    if rpc_mode:
        command = json.loads(sys.stdin.buffer.readline())
        rpc_id = command["id"]
        prompt = command["message"]
    else:
        prompt = cli[cli.index("-p") + 1] if "-p" in cli and cli.index("-p") + 1 < len(cli) else ""
    trace = os.environ.get("FAKE_TRACE")
    if trace:
        with open(trace, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "flavor": flavor, "argv": cli, "prompt": prompt, "cwd": os.getcwd(),
                "env": {k: os.environ[k] for k in _ENV_NAMES if k in os.environ},
            }) + "\n")
    copy_to = os.environ.get("FAKE_MANIFEST_COPY")
    mf = re.search(r'--paths-file "([^"]+)"', prompt)
    if copy_to and mf and os.path.isfile(mf.group(1)):
        with open(mf.group(1), "rb") as src, open(copy_to, "wb") as dst:
            dst.write(src.read())
    if prompt.startswith("Sandbox self-check"):
        if os.environ.get("FAKE_CANARY_LEAK") == "1":
            m = re.search(r"create the file (\S+) containing", prompt)
            if m:
                with open(m.group(1), "w", encoding="utf-8") as fh:
                    fh.write("ok\n")
        return 0          # the deny-extension canary: a sandboxed worker writes nothing
    scenario = os.environ.get("FAKE_SCENARIO", "")
    if prompt.startswith("Reply with the single word"):
        sys.stdout.write(_wrap(flavor, "OK"))     # kimchi-verify's thinking-suffix probe
        return 0
    if scenario == "auth":
        text = _AUTH_TEXT[flavor]
        if flavor == "claude":
            sys.stdout.write(json.dumps({"type": "result", "subtype": "success",
                                         "is_error": True, "result": text}) + "\n")
        else:
            sys.stdout.write(text + "\n")
        return int(os.environ.get("FAKE_AUTH_RC", "1"))
    if scenario == "notloggedin":
        text = "Not logged in · Please run /login"
        if flavor == "claude":
            sys.stdout.write(json.dumps({"type": "result", "subtype": "success",
                                         "is_error": True, "result": text}) + "\n")
        else:
            sys.stdout.write(text + "\n")
        return 1
    if scenario == "garbage":
        sys.stdout.write(_wrap(flavor, "I looked at the change and it seems fine."))
        return 0
    if scenario == "crash":
        sys.stderr.write("fatal: the backend crashed\n")
        return 3
    if scenario == "rawtext":
        sys.stdout.write("this is not a JSON stream\n")
        return 0
    if scenario == "native-timeout":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        with open(os.environ["FAKE_TIMEOUT_CHILD"], "w", encoding="utf-8") as fh:
            fh.write(str(child.pid))
        if rpc_mode:
            sys.stdout.write(json.dumps({
                "id": rpc_id, "type": "response", "command": "prompt",
                "success": True, "data": {"disposition": "started"},
            }) + "\n" + json.dumps({
                "type": "message_end", "message": {
                    "role": "assistant", "stopReason": "stop",
                    "content": [{"type": "text", "text": "api_key=stdout-secret"}],
                },
            }) + "\n")
            sys.stdout.flush()
            sys.stderr.write("model tool failed: bearer stderr-secret\n")
            sys.stderr.flush()
            while True:
                line = sys.stdin.buffer.readline()
                if not line:
                    return 0
                abort = json.loads(line)
                if abort["type"] == "abort":
                    sys.stdout.write(json.dumps({
                        "id": abort["id"], "type": "response", "command": "abort",
                        "success": True,
                    }) + "\n" + json.dumps({"type": "agent_settled", "aborted": True}) + "\n")
                    sys.stdout.flush()
                    return 0
        sys.stdout.write(json.dumps({
            "type": "model_tool", "status": "api_key=stdout-secret",
        }) + "\n")
        sys.stdout.flush()
        sys.stderr.write("model tool failed: bearer stderr-secret\n")
        sys.stderr.flush()
        time.sleep(120)
        return 0
    proc = subprocess.run([sys.executable, _STUB] + _stub_argv(prompt),
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", stdin=subprocess.DEVNULL)
    if proc.returncode != 0:
        sys.stdout.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        return proc.returncode
    output = proc.stdout
    pf = re.search(r'--paths-file "([^"]+)"', prompt)
    if pf:
        with open(pf.group(1), encoding="utf-8") as fh:
            manifest = json.load(fh)
        task = manifest.get("native_deep_task")
        if task:
            payload = json.loads(output)
            input_sha = re.search(r'"input_sha256": "([0-9a-f]{64})"', prompt)
            payload["native_deep_evidence"] = {
                "task_id": task["task_id"],
                "input_sha256": input_sha.group(1),
                "manifest_sha256": manifest["manifest_sha256"],
                "reviewed_paths": manifest["paths"],
                "completed_duties": task["duties"],
            }
            output = json.dumps(payload)
    if rpc_mode:
        sys.stdout.write(json.dumps({
            "id": rpc_id, "type": "response", "command": "prompt",
            "success": True, "data": {"disposition": "started"},
        }) + "\n" + json.dumps({
            "type": "message_end", "message": {
                "role": "assistant", "stopReason": "stop",
                "content": [{"type": "text", "text": output}],
            },
        }) + "\n" + json.dumps({
            "type": "agent_end", "messages": [], "willRetry": False,
        }) + "\n" + json.dumps({"type": "agent_settled", "aborted": False}) + "\n")
        sys.stdout.flush()
        while sys.stdin.buffer.readline():
            pass
    else:
        sys.stdout.write(_wrap(flavor, output))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
