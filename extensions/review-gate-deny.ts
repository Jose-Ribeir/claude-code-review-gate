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

const REDIRECT_OPS = [">", ">>", "&>", ">&", "<>"]

// Every deny reason ends with what to do instead: a deny the model cannot
// act on is just a retry loop.
const _ADVICE = "The review sandbox is read-only: inspect with the " +
  "read/grep/find/ls tools or git read verbs (diff log show rev-parse " +
  "cat-file blame ls-files grep status describe merge-base, plus " +
  "grep/rg/find/ls/wc/cat/head/tail/sed -n), and ask the supervisor " +
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
  // Leading VAR=value assignments are environment for the command, not the
  // command itself.
  let i = 0
  while (i < words.length && !words[i].quoted &&
         /^[A-Za-z_][A-Za-z0-9_]*=/.test(words[i].text)) {
    i++
  }
  if (i >= words.length) return undefined
  const name = words[i].text
  if (name.includes("/")) {
    return deny(JSON.stringify(name) + " is a path, not an allowlisted " +
      "command name (a path could be any executable).")
  }
  if (name === "git") return checkGit(words.slice(i + 1))
  if (NETWORK.has(name)) {
    return deny(name + " opens a network connection from inside the review.")
  }
  if (INTERPRETERS.has(name)) {
    return deny(name + " executes arbitrary code outside the sandbox (and " +
      "could spawn an unsandboxed nested agent session).")
  }
  if (name === "sed") {
    const flags = words.slice(i + 1).map((w) => w.text)
    if (flags.some((f) => f === "-i" || f.startsWith("-i"))) {
      return deny("sed -i edits files in place.")
    }
    if (!flags.includes("-n")) {
      return deny("sed without -n is not on the read-only allowlist (only " +
        "`sed -n <script> <file>` is).")
    }
    return undefined
  }
  if (name === "find") {
    const flags = words.slice(i + 1).map((w) => w.text)
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

export default function denyExtension(pi) {
  pi.on("tool_call", (event) => {
    if (!event) return undefined
    const toolName = event.toolName !== undefined ? event.toolName : event.tool_name
    const input = event.input !== undefined ? event.input
      : (event.toolInput !== undefined ? event.toolInput
        : (event.tool_input !== undefined ? event.tool_input : event.args))
    return decide(toolName, input)
  })
}

// Exposed for the test suite (node runs this file's erasable TypeScript
// directly); the default export is the production entry point.
export const _internals = { tokenize, checkBashCommand, checkSegment, checkGit, decide }
