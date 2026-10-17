# SCANIA V4 - Lanza la API/demo (demo_api.py, :8091) sin consola. Lo invoca SCANIA-api.vbs
# desde la carpeta de Inicio de Windows al iniciar sesion.
#   * IDEMPOTENTE: si algo ya escucha en el puerto, no arranca otra instancia.
#   * Usa SIEMPRE el pythonw del .venv del repo (nunca el `python` del PATH, que es el
#     alias de WindowsApps y acabaria en otro interprete sin las dependencias).
#   * cwd = raiz del repo: demo_api.py importa ocr_pipeline/facturas_json y escribe demo_jobs\.
#
# Uso:  powershell -NoProfile -ExecutionPolicy Bypass -File deploy\windows\lanzar_api.ps1 [-Force]

param([switch]$Force)

$ErrorActionPreference = 'Stop'

$Root    = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Pythonw = Join-Path $Root '.venv\Scripts\pythonw.exe'
$Puerto  = if ($env:DEMO_PORT) { [int]$env:DEMO_PORT } else { 8091 }
$LogDir  = Join-Path $Root 'logs'

if (-not (Test-Path $Pythonw)) {
  Write-Error "No existe el interprete del .venv: $Pythonw -- ver INSTALL.md."
  exit 2
}
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }

$escucha = $null
try { $escucha = Get-NetTCPConnection -LocalPort $Puerto -State Listen -ErrorAction Stop } catch { }
if ($escucha -and -not $Force) {
  Write-Output "API :$Puerto ya escucha -> no se arranca otra (idempotente)."
  exit 0
}

# Host de los llama-server (ver CLAUDE.md). Respeta el valor si ya viene del entorno.
if (-not $env:OCR_MODELS_HOST) { $env:OCR_MODELS_HOST = '192.168.1.194' }
$env:PYTHONIOENCODING = 'utf-8'

# Bajo pythonw stdout/stderr no existen: se redirigen a ficheros (distintos: Start-Process
# falla si ambos apuntan al mismo).
Start-Process -FilePath $Pythonw -ArgumentList 'demo_api.py' -WorkingDirectory $Root -WindowStyle Hidden `
  -RedirectStandardOutput (Join-Path $LogDir 'api.out.log') -RedirectStandardError (Join-Path $LogDir 'api.err.log')
Write-Output "API arrancada windowless (.venv pythonw) en :$Puerto; logs en logs\api.*.log."
