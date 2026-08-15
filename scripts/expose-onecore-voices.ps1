# Make Windows OneCore voices visible to SAPI5.
#
# Windows ships more voices than SAPI can see. Microsoft George (British male)
# is usually installed as a OneCore voice only, so `jarvis` cannot select it and
# falls back to Hazel. This copies the OneCore voice registrations across to the
# SAPI5 hive, which is where SAPI looks.
#
# Needs an elevated PowerShell. Registrations point at the same voice data
# already on disk - nothing is downloaded and no files are modified.
#
#   .\scripts\expose-onecore-voices.ps1              # copy every OneCore voice
#   .\scripts\expose-onecore-voices.ps1 -Name George # just one
#   .\scripts\expose-onecore-voices.ps1 -List        # show what is available
#   .\scripts\expose-onecore-voices.ps1 -Undo        # remove what this added

[CmdletBinding()]
param(
    [string]$Name,
    [switch]$List,
    [switch]$Undo
)

$ErrorActionPreference = "Stop"
$oneCore = "HKLM:\SOFTWARE\Microsoft\Speech_OneCore\Voices\Tokens"
$sapi = "HKLM:\SOFTWARE\Microsoft\Speech\Voices\Tokens"

function Get-VoiceInfo($path) {
    Get-ChildItem $path -ErrorAction SilentlyContinue | ForEach-Object {
        $attributes = Get-ItemProperty "$($_.PSPath)\Attributes" -ErrorAction SilentlyContinue
        [PSCustomObject]@{
            Key         = $_.PSChildName
            Description = (Get-ItemProperty $_.PSPath -ErrorAction SilentlyContinue).'(default)'
            Gender      = $attributes.Gender
            Language    = $attributes.Language
        }
    }
}

if ($List) {
    Write-Host "`nVisible to SAPI5 now:" -ForegroundColor Cyan
    Get-VoiceInfo $sapi | Format-Table Description, Gender, Language -AutoSize
    Write-Host "Installed as OneCore only:" -ForegroundColor Cyan
    $existing = (Get-VoiceInfo $sapi).Key
    Get-VoiceInfo $oneCore | Where-Object { $_.Key -notin $existing } |
        Format-Table Description, Gender, Language -AutoSize
    exit 0
}

$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Error "This writes to HKLM, so it needs an elevated PowerShell. Right click, Run as administrator."
    exit 1
}

$voices = Get-VoiceInfo $oneCore
if ($Name) { $voices = $voices | Where-Object { $_.Key -like "*$Name*" -or $_.Description -like "*$Name*" } }
if (-not $voices) { Write-Error "No matching OneCore voice found. Try -List."; exit 1 }

foreach ($voice in $voices) {
    $target = Join-Path $sapi $voice.Key
    if ($Undo) {
        if (Test-Path $target) {
            Remove-Item $target -Recurse -Force
            Write-Host "removed  $($voice.Description)" -ForegroundColor Yellow
        }
        continue
    }
    if (Test-Path $target) {
        Write-Host "already  $($voice.Description)" -ForegroundColor DarkGray
        continue
    }
    # reg copy keeps the values byte for byte, which matters for the binary
    # attributes SAPI reads back.
    $from = "HKLM\SOFTWARE\Microsoft\Speech_OneCore\Voices\Tokens\$($voice.Key)"
    $to = "HKLM\SOFTWARE\Microsoft\Speech\Voices\Tokens\$($voice.Key)"
    & reg.exe copy $from $to /s /f | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "exposed  $($voice.Description)" -ForegroundColor Green
    } else {
        Write-Host "failed   $($voice.Description)" -ForegroundColor Red
    }
}

Write-Host "`nSAPI5 can now use:" -ForegroundColor Cyan
Get-VoiceInfo $sapi | Format-Table Description, Gender, Language -AutoSize
Write-Host "Restart JARVIS to pick this up." -ForegroundColor Cyan
