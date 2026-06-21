# =====================================================================
#  Cria atalho "Tradutor PT-ES" na area de trabalho do usuario.
#  Roda sem precisar de admin.
#
#  Uso:
#    powershell -ExecutionPolicy Bypass -File install_shortcut.ps1
# =====================================================================

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$appScript = Join-Path $scriptDir "tradutor_app.py"
$iconPath  = Join-Path $scriptDir "tradutor.ico"
$desktop   = [Environment]::GetFolderPath("Desktop")
$lnkPath   = Join-Path $desktop "Tradutor PT-ES.lnk"

if (-not (Test-Path $appScript)) {
    Write-Host "ERRO: tradutor_app.py nao encontrado em $scriptDir" -ForegroundColor Red
    exit 1
}

if (-not (Test-Path $iconPath)) {
    Write-Host "INFO: tradutor.ico nao existe, gerando..." -ForegroundColor Yellow
    & python (Join-Path $scriptDir "make_icon.py")
    if (-not (Test-Path $iconPath)) {
        Write-Host "ERRO: Falha gerando icone" -ForegroundColor Red
        exit 1
    }
}

# Acha pythonw.exe (sem console preto). Cai pra python.exe se nao houver.
$pythonw = $null
$py = Get-Command python -ErrorAction SilentlyContinue
if ($py) {
    $pythonDir = Split-Path -Parent $py.Source
    $pwCandidate = Join-Path $pythonDir "pythonw.exe"
    if (Test-Path $pwCandidate) {
        $pythonw = $pwCandidate
    } else {
        $pythonw = $py.Source
        Write-Host "INFO: pythonw.exe nao encontrado, usando python.exe" -ForegroundColor Yellow
    }
} else {
    Write-Host "ERRO: python nao esta no PATH" -ForegroundColor Red
    exit 1
}

Write-Host "Criando atalho:"
Write-Host "  Target:    $pythonw"
Write-Host "  Arguments: $appScript"
Write-Host "  Icon:      $iconPath"
Write-Host "  Destino:   $lnkPath"

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($lnkPath)
$shortcut.TargetPath = $pythonw
$shortcut.Arguments = '"' + $appScript + '"'
$shortcut.WorkingDirectory = $scriptDir
$shortcut.IconLocation = $iconPath + ",0"
$shortcut.Description = "Traducao simultanea PT - ES (servidor local)"
$shortcut.WindowStyle = 1
$shortcut.Save()

Write-Host ""
Write-Host "OK: Atalho criado em $lnkPath" -ForegroundColor Green
Write-Host ""
Write-Host "Duplo clique no atalho pra abrir."
Write-Host "Servidor sobe em ~10-20s na primeira vez (carrega modelo GPU)."
