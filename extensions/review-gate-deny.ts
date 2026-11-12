// review-gate-deny.ts -- read-only sandbox for the review-gate worker (port D13, chunk C3).
//
// Loaded into the headless reviewer via `kimchi -p ... -e <this file>`.
// Why an extension and not CLI flags: `--deny-tool`/`--allow-tool` and
// KIMCHI_PERMISSIONS are NOT enforced in headless -p runs on this build
// (verified, port spec section 6) -- sessions report permission_mode=yolo.
// A tool_call subscriber is enforced regardless of mode.
//
// Scope: while OCR_IN_REVIEW=1 is in the environment (only the review worker
// sets it; the supervisor strips it from everything else it spawns), the
// extension
//  - hard-denies the mutating tools (write/edit/web_fetch/web_search/mcp__*),
//  - enforces a BASH ALLOWLIST (git read verbs + read-only file tools),
//    denying everything else with a reason that says what to do instead,
//  - passes read/grep/find/ls/lsp_*/Agent through untouched.
// With OCR_IN_REVIEW unset it is provably inert: the handler returns
// undefined for every event (tested).
//
// Deny is fail-closed by construction: a bash command we cannot parse, or a
// bash payload we cannot even find in the event, is DENIED -- the reviewer
// loses a tool call, the push never loses its sandbox.
//
// Hardening policies (gate self-review findings):
//  - Leading VAR=value assignments are DENIED, never skipped. Exported env
//    reaches the child process, and git alone honors GIT_CONFIG_COUNT /
//    GIT_CONFIG_KEY_* / GIT_CONFIG_VALUE_* (arbitrary config, including `!`
//    aliases that execute an arbitrary command), GIT_DIR / GIT_WORK_TREE
//    (retargeting reads at any repo on disk, defeating GIT_ESCAPES),
//    GIT_CONFIG_PARAMETERS and GIT_PAGER -- each a full escape from the
//    read-only sandbox. The reviewer needs no environment overrides.
//  - sed is NOT on the allowlist, even with -n: its script language writes
//    arbitrary files (the w/W commands) and on GNU sed executes the shell
//    (the e command, the s///e substitution flag). head/cat/grep/tail cover
//    line-range reads.
//  - For every git verb the allowlist admits, --output, --output=<path> and
//    -o are DENIED: they write a file from inside the sandbox.

import { fileURLToPath } from "node:url"

// --- what the reviewer may run -------------------------------------------------

const GIT_READ_VERBS = new Set([
  "diff", "log", "show", "rev-parse", "cat-file", "blame", "ls-files", "grep",
  "status", "describe", "merge-base",
])

const READ_TOOLS = new Set(["grep", "rg", "find", "ls", "wc", "cat", "head", "tail"])

// Network clients (no fetch/exfil channel for the reviewed code or its output).
const NETWORK = new Set([
  "curl", "wget", "nc", "ncat", "netcat", "ssh", "scp", "sftp", "rsync",
  "ftp", "telnet", "gh",
])

// Interpreters / package managers: each executes arbitrary code -- a whole
// unsandboxed process, and node/npm can spawn a nested unsandboxed agent.
const INTERPRETERS = new Set([
  "node", "npm", "npx", "yarn", "pnpm", "pnpx", "make", "pip", "pip3",
  "python", "python2", "python3", "perl", "ruby", "lua", "tclsh", "bash",
  "sh", "zsh", "dash", "ksh", "source",
])

// git subcommands that mutate the repo, its config, or the network.
const GIT_WRITE_VERBS = new Set([
  "push", "pull", "fetch", "clone", "ls-remote", "checkout", "switch",
  "restore", "apply", "commit", "reset", "clean", "update-ref",
  "symbolic-ref", "worktree", "config", "merge", "rebase", "cherry-pick",
  "revert", "tag", "branch", "add", "rm", "mv", "init", "am", "stash",
])

// Flags that can point git at a DIFFERENT repository or config than the one
// this sandbox was built around. Any occurrence after the `git` token denies.
const GIT_ESCAPES = ["-C", "--git-dir", "--work-tree", "--git-common-dir",
  "--exec-path", "-c", "--namespace", "--super-prefix"]

// Options of the READ verbs whose following token is DATA, not a flag. The
// classic false positive: `git log --grep "git push"` -- the push is a search
// value. Only governs the BARE-value form; `--grep=x` carries its own value.
const GIT_VALUE_OPTS = new Set([
  "--grep", "--author", "--committer", "--since", "--until", "--format",
  "--pretty", "--max-count", "--diff-filter", "--abbrev", "--find-renames",
  "-S", "-G", "-n", "-U", "-L", "-M",
])

