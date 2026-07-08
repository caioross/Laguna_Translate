# Laguna Translator — instala atalhos (Desktop + Start Menu).
# Uso: clicar com direito em "install_shortcuts.ps1" -> Run with PowerShell
# Ou: powershell -ExecutionPolicy Bypass -File install_shortcuts.ps1

$ErrorActionPreference = "Stop"

$here     = Split-Path -Parent $MyInvocation.MyCommand.Definition
$launcher = Join-Path $here "Laguna.vbs"
$iconPath = Join-Path $here "static\laguna.ico"

if (-not (Test-Path $launcher)) {
    Write-Error "Launcher not found / Nao achei: $launcher"
    exit 1
}

# Icone fallback: usa pythonw.exe se nao tiver .ico
if (-not (Test-Path $iconPath)) {
    $iconPath = "C:\Python313\pythonw.exe,0"
}

function New-Shortcut {
    param(
        [string]$Path,
        [string]$Target,
        [string]$Icon,
        [string]$Description,
        [string]$WorkingDirectory
    )
    $wsh = New-Object -ComObject WScript.Shell
    $lnk = $wsh.CreateShortcut($Path)
    $lnk.TargetPath       = $Target
    $lnk.IconLocation     = $Icon
    $lnk.Description      = $Description
    $lnk.WorkingDirectory = $WorkingDirectory
    $lnk.Save()
    Write-Host "OK: $Path"
}

$desktop   = [Environment]::GetFolderPath('Desktop')
$startMenu = [Environment]::GetFolderPath('Programs')  # %AppData%\Microsoft\Windows\Start Menu\Programs

if (-not (Test-Path $startMenu)) {
    New-Item -ItemType Directory -Path $startMenu -Force | Out-Null
}

$desktopLnk = Join-Path $desktop   "Laguna Translator.lnk"
$startLnk   = Join-Path $startMenu "Laguna Translator.lnk"

New-Shortcut -Path $desktopLnk -Target $launcher -Icon $iconPath -Description "Laguna Translator - local voice translator / tradutor de voz local" -WorkingDirectory $here
New-Shortcut -Path $startLnk   -Target $launcher -Icon $iconPath -Description "Laguna Translator - local voice translator / tradutor de voz local" -WorkingDirectory $here

Write-Host ""
Write-Host "Shortcuts created / Atalhos criados:"
Write-Host "  Desktop:     $desktopLnk"
Write-Host "  Start Menu:  $startLnk"
Write-Host ""
Write-Host "Tip / Dica: click the shortcut and the app opens at http://127.0.0.1:7531 in your browser / clique no atalho e o app abre em http://127.0.0.1:7531 no seu navegador."
