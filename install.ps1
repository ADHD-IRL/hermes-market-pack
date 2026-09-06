<#
.SYNOPSIS
    Install the Hermes Market Pack skills into a Hermes Agent installation.

.DESCRIPTION
    Copies this pack's skills into $env:LOCALAPPDATA\hermes\skills\finance
    (or $env:HERMES_HOME\skills\finance when that is set).

    Copies rather than links: symbolic links on Windows require Developer Mode
    or an elevated shell, so a copy is what works without privileges. Re-run
    this script after pulling updates.

.PARAMETER Uninstall
    Remove the pack's skills. Your trade journal is left untouched.

.PARAMETER Link
    Use symbolic links instead of copies. Requires Developer Mode or an
    elevated PowerShell session.

.EXAMPLE
    .\install.ps1
    .\install.ps1 -Uninstall
#>

[CmdletBinding()]
param(
    [switch]$Uninstall,
    [switch]$Link
)

$ErrorActionPreference = 'Stop'

$SourceDir = Split-Path -Parent $MyInvocation.MyCommand.Path

# Mirror hermes_constants._get_platform_default_hermes_home().
if ($env:HERMES_HOME -and $env:HERMES_HOME.Trim()) {
    $HermesHome = $env:HERMES_HOME.Trim()
} elseif ($env:LOCALAPPDATA) {
    $HermesHome = Join-Path $env:LOCALAPPDATA 'hermes'
} else {
    $HermesHome = Join-Path $env:USERPROFILE 'AppData\Local\hermes'
}

$TargetDir = Join-Path $HermesHome 'skills\finance'
$Skills = @('options-chain', 'options-strategy', 'position-sizing', 'trade-journal', 'market-brief')

if ($Uninstall) {
    foreach ($skill in $Skills) {
        $dest = Join-Path $TargetDir $skill
        if (Test-Path -LiteralPath $dest) {
            Remove-Item -LiteralPath $dest -Recurse -Force
            Write-Host "removed  $skill"
        }
    }
    Write-Host ''
    Write-Host "Done. Your trade journal at $HermesHome\trade_journal.db was left untouched."
    exit 0
}

if (-not (Test-Path -LiteralPath $TargetDir)) {
    New-Item -ItemType Directory -Path $TargetDir -Force | Out-Null
}

foreach ($skill in $Skills) {
    $src = Join-Path $SourceDir "skills\finance\$skill"
    $dest = Join-Path $TargetDir $skill

    if (-not (Test-Path -LiteralPath $src)) {
        Write-Warning "missing source for $skill, skipping"
        continue
    }

    if (Test-Path -LiteralPath $dest) {
        Remove-Item -LiteralPath $dest -Recurse -Force
    }

    if ($Link) {
        try {
            New-Item -ItemType SymbolicLink -Path $dest -Target $src -ErrorAction Stop | Out-Null
            Write-Host "linked   $skill"
        } catch {
            # Developer Mode is off and the shell is not elevated.
            Copy-Item -LiteralPath $src -Destination $dest -Recurse -Force
            Write-Host "copied   $skill  (symlink refused: enable Developer Mode or run elevated)"
        }
    } else {
        Copy-Item -LiteralPath $src -Destination $dest -Recurse -Force
        Write-Host "copied   $skill"
    }
}

Write-Host ''
Write-Host "Installed to $TargetDir"
if (-not $Link) {
    Write-Host '(copied, not linked - re-run this script after pulling updates)'
}
Write-Host ''
Write-Host 'Next steps:'
Write-Host "  1. Create a watchlist:  'AAPL' | Out-File -Append -Encoding utf8 '$HermesHome\watchlist.txt'"
Write-Host '  2. Restart Hermes so it picks up the new skills'
Write-Host '  3. Try it:              hermes  ->  "what does the AAPL option chain look like?"'