// find(1) options that execute or delete: the allowlist trusts find only as
// a directory walker.
const FIND_ESCAPES = ["-exec", "-execdir", "-ok", "-okdir", "-delete",
  "-fprint", "-fprintf", "-fls"]

const TOOL_DENIES = new Set(["write", "edit", "multiedit", "web_fetch",
  "web_search", "notebook_edit"])

const REDIRECT_OPS = [">", ">>", "&>", ">&", "<>", "<"]

// Every deny reason ends with what to do instead: a deny the model cannot
// act on is just a retry loop.
const _ADVICE = "The review sandbox is read-only: inspect with the " +
  "read/grep/find/ls tools or git read verbs (diff log show rev-parse " +
  "cat-file blame ls-files grep status describe merge-base, plus " +
  "grep/rg/find/ls/wc/cat/head/tail), and ask the supervisor " +
  "(outside the review) for anything else."

function deny(reason) {
  return { block: true, reason: reason + " " + _ADVICE }
}

// --- shell tokenizer ------------------------------------------------------------
//
// Splits a command line into words and operators, tracking quoting so values
// stay data. Returns null when the line uses a construct this model does not
// cover (command/process substitution, subshells, heredocs, unbalanced
// quotes): the caller DENIES on null -- ambiguity is never allowed.

const WORD = 0
const OP = 1

function tokenize(line) {
  const toks = []
  let text = ""
  let quoted = false
  let sawQuote = false
  let i = 0
  const flush = () => {
    if (text !== "") toks.push({ kind: WORD, text, quoted })
    text = ""
    quoted = false
  }
  while (i < line.length) {
    const c = line[i]
    if (!quoted && (c === " " || c === "\t" || c === "\n")) { flush(); i++; continue }
    if (c === "'") {
      const end = line.indexOf("'", i + 1)
      if (end < 0) return null
      text += line.slice(i + 1, end)
      quoted = true; sawQuote = true; i = end + 1; continue
    }
    if (c === '"') {
      const end = line.indexOf('"', i + 1)
      if (end < 0) return null
      // Backslash escapes are consumed, not kept: `\"` inside is a quote.
      text += line.slice(i + 1, end).replace(/\\(.)/g, "$1")
      quoted = true; sawQuote = true; i = end + 1; continue
    }
    if (c === "\\") {
      if (i + 1 >= line.length) return null
      text += line[i + 1]; i += 2; continue
    }
    if (!quoted && c === "$" && line[i + 1] === "(") return null // $( cmd )
    if (!quoted && c === "`") return null                          // ` cmd `
    if (!quoted && (c === "(" || c === ")")) return null           // subshell/group
    if (!quoted && c === "{" && text === "" && toks.length === 0 &&
        (i === 0 || /\s/.test(line[i - 1]))) return null           // { group; }
    if (!quoted && c === "<" && (line[i + 1] === "<" || line[i + 1] === "(")) {
      return null                                                  // heredoc / <( )
    }
    if (!quoted && c === ">" && line[i + 1] === "(") return null   // >( )
    if (!quoted && (c === ";" || c === "&" || c === "|" || c === "<" || c === ">")) {
      flush()
      let op = c
      const two = line.slice(i, i + 2)
      if (two === "&&" || two === "||" || two === ">>" || two === "|&" ||
          two === "<>" || two === ">&" || two === "&>") { op = two; i++ }
      toks.push({ kind: OP, text: op })
      i++; continue
    }
    text += c; i++
  }
  flush()
  if (toks.length === 0) return []
  return toks
}

// --- allowlist evaluation ---------------------------------------------------------

