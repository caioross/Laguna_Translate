# Laguna Translator — remove atalhos Desktop + Start Menu.
$desktop   = [Environment]::GetFolderPath('Desktop')
$startMenu = [Environment]::GetFolderPath('Programs')

foreach ($p in @(
    (Join-Path $desktop   "Laguna Translator.lnk"),
    (Join-Path $startMenu "Laguna Translator.lnk")
)) {
    if (Test-Path $p) {
        Remove-Item $p -Force
        Write-Host "Removido: $p"
    }
}
Write-Host "Atalhos removidos."
