"""The seeded scenarios of Part S's benchmark (docs/plans/resume-truncated-chunks.md).

Each scenario is a base and a tip of a small synthetic repository with ONE seeded cross-function
bug, plus what the reviewer must be HANDED for it to have a chance of seeing the bug: the changed
unit, the caller as a checked dependency (a task and its context), the cross-file call site, the
index that shows a twin unit exists. tests/test_segment_scenarios.py runs every scenario through
the real gate and the stub reviewer and asserts exactly that -- the mechanics, deterministic and
free.

Whether a MODEL then finds the bug is the other half of the benchmark, and it needs the model:
see docs/benchmark-part-s.md for the manual procedure (this module's `seed_repo` builds the same
repositories for it).

A scenario is a dict:
  name, base {path: text}, tip {path: text}   -- the push is base -> tip, `big.*` is the file in units
  units       unit names that must be among the unit_diff items
  tasks       the exact set of (callee unit, caller unit) caller checks
  impact      (symbol, call-site file) pairs that must be among the impact bundle's sites
  caller_text substrings that must be in the caller's context file
  diff_text   substrings that must be in the changed unit's diff
  index       substrings that must be in the file_context item (the unit index)
  same_chunk  (unit, unit) pairs that must be reviewed in one chunk
  limit       documented: where Part S gives no more than the old large-file review
"""

FILLER = "".join(f"def filler{i}(x):\n    return x + {i}\n\n\n" for i in range(3))


def py(*parts):
    return "import os\n\n\n" + FILLER + "".join(parts)


def _use(name, call, imp=None):
    return f"from big import {imp or name}\n\n\ndef consume_{name}(x):\n    return {call}\n"


S = []


def scenario(**kw):
    kw.setdefault("units", set())
    kw.setdefault("tasks", set())
    kw.setdefault("impact", set())
    S.append(kw)
    return kw


# 1. A body-only change now returns None; a same-file caller dereferences the result.
scenario(
    name="1-same-file-none",
    base={"big.py": py("def compute(x):\n    return x + 1\n\n\ndef consume(x):\n    result = compute(x)\n    return result.value\n")},
    tip={"big.py": py("def compute(x):\n    return None\n\n\ndef consume(x):\n    result = compute(x)\n    return result.value\n")},
    units={"compute"}, tasks={("compute", "consume")}, caller_text=["result.value"],
    diff_text=["-    return x + 1", "+    return None"])

# 2. The same, with the caller in another file.
scenario(
    name="2-cross-file-none",
    base={"big.py": py("def compute(x):\n    return x + 1\n"), "use.py": _use("compute", "compute(x).value")},
    tip={"big.py": py("def compute(x):\n    return None\n"), "use.py": _use("compute", "compute(x).value")},
    units={"compute"}, impact={("compute", "use.py")}, site_text=["def consume_compute", ".value"])

# 3. A function now raises TimeoutError; its caller catches only ValueError.
scenario(
    name="3-new-exception",
    base={"big.py": py("def fetch(url):\n    return url.upper()\n\n\n"
                       "def handler(url):\n    try:\n        return fetch(url)\n    except ValueError:\n        return None\n")},
    tip={"big.py": py("def fetch(url):\n    raise TimeoutError(url)\n\n\n"
                      "def handler(url):\n    try:\n        return fetch(url)\n    except ValueError:\n        return None\n")},
    units={"fetch"}, tasks={("fetch", "handler")}, caller_text=["except ValueError"],
    diff_text=["+    raise TimeoutError(url)"])

# 4. Parameters reordered, with three callers across two files.
scenario(
    name="4-reordered-params",
    base={"big.py": py("def send(a, b):\n    return a - b\n\n\ndef first():\n    return send(1, 2)\n\n\n"
                       "def second():\n    return send(3, 4)\n"),
          "use.py": _use("send", "send(5, 6)")},
    tip={"big.py": py("def send(b, a):\n    return a - b\n\n\ndef first():\n    return send(1, 2)\n\n\n"
                      "def second():\n    return send(3, 4)\n"),
         "use.py": _use("send", "send(5, 6)")},
    units={"send"}, tasks={("send", "first"), ("send", "second")}, impact={("send", "use.py")},
    diff_text=["-def send(a, b):", "+def send(b, a):"])

# 5. A default changed from retries=3 to 0.
scenario(
    name="5-changed-default",
    base={"big.py": py("def connect(host, retries=3):\n    return host * retries\n\n\n"
                       "def run_job():\n    return connect('h')\n")},
    tip={"big.py": py("def connect(host, retries=0):\n    return host * retries\n\n\n"
                      "def run_job():\n    return connect('h')\n")},
    units={"connect"}, tasks={("connect", "run_job")}, diff_text=["+def connect(host, retries=0):"])

# 6. `def` became `async def`, and a caller does not await.
scenario(
    name="6-became-async",
    base={"big.py": py("def load(path):\n    return path\n\n\ndef main_flow(path):\n    data = load(path)\n    return data\n")},
    tip={"big.py": py("async def load(path):\n    return path\n\n\ndef main_flow(path):\n    data = load(path)\n    return data\n")},
    units={"load"}, tasks={("load", "main_flow")}, caller_text=["data = load(path)"],
    diff_text=["+async def load(path):"])

# 7. A module constant changed, breaking an assumption elsewhere. (A documented limit: there is no
#    caller edge to a constant, so the reviewer gets the changed region and the unit index only.)
scenario(
    name="7-module-constant",
    base={"big.py": py("MAX_RETRIES = 3\n\n\ndef retry_loop(f):\n    for _ in range(MAX_RETRIES):\n        f()\n")},
    tip={"big.py": py("MAX_RETRIES = 0\n\n\ndef retry_loop(f):\n    for _ in range(MAX_RETRIES):\n        f()\n")},
    units={"MAX_RETRIES = 0"}, diff_text=["-MAX_RETRIES = 3", "+MAX_RETRIES = 0"],
    index=["def retry_loop"], limit="constants: no caller edge")

