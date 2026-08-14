# review-gate — PreToolUse adapter for Windows without Git Bash.
#
# Sibling of gate-hook.sh. Claude Code passes a shell-form hook command to
# `sh -c` on macOS/Linux, Git Bash on Windows, or PowerShell when Git Bash is
# not installed -- and there is NO platform-conditional mechanism for hooks
# (`if` is permission-rule syntax, not a platform test). So both entries in
# hooks.json fire on every matching push and each one has to decide for itself
# whether it is the right adapter for this machine. This one defers when Git
# Bash is installed, because then gate-hook.sh runs and owns the decision.
#
# Failure policy matches gate-hook.sh: FAIL CLOSED. OCR_FAIL_OPEN=1 bypasses.

$ErrorActionPreference = 'Stop'

function Write-Decision {
    param([string]$Decision, [string]$Reason = '')
    $payload = @{
        hookSpecificOutput = @{
            hookEventName        = 'PreToolUse'
            permissionDecision   = $Decision
        }
    }
    if ($Reason) { $payload.hookSpecificOutput.permissionDecisionReason = $Reason }
    Write-Output ($payload | ConvertTo-Json -Compress -Depth 5)
}

function Test-Truthy { param([string]$v) return @('1', 'true', 'yes') -contains $v.Trim().ToLower() }

# --- Re-entry guard -----------------------------------------------------------
# review-gate.py runs the headless review with --plugin-dir, so this plugin --
# including this hook -- is registered inside the review session too.
if ($env:OCR_IN_REVIEW -eq '1') { Write-Decision 'allow'; exit 0 }

# --- Should this adapter run at all? ------------------------------------------
# Deliberately biased toward RUNNING. Deferring when gate-hook.sh cannot
# actually run means both adapters go inert and the gate silently disappears --
# the exact fail-open this project exists to prevent. Running when we did not
# need to costs a duplicate review; not running costs an unreviewed push.
#
# The test mirrors how Claude Code itself picks the hook shell: Git Bash by
# INSTALLATION, not by PATH. Git for Windows' "command line only" setup option
# leaves bash.exe on disk while keeping it off PATH, and gate-hook.sh still
# runs fine there -- so a PATH probe alone would wrongly conclude we are needed
# and double every review. A WSL bash in System32 is explicitly not Git Bash;
# it does not make gate-hook.sh runnable.
function Test-GitBashInstalled {
    foreach ($cmd in @(Get-Command bash.exe -All -ErrorAction SilentlyContinue)) {
        $p = $cmd.Source
        if (-not $p) { continue }
        if ($p -match '\\System32\\' -or $p -match 'WindowsApps') { continue }
        return $true
    }
    # Git Bash installed but not on PATH: look next to git.exe. Git for Windows
    # keeps bash at <root>\bin\bash.exe and <root>\usr\bin\bash.exe, where
    # git.exe lives at <root>\cmd\git.exe or <root>\bin\git.exe.
    $git = Get-Command git.exe -ErrorAction SilentlyContinue
    if ($git -and $git.Source) {
        $root = Split-Path (Split-Path $git.Source -Parent) -Parent
        foreach ($rel in @('bin\bash.exe', 'usr\bin\bash.exe')) {
            if (Test-Path (Join-Path $root $rel)) { return $true }
        }
    }
    return $false
}

if (Test-GitBashInstalled) {
    # Emit NOTHING and exit 0. A PreToolUse hook that exits 0 without JSON
    # contributes no decision, so this cannot override gate-hook.sh's deny.
    exit 0
}

# --- Find a working Python ----------------------------------------------------
# Same rules as gate-hook.sh: skip the Windows Store alias stubs, which resolve
# on PATH but cannot execute.
$py = $null
foreach ($name in @('python3', 'python', 'py')) {
    foreach ($cmd in @(Get-Command $name -All -ErrorAction SilentlyContinue)) {
        $p = $cmd.Source
        if (-not $p -or $p -match 'WindowsApps') { continue }
        try {
            & $p -c 'import sys' 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) { $py = $p; break }
        } catch { continue }
    }
    if ($py) { break }
}

$core = Join-Path $PSScriptRoot 'review-gate.py'

if (-not $py -or -not (Test-Path $core)) {
    # FAIL CLOSED. A gate that cannot run is not a reason to wave a push
    # through -- that is the whole premise of this project. The one escape
    # hatch has to be named here, because the failure is silent otherwise.
    if (Test-Truthy "$($env:OCR_FAIL_OPEN)") { Write-Decision 'allow'; exit 0 }
    $why = if (-not $py) {
        'no working Python 3 interpreter found'
    } else {
        "review-gate.py missing at $core (broken install)"
    }
    Write-Decision 'deny' (@(
        "review-gate: $why, so the push could not be reviewed. Blocking, because a gate that cannot run must not wave a push through.",
        '',
        'Fix it:',
        '  - Install Python 3 (or repair the plugin install), then retry.',
        '  - Run /review-gate:doctor to see what is missing.',
        '  - Emergency bypass: set OCR_FAIL_OPEN=1 in the environment Claude Code',
        '    itself was launched from. A shell prefix on `git push` will NOT work --',
        '    this hook inherits Claude Code''s environment, not the Bash tool call''s.',
        '  - Or push from a plain terminal, which this adapter does not gate.'
    ) -join "`n")
    exit 0
}

# --- Run the gate -------------------------------------------------------------
# Stdin carries the PreToolUse payload. Pipe it through as raw BYTES: piping to
# a native child in Windows PowerShell 5.1 re-encodes via $OutputEncoding
# (ASCII/OEM by default), which would mangle non-ASCII paths in the JSON.
$stdinBytes = New-Object System.IO.MemoryStream
[Console]::OpenStandardInput().CopyTo($stdinBytes)
$bytes = $stdinBytes.ToArray()

$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName               = $py
$psi.Arguments              = '"' + $core + '" --mode hook'
$psi.UseShellExecute         = $false
$psi.RedirectStandardInput   = $true
$psi.RedirectStandardOutput  = $true
$psi.RedirectStandardError   = $true

$proc = [System.Diagnostics.Process]::Start($psi)
# Read stdout asynchronously before writing stdin, so a large payload cannot
# deadlock against a full pipe buffer.
$stdoutTask = $proc.StandardOutput.ReadToEndAsync()
$stderrTask = $proc.StandardError.ReadToEndAsync()
$proc.StandardInput.BaseStream.Write($bytes, 0, $bytes.Length)
$proc.StandardInput.BaseStream.Flush()
$proc.StandardInput.Close()
$proc.WaitForExit()

$stdout = $stdoutTask.Result
$stderr = $stderrTask.Result
if ($stderr) { [Console]::Error.Write($stderr) }

if ($stdout.Trim()) {
    [Console]::Out.Write($stdout)
    exit $proc.ExitCode
}

# review-gate.py produced no decision. It is built to always emit one, so
# reaching here means it died in a way its own fail-closed wrapper did not
# catch. Block rather than let the silence read as approval.
if (Test-Truthy "$($env:OCR_FAIL_OPEN)") { Write-Decision 'allow'; exit 0 }
Write-Decision 'deny' "review-gate: the reviewer exited $($proc.ExitCode) without returning a verdict, so the push was not reviewed. Set OCR_FAIL_OPEN=1 in the environment Claude Code was launched from to bypass."
exit 0
