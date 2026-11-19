"""Context-local process ownership for bounded repository/test execution."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import os
import subprocess


_EXECUTION_OWNER = ContextVar("native_execution_owner", default=None)


@contextmanager
def execution_ownership(owner):
    token = _EXECUTION_OWNER.set(owner)
    try:
        yield
    finally:
        _EXECUTION_OWNER.reset(token)


def run_owned_process(args, **kwargs):
    """Run subprocesses normally unless an execution worker owns this context."""
    owner = _EXECUTION_OWNER.get()
    if owner is None:
        return subprocess.run(args, **kwargs)

    options = dict(kwargs)
    timeout = options.pop("timeout", None)
    check = options.pop("check", False)
    input_value = options.pop("input", None)
    capture = options.pop("capture_output", False)
    if capture:
        options["stdout"] = subprocess.PIPE
        options["stderr"] = subprocess.PIPE
    if os.name == "posix":
        options.setdefault("start_new_session", True)
    else:
        options.setdefault("creationflags", subprocess.CREATE_NEW_PROCESS_GROUP)
    process = subprocess.Popen(args, **options)
    owner.own_process(process)
    try:
        stdout, stderr = process.communicate(input=input_value, timeout=timeout)
        if check and process.returncode:
            raise subprocess.CalledProcessError(
                process.returncode, args, output=stdout, stderr=stderr,
            )
        return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
    except subprocess.TimeoutExpired:
        owner.kill_process(process)
        try:
            process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            owner.close_process(process)
        raise
    except BaseException:
        owner.kill_process(process)
        owner.close_process(process)
        raise
    finally:
        owner.release_process(process)