function checkBashCommand(cmd) {
  const toks = tokenize(cmd)
  if (toks === null) {
    return deny("bash command could not be parsed safely: it uses quoting, " +
      "substitution, or grouping the sandbox does not model.")
  }
  if (toks.length === 0) return undefined
  let words = []          // current segment's words
  let expectTarget = false // the next WORD is a redirect target, not a command
  const run = () => {
    if (words.length === 0) return undefined
    const bad = checkSegment(words)
    words = []
    return bad
  }
  for (let idx = 0; idx < toks.length; idx++) {
    const t = toks[idx]
    if (t.kind === WORD) {
      if (expectTarget) { expectTarget = false; continue }
      // A bare all-digits word glued to the next redirect op is an fd
      // prefix (`2>/dev/null`): `2` is not a command to evaluate.
      const next = toks[idx + 1]
      if (/^\d+$/.test(t.text) && next && next.kind === OP &&
          REDIRECT_OPS.includes(next.text)) continue
      words.push(t)
      continue
    }
    const bad = run()
    if (bad) return bad
    if (REDIRECT_OPS.includes(t.text)) {
      if (t.text === "<>") {
        return deny("read-write redirection <> writes a file.")
      }
      if (t.text === "<") { expectTarget = true; continue } // input: read-only
      // Output redirect: only /dev/null and fd dups (&1, &2) are safe.
      let j = idx + 1
      while (j < toks.length && toks[j].kind === OP) j++
      const target = j < toks.length ? toks[j].text : ""
      if (!(target === "/dev/null" || target === "&1" || target === "&2" ||
            target === "1" || target === "2")) {
        return deny("output redirection to " + JSON.stringify(target) +
          " writes outside the read-only sandbox.")
      }
      expectTarget = true
      continue
    }
    if (t.text === "<<" || t.text === "<<-") {
      return deny("heredocs are outside the read-only sandbox.")
    }
    // ; && || | & -- plain separators: each side evaluated on its own.
  }
  return run()
}

function checkSegment(words) {
  // Leading VAR=value assignments are DENIED, not skipped: exported
  // environment reaches the child and git alone honors GIT_CONFIG_COUNT /
  // GIT_CONFIG_KEY_* / GIT_CONFIG_VALUE_* (arbitrary config incl. `!` alias
  // shell execution), GIT_DIR / GIT_WORK_TREE (repo retarget),
  // GIT_CONFIG_PARAMETERS, GIT_PAGER ... (see header policy).
  if (!words[0].quoted && /^[A-Za-z_][A-Za-z0-9_]*=/.test(words[0].text)) {
    return deny("leading environment assignment " +
      JSON.stringify(words[0].text) + " can steer the command it prefixes " +
      "(git reads GIT_CONFIG_*, GIT_DIR, GIT_WORK_TREE, GIT_PAGER, ...) and " +
      "is not allowed in the read-only sandbox.")
  }
  const name = words[0].text
  if (name.includes("/")) {
    return deny(JSON.stringify(name) + " is a path, not an allowlisted " +
      "command name (a path could be any executable).")
  }
  if (name === "git") return checkGit(words.slice(1))
  if (NETWORK.has(name)) {
    return deny(name + " opens a network connection from inside the review.")
  }
  if (INTERPRETERS.has(name)) {
    return deny(name + " executes arbitrary code outside the sandbox (and " +
      "could spawn an unsandboxed nested agent session).")
  }
  if (name === "sed") {
    // Not on the allowlist at all: the sed script language writes files
    // (`sed -n 'w /tmp/out' f`; W on GNU sed) and GNU sed executes the
    // shell (the e command and the s///e substitution flag). -n does not
    // restrict the script, so no flag check can make sed read-only.
    return deny("sed is not on the read-only allowlist: its script " +
      "language writes files (w command) and GNU sed executes the shell " +
      "(e command, s///e flag). Use head/cat/grep/tail for line reads.")
  }
  if (name === "find") {
    const flags = words.slice(1).map((w) => w.text)
    const bad = flags.find((f) => FIND_ESCAPES.includes(f) || f.startsWith("-fprint"))
    if (bad) {
      return deny("find " + bad + " executes or writes; it is not the " +
        "read-only directory walk the allowlist permits.")
    }
    return undefined
  }
  if (READ_TOOLS.has(name)) return undefined
  return deny(JSON.stringify(name) + " is not on the read-only allowlist.")
}

