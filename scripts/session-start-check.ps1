# review-gate — SessionStart adapter for Windows without Git Bash.
#
# Sibling of session-start-check.sh. Claude Code has no "on install" hook, so
# this is the earliest reliable place to warn a user that the push gate can't
# run, instead of letting them discover it only when a real `git push` gets
# denied. Defers when Git Bash is installed, same rule as gate-hook.ps1, so
# the warning doesn't print twice on a machine where both adapters would
# otherwise run.
#
# Non-blocking (SessionStart can't stop the session anyway) and silent when a
# working Python is found, so it never becomes noise once resolved.

$ErrorActionPreference = 'Stop'

if ($env:OCR_IN_REVIEW -eq '1') { exit 0 }

# Same test as gate-hook.ps1: Git Bash by INSTALLATION, not by PATH.
function Test-GitBashInstalled {
    foreach ($cmd in @(Get-Command bash.exe -All -ErrorAction SilentlyContinue)) {
        $p = $cmd.Source
        if (-not $p) { continue }
        if ($p -match '\\System32\\' -or $p -match 'WindowsApps') { continue }
        return $true
    }
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
    # session-start-check.sh owns the decision when it can actually run.
    exit 0
}

# Same interpreter search as gate-hook.ps1: skip Windows Store alias stubs.
$py = $null
foreach ($name in @('python3', 'python', 'py')) {
    foreach ($cmd in @(Get-Command $name -All -ErrorAction SilentlyContinue)) {
        $p = $cmd.Source
        if (-not $p -or $p -match 'WindowsApps') { continue }
        try {
            & $p -c 'import sys; sys.exit(0 if sys.version_info >= (3, 7) else 1)' 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) { $py = $p; break }
        } catch { continue }
    }
    if ($py) { break }
}

if ($py) {
    # Python is fine. Same resume context as session-start-check.sh: if this
    # session's previous process died mid-review, say so (see --mode resume).
    # Best effort; nothing but one JSON object may reach stdout.
    try {
        $core = Join-Path $PSScriptRoot 'review-gate.py'
        if (Test-Path $core) {
            $stdinBytes = New-Object System.IO.MemoryStream
            [Console]::OpenStandardInput().CopyTo($stdinBytes)
            $bytes = $stdinBytes.ToArray()
            $psi = New-Object System.Diagnostics.ProcessStartInfo
            $psi.FileName = $py
            $psi.Arguments = '"' + $core + '" --mode resume'
            $psi.UseShellExecute = $false
            $psi.RedirectStandardInput = $true
            $psi.RedirectStandardOutput = $true
            $psi.RedirectStandardError = $true
            $proc = [System.Diagnostics.Process]::Start($psi)
            $stdoutTask = $proc.StandardOutput.ReadToEndAsync()
            $stderrTask = $proc.StandardError.ReadToEndAsync()
            $proc.StandardInput.BaseStream.Write($bytes, 0, $bytes.Length)
            $proc.StandardInput.Close()
            # Bounded: a reporter that stalls must go quiet, never hold up
            # session start. hooks.json gives this hook 15 s in total.
            if (-not $proc.WaitForExit(8000)) {
                try { $proc.Kill() } catch { }
                exit 0
            }
            $null = $stderrTask.Result
            $out = $stdoutTask.Result
            if ($out.Trim()) { [Console]::Out.Write($out) }
        }
    } catch { }
    exit 0
}

Write-Output 'review-gate: no working Python 3 interpreter found. The push gate FAILS CLOSED and will block `git push` until this is fixed.'
Write-Output '  - Install Python 3, then restart Claude Code so it picks up the new PATH.'
Write-Output '  - Run /review-gate:doctor for a full diagnosis.'
Write-Output '  - Emergency bypass: set OCR_FAIL_OPEN=1 in the environment Claude Code itself was launched from.'
exit 0