# 8. A rename, with a same-file caller still using the old name and a cross-file one.
scenario(
    name="8-rename",
    base={"big.py": py("def old_name(x):\n    return x * 2\n\n\ndef legacy(x):\n    return old_name(x) + 1\n"),
          "use.py": _use("old_name", "old_name(3)")},
    tip={"big.py": py("def new_name(x):\n    return x * 2\n\n\ndef legacy(x):\n    return old_name(x) + 1\n"),
         "use.py": _use("old_name", "old_name(3)")},
    units={"new_name"}, tasks={("old_name", "legacy")}, impact={("old_name", "use.py")},
    caller_text=["old_name(x) + 1"], diff_text=["-def old_name(x):", "+def new_name(x):"])

# 9. A sibling fix: one twin fixed, the other not. (Limit: the twin is not under review; the unit
#    index tells the reviewer it exists.)
scenario(
    name="9-sibling-fix",
    base={"big.py": py("def parse_a(s):\n    return int(s)\n\n\ndef parse_b(s):\n    return int(s)\n")},
    tip={"big.py": py("def parse_a(s):\n    return int(s or 0)\n\n\ndef parse_b(s):\n    return int(s)\n")},
    units={"parse_a"}, index=["def parse_b(s):"], limit="the unchanged twin is not reviewed")

# 10. Lock pairing: one side of the pair changes. (Same limit: the index shows the other side.)
scenario(
    name="10-lock-pairing",
    base={"big.py": py("def acquire(lock):\n    lock.append(1)\n\n\ndef release(lock):\n    lock.pop()\n")},
    tip={"big.py": py("def acquire(lock):\n    lock.append(1)\n    lock.append(2)\n\n\ndef release(lock):\n    lock.pop()\n")},
    units={"acquire"}, index=["def release(lock):"], limit="the unchanged other half is not reviewed")

# 11. Two changed units must agree (a serializer and a deserializer): reviewed in one chunk, even
#     with unrelated changed units between them and a chunk budget that holds only a few units.
scenario(
    name="11-serializer-pair",
    base={"big.py": "import os\n\n\n" + "def serialize(o):\n    return str(o)\n\n\n"
          + "".join(f"def mid{i}(x):\n    return x + {i}\n\n\n" for i in range(5))
          + "def deserialize(s):\n    return serialize(int(s))\n"},
    tip={"big.py": "import os\n\n\n" + "def serialize(o):\n    return repr(o)\n\n\n"
         + "".join(f"def mid{i}(x):\n    return x + {i} + 1\n\n\n" for i in range(5))
         + "def deserialize(s):\n    return serialize(eval(s))\n"},
    units={"serialize", "deserialize"}, tasks={("serialize", "deserialize")}, same_chunk=[("serialize", "deserialize")], chunk_budget=40)

# 12. A removed dataclass field still read elsewhere. (Limit: the read is `cfg.timeout`, not the
#     name `Config`; the importing file is found, the attribute read is not.)
scenario(
    name="12-removed-field",
    base={"big.py": py("class Config:\n    host = 'h'\n    timeout = 3\n"),
          "use.py": "from big import Config\n\n\ndef wait(cfg):\n    return cfg.timeout\n"},
    tip={"big.py": py("class Config:\n    host = 'h'\n"),
         "use.py": "from big import Config\n\n\ndef wait(cfg):\n    return cfg.timeout\n"},
    units={"Config"}, impact={("Config", "use.py")}, diff_text=["-    timeout = 3"],
    limit="attribute reads are not name references")

# 15a. The TypeScript variant of scenario 1 (the name-reference regex finds the caller).
scenario(
    name="15a-ts-same-file-none",
    base={"big.ts": "export const version = 1;\n\n"
          "export function computeTotal(items: number[]): number {\n  return items.length;\n}\n\n"
          "export function renderTotal(items: number[]): string {\n  const total = computeTotal(items);\n"
          "  return total.toFixed(2);\n}\n"},
    tip={"big.ts": "export const version = 1;\n\n"
         "export function computeTotal(items: number[]): number | null {\n  return null;\n}\n\n"
         "export function renderTotal(items: number[]): string {\n  const total = computeTotal(items);\n"
         "  return total.toFixed(2);\n}\n"},
    units={"computeTotal"}, tasks={("computeTotal", "renderTotal")}, caller_text=["total.toFixed(2)"])

# 15b. The TypeScript variant of scenario 4: five same-file callers, the regex path keeps four.
scenario(
    name="15b-ts-reordered-params",
    base={"big.ts": "export function sendMessage(a: string, b: number): string {\n  return a + b;\n}\n\n"
          + "".join(f"export function caller{i}(): string {{\n  return sendMessage('x', {i});\n}}\n\n" for i in range(5))},
    tip={"big.ts": "export function sendMessage(b: number, a: string): string {\n  return a + b;\n}\n\n"
         + "".join(f"export function caller{i}(): string {{\n  return sendMessage('x', {i});\n}}\n\n" for i in range(5))},
    units={"sendMessage"},
    tasks={("sendMessage", f"caller{i}") for i in range(4)}, tasks_cap=4)


def seed_repo(work, scenario_dict, commit):
    """Write a scenario's base, call commit(files) for it, then for its tip (the manual benchmark)."""
    commit(scenario_dict["base"], "base")
    return commit(scenario_dict["tip"], "tip")
