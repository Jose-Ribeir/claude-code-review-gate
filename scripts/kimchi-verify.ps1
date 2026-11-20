# Real-kimchi verification (PowerShell wrapper). Run once where kimchi is
# installed and paste back .\kimchi-verify-report.txt:
#   powershell -File scripts\kimchi-verify.ps1
# Same as scripts/kimchi-verify.sh; exits non-zero when any check fails.
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = $null
foreach ($c in 'python3', 'python', 'py') {
    $cmd = Get-Command $c -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source -notmatch 'WindowsApps') { $py = $cmd.Source; break }
}
if (-not $py) {
    Write-Error 'kimchi-verify: no working Python 3 found. Install Python 3 and re-run.'
    exit 1
}
& $py (Join-Path $here 'kimchi_verify.py') @args
exit $LASTEXITCODE
