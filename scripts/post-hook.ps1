# review-gate - PostToolUse adapter for Windows without Git Bash.
#
# Sibling of post-hook.sh, and the PostToolUse counterpart to gate-hook.ps1.
# Claude Code passes a shell-form hook command to `sh -c` on macOS/Linux, Git
# Bash on Windows, or PowerShell when Git Bash is not installed, and there is no
# platform-conditional mechanism for hooks -- so both entries fire and each has
# to decide whether it is the right adapter for this machine.
#
# Failure policy: FAIL OPEN, unlike gate-hook.ps1 and deliberately so. That one
# decides whether a push proceeds, so it must block when it cannot run. This one
# decides only whether a report is printed; a reporting path that cannot run
# goes quiet. Nothing but a JSON object may ever reach stdout.

$ErrorActionPreference = 'Stop'

try {
    # Re-entry guard: this plugin is loaded into the headless review session via
    # --plugin-dir, where this hook would fire on every Bash call it makes.
    if ($env:OCR_IN_REVIEW -eq '1') { exit 0 }

    # Read raw BYTES: piping to a native child in Windows PowerShell 5.1
    # re-encodes via $OutputEncoding (ASCII/OEM by default), which would mangle
    # non-ASCII paths inside the JSON payload.
    $stdinBytes = New-Object System.IO.MemoryStream
    [Console]::OpenStandardInput().CopyTo($stdinBytes)
    $bytes = $stdinBytes.ToArray()

    $payloadText = [System.Text.Encoding]::UTF8.GetString($bytes)
    if ($payloadText -notlike '*git push*') {
        # See post-hook.sh: a review parked by a push that never reported still
        # has to reach the model, and a push that FAILED never reports -- no
        # PostToolUse hook fires for it. Cheap existence test, nothing spawned.
        # Mirrors _gate_data_dir() in review-gate.py; keep the two in step.
        $data = $env:CLAUDE_PLUGIN_DATA
        if (-not $data) {
            $cfg = $env:CLAUDE_CONFIG_DIR
            if (-not $cfg) { $cfg = Join-Path $env:USERPROFILE '.claude' }
            $data = Join-Path $cfg 'plugins\data\review-gate-local'
        }
        if (-not (Test-Path (Join-Path $data 'pending-*'))) { exit 0 }
    }

    # Defer to post-hook.sh when Git Bash is installed, mirroring
    # gate-hook.ps1's test (installation, not PATH -- Git for Windows'
    # "command line only" option keeps bash.exe off PATH but still runnable,
    # and a System32 bash is WSL, not Git Bash). Running both would only cost a
    # wasted process rather than a duplicate report, since the delivered-marker
    # is claimed atomically -- but there is no reason to pay for it.
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

    if (Test-GitBashInstalled) { exit 0 }

    # Same interpreter rules as gate-hook.ps1: skip Store alias stubs.
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
    # No Python or a broken install: stay silent. The findings are still in
    # .git/review-gate-findings.jsonl and `--history` still replays them.
    if (-not $py -or -not (Test-Path $core)) { exit 0 }

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName              = $py
    $psi.Arguments             = '"' + $core + '" --mode post'
    $psi.UseShellExecute       = $false
    $psi.RedirectStandardInput  = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError  = $true

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
    if ($stdout.Trim()) { [Console]::Out.Write($stdout) }
} catch {
    # Any failure here is a failure to REPORT. Swallow it: nothing about a
    # missing report should surface as a hook error on the user's push.
}
exit 0
