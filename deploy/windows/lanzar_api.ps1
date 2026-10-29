# SCANIA V4 - Lanza la API/demo (demo_api.py, :8091) sin consola. Lo invoca SCANIA-api.vbs
# desde la carpeta de Inicio de Windows al iniciar sesion.
#   * IDEMPOTENTE: si algo ya escucha en el puerto, no arranca otra instancia.
#   * Usa SIEMPRE el pythonw del .venv del repo (nunca el `python` del PATH, que es el
#     alias de WindowsApps y acabaria en otro interprete sin las dependencias).
#   * cwd = raiz del repo: demo_api.py importa ocr_pipeline/facturas_json y escribe demo_jobs\.
#
#   * -Redeploy: reinicio sin cortar trabajos. Primero comprueba que el codigo nuevo y api_keys.json
#     cargan (importa demo_api en un proceso aparte: solo lee); si no, sale SIN tocar la API en marcha
#     (un api_keys.json roto la dejaria caida para TODOS los clientes). Luego crea .no_aceptar
#     (POST /api/jobs responde 503 y WTCP reintenta mas tarde), espera a que no haya trabajos en curso
#     ni en cola, para el proceso que escucha, arranca el nuevo, espera /api/health y borra .no_aceptar.
#   * -Comprobar: solo esa comprobacion (p. ej. tras editar api_keys.json para un cliente nuevo).
#
# Uso:  powershell -NoProfile -ExecutionPolicy Bypass -File deploy\windows\lanzar_api.ps1 [-Force | -Redeploy | -Comprobar]

param([switch]$Force, [switch]$Redeploy, [switch]$Comprobar)

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

if ($Redeploy -or $Comprobar) {
  $Python = Join-Path $Root '.venv\Scripts\python.exe'
  $env:PYTHONIOENCODING = 'utf-8'
  $prev = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  Push-Location $Root
  $salida = & $Python -c "import demo_api; demo_api.comprobar()" 2>&1
  $codigo = $LASTEXITCODE
  Pop-Location
  $ErrorActionPreference = $prev
  if ($codigo -ne 0) {
    Write-Output (($salida | ForEach-Object { "$_" }) -join "`n")
    Write-Output "Comprobacion FALLIDA (codigo nuevo o api_keys.json): la API en marcha NO se toca."
    exit 1
  }
  Write-Output (($salida | Where-Object { $_ -is [string] }) -join "`n")
  if ($Comprobar) { exit 0 }
}

$Drain = Join-Path $Root '.no_aceptar'
if ($Redeploy) {
  New-Item -ItemType File -Path $Drain -Force | Out-Null
  Write-Output "Drenaje: no se aceptan subidas nuevas; esperando a que terminen los trabajos..."
  try {
    while ($true) {
      $h = $null
      try { $h = Invoke-RestMethod "http://127.0.0.1:$Puerto/api/health" -TimeoutSec 10 } catch { }
      if (-not $h -or ($h.jobs_running -eq 0 -and $h.jobs_queued -eq 0)) { break }
      Start-Sleep 2
    }
    $c = $null
    try { $c = Get-NetTCPConnection -LocalPort $Puerto -State Listen -ErrorAction Stop } catch { }
    if ($c) {
      Stop-Process -Id $c.OwningProcess -Force -Confirm:$false
      for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep 1
        $c = $null
        try { $c = Get-NetTCPConnection -LocalPort $Puerto -State Listen -ErrorAction Stop } catch { }
        if (-not $c) { break }
      }
    }
    & $PSCommandPath -Force
    $ok = $false
    for ($i = 0; $i -lt 60; $i++) {
      Start-Sleep 2
      try { $null = Invoke-RestMethod "http://127.0.0.1:$Puerto/api/health" -TimeoutSec 10; $ok = $true; break } catch { }
    }
    $p = (Get-NetTCPConnection -LocalPort $Puerto -State Listen -ErrorAction SilentlyContinue).OwningProcess
    Write-Output ("Redeploy: " + $(if ($ok) { "API nueva responde (PID $p)" } else { "la API nueva NO responde: ver logs\api.err.log" }))
  } finally {
    Remove-Item $Drain -Force -ErrorAction SilentlyContinue
  }
  exit 0
}

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
