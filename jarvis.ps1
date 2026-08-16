# JARVIS - PowerShell entry point.
#
#   .\jarvis.ps1                  run the voice service in this terminal
#   .\jarvis.ps1 -Windowed        open a new terminal window and run it there
#   .\jarvis.ps1 status           any other subcommand or flag is passed through
#
# -Windowed is what an agent should use: it returns immediately instead of
# blocking the caller, and the live transcript stays visible in its own window.

[CmdletBinding()]
param(
    [switch]$Windowed,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Remaining
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

function Get-JarvisCommand {
    # --directory rather than Set-Location: this script runs in the caller's
    # session, so changing directory here would move their shell as a side effect.
    #
    # --no-sync because `uv run` otherwise reinstalls the project whenever its
    # metadata changes, and reinstalling means replacing .venv\Scripts\jarvis.exe.
    # An MCP server started by an agent is running from that exe, and Windows
    # locks a running image - so bumping the version made every start fail with
    # "The process cannot access the file". The project is installed editable, so
    # source changes need no sync anyway; only changed dependencies do.
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        if (-not (Test-Path (Join-Path $root ".venv\Scripts\python.exe"))) {
            Write-Host "No virtual environment yet, running uv sync..."
            & uv sync --directory $root
            if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        }
        return @{ File = "uv"; Prefix = @("run", "--no-sync", "--directory", $root, "jarvis") }
    }
    $python = Join-Path $root ".venv\Scripts\python.exe"
    if (-not (Test-Path $python)) {
        Write-Error "No uv on PATH and no virtual environment at $python. Run: uv sync"
        exit 1
    }
    $env:PYTHONPATH = Join-Path $root "src"
    return @{ File = $python; Prefix = @("-m", "jarvis") }
}

$cmd = Get-JarvisCommand
$passthrough = if ($Remaining) { $Remaining } else { @() }

if (-not $Windowed) {
    & $cmd.File @($cmd.Prefix + $passthrough)
    exit $LASTEXITCODE
}

# Already listening? Starting a second one just fails to bind the port.
# Stderr from a native exe is a terminating error while ErrorActionPreference
# is Stop, and `jarvis status` writes there when nothing is listening.
$previous = $ErrorActionPreference
$ErrorActionPreference = "Continue"
$status = (& $cmd.File @($cmd.Prefix + @("status")) 2>&1 | Out-String).Trim()
$running = $LASTEXITCODE -eq 0
$ErrorActionPreference = $previous

if ($running) {
    Write-Host "JARVIS is already running."
    Write-Host $status
    exit 0
}

$arguments = ($cmd.Prefix + $passthrough | ForEach-Object { "'$_'" }) -join " "
$inner = "& '$($cmd.File)' $arguments"
$title = "JARVIS"

if (Get-Command wt -ErrorAction SilentlyContinue) {
    Start-Process wt -ArgumentList @(
        "--title", $title, "powershell", "-NoExit", "-Command", $inner
    )
} else {
    Start-Process powershell -ArgumentList @("-NoExit", "-Command", "`$host.UI.RawUI.WindowTitle='$title'; $inner")
}

Write-Host "JARVIS starting in a new window. The first run loads the Whisper model, so give it a few seconds."
Write-Host "Check it came up with:  jarvis status"
exit 0
