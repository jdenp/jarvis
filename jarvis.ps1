# JARVIS - PowerShell entry point.
#
#   .\jarvis.ps1                  run the voice service in this terminal
#   .\jarvis.ps1 -Windowed        open a new terminal window and run it there
#   .\jarvis.ps1 --admin          elevated, so it can drive Task Manager and friends
#   .\jarvis.ps1 status           any other subcommand or flag is passed through
#
# -Windowed is what an agent should use: it returns immediately instead of
# blocking the caller, and the live transcript stays visible in its own window.
#
# --admin asks Windows for consent once, at launch, and never again: a child
# process inherits its parent's token, so everything JARVIS starts after that is
# already elevated and nothing prompts. That is the whole appeal and also the
# whole cost - read the note above the switch below before using it.

[CmdletBinding()]
param(
    [switch]$Windowed,
    [switch]$Admin,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Remaining
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

function Test-Elevated {
    $me = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    return $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

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

# -Admin, and what it costs.
#
# Windows blocks an unelevated process from reading an elevated window's
# accessibility tree, from sending it clicks or keys, and from receiving a
# keyboard hook while one has focus. Task Manager, an admin terminal, regedit:
# all invisible and untouchable. Elevating JARVIS lifts all of that, because you
# may reach down but never up.
#
# One consent prompt, at launch. After that a child process inherits this token,
# so nothing prompts again - which is the appeal, and is also the point: there is
# no second checkpoint. Every command run_command runs is an administrator
# command with nothing asked first, and every application it opens is elevated
# too. Mapped network drives also disappear: an elevated token is a separate
# logon session and does not see them.
#
# It cannot work over SSH. The consent dialog is drawn on the secure desktop,
# which by design nothing can see or click but somebody sitting at the machine.
$passthrough = if ($Remaining) { $Remaining } else { @() }

# --admin as well as -Admin. Everything else passed through this script is
# double dashed on its way to the Python CLI, so PowerShell's own switch syntax
# is the odd one out and both spellings are cheaper than remembering which.
if ($passthrough -contains "--admin") {
    $Admin = $true
    $passthrough = @($passthrough | Where-Object { $_ -ne "--admin" })
}

if ($Admin -and -not (Test-Elevated)) {
    $self = $PSCommandPath
    # No -Windowed and no -Admin: elevation already gives us a fresh console,
    # and this one is the JARVIS window.
    $again = @("-NoExit", "-ExecutionPolicy", "Bypass", "-File", $self)
    if ($passthrough) { $again += $passthrough }
    Write-Host "Asking Windows for administrator rights. Approve the prompt on the machine itself -"
    Write-Host "it cannot be clicked over SSH."
    try {
        Start-Process powershell -Verb RunAs -ArgumentList $again
    } catch {
        Write-Host "Elevation was refused or no interactive desktop is available." -ForegroundColor Red
        exit 1
    }
    exit 0
}

$cmd = Get-JarvisCommand

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
