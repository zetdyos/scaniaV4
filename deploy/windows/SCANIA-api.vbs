' SCANIA V4 - Arranca la API/demo de facturas (demo_api.py, :8091) al iniciar sesion, SIN consola.
' Delega en lanzar_api.ps1, que es IDEMPOTENTE (no arranca otra si :8091 ya escucha).
'
' DESPLIEGUE: copiar a la carpeta de Inicio (Win+R -> shell:startup)
'   %APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\SCANIA-api.vbs
' PARADA: Get-NetTCPConnection -LocalPort 8091 -State Listen | % { Stop-Process -Id $_.OwningProcess }
'         y sacar esta VBS de Inicio para que no vuelva al iniciar sesion.
Set sh = CreateObject("WScript.Shell")
sh.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""C:\Users\LERIT\scaniaV4\deploy\windows\lanzar_api.ps1""", 0, False