function checkGit(rest) {
  if (rest.length === 0) {
    return deny("bare `git` is not a read verb; name one (git log, git diff, ...).")
  }
  // Global options before the subcommand: each is either on the escape list
  // (deny) or skipped; the first non-option word is the verb.
  let i = 0
  while (i < rest.length && rest[i].text.startsWith("-")) {
    const flag = rest[i].text.split("=")[0]
    if (GIT_ESCAPES.includes(flag)) {
      return deny("git " + flag + " retargets git at a different repo or " +
        "config: a sandbox escape.")
    }
    i++
  }
  if (i >= rest.length) {
    return deny("git with no read verb; name one (git log, git diff, ...).")
  }
  const verb = rest[i].text
  if (GIT_WRITE_VERBS.has(verb)) {
    return deny("git " + verb + " mutates the repository or its remotes.")
  }
  if (!GIT_READ_VERBS.has(verb)) {
    return deny("git " + JSON.stringify(verb) + " is not one of the read " +
      "verbs the sandbox allows.")
  }
  // Arguments after the verb. Option VALUES are data (the
  // `git log --grep "git push"` case), and everything after `--` is a path.
  // Quoting does NOT neutralize a flag here: git itself ignores the quotes,
  // so a quoted "-C" is still -C.
  for (let j = i + 1; j < rest.length; j++) {
    const w = rest[j]
    if (w.text === "--") return undefined
    const prev = j > 0 ? rest[j - 1].text : ""
    if (GIT_VALUE_OPTS.has(prev) && !prev.includes("=") && prev.startsWith("-")) {
      continue // this token is the previous option's VALUE: data
    }
    const flag = w.text.split("=")[0]
    if (w.text.startsWith("-") && GIT_ESCAPES.includes(flag)) {
      return deny("git " + flag + " retargets git at a different repo or " +
        "config: a sandbox escape.")
    }
    // --output (and --output=<path>) writes a file from inside the sandbox;
    // -o is the short form where a verb supports it. None of the admitted
    // read verbs need a file sink: the extension captures the output stream.
    if (flag === "--output" || w.text === "-o") {
      return deny("git " + flag + " writes output to a file inside the " +
        "sandbox; read the command's own stdout instead.")
    }
  }
  return undefined
}

// --- the extension ----------------------------------------------------------------

function decide(toolName, input) {
  if (process.env.OCR_IN_REVIEW !== "1") return undefined
  const name = (toolName === null || toolName === undefined)
    ? "" : String(toolName).toLowerCase()
  if (TOOL_DENIES.has(name)) {
    return deny("The " + toolName + " tool is blocked: the review worker is read-only.")
  }
  if (name.startsWith("mcp__")) {
    return deny("MCP servers are disabled for the review worker (tool " +
      toolName + ").")
  }
  if (name === "bash") {
    let command = null
    if (typeof input === "string") {
      command = input
    } else if (input && typeof input === "object") {
      for (const key of ["command", "cmd", "script"]) {
        if (typeof input[key] === "string") { command = input[key]; break }
      }
    }
    if (command === null) {
      return deny("The bash command payload could not be inspected, so it " +
        "is not provably read-only.")
    }
    return checkBashCommand(command)
  }
  // read, grep, find, ls, lsp_*, Agent (and anything else read-shaped) pass.
  return undefined
}

// --- skill wiring (DOCS-GATE-08) -------------------------------------------------
//
// `kimchi -p "/skill:review ..."` does NOT expand slash commands in a
// headless prompt: the text reaches the model verbatim, which then resolves
// the skill by NAME (observed: skill_view name="review") against the loaded
// skill catalogue -- and the catalogue only holds the user's global skills
// (config.json skillPaths), where a `code-review` skill shadows the lookup
// and the gate's real review skill never ran. Verified live 2026-11-11.
//
// The mechanism kimchi natively supports for a per-invocation fix (no
// mutation of the user's global config): a `-e` extension's resources_discover
// handler may return skillPaths, which the runner extends into the resource
// loader at session start (print mode included) and then rebuilds the system
// prompt. The deny extension is ALREADY loaded into the worker via -e, so it
// doubles as the carrier: while OCR_IN_REVIEW=1 it contributes this plugin's
// own skills/review/SKILL.md (frontmatter name: review), making the worker's
// `skill:review` / skill_view("review") resolve to THIS file -- including its
// docs-checker pass -- instead of any foreign global skill.

// Absolute path of <plugin>/skills/review/SKILL.md: extensions/../skills/...
// Same import.meta.url idiom the harness's own bundled extensions use.
const _REVIEW_SKILL_PATH =
  fileURLToPath(new URL("../skills/review/SKILL.md", import.meta.url))

export default function denyExtension(pi) {
  pi.on("tool_call", (event) => {
    if (!event) return undefined
    const toolName = event.toolName !== undefined ? event.toolName : event.tool_name
    const input = event.input !== undefined ? event.input
      : (event.toolInput !== undefined ? event.toolInput
        : (event.tool_input !== undefined ? event.tool_input : event.args))
    return decide(toolName, input)
  })
  pi.on("resources_discover", () => {
    // Same inertness rule as the sandbox: only inside the review worker.
    if (process.env.OCR_IN_REVIEW !== "1") return undefined
    return { skillPaths: [_REVIEW_SKILL_PATH] }
  })
}

// Exposed for the test suite (node runs this file's erasable TypeScript
// directly); the default export is the production entry point.
export const _internals = { tokenize, checkBashCommand, checkSegment, checkGit, decide }
