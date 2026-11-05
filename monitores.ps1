# Levanta el panel local y, si está configurado en monitores.config.ps1, otro remoto por túnel SSH.
#   .\monitores.cmd            levanta lo que falte y abre las dos pestañas
#   .\monitores.cmd -SinAbrir  igual, sin abrir el navegador
#   .\monitores.cmd -Parar     para el panel local, el túnel y el panel remoto
# Se puede repetir sin riesgo: lo que ya está en marcha no se toca.
param([switch]$SinAbrir, [switch]$Parar)

$RemoteHost = ""                # Destino SSH del panel remoto. Vacío: solo el panel local.
$RemoteName = "panel remoto"
$Tailnet = ""                   # Red de Tailscale que debe estar activa. Vacío: no se comprueba.
$LocalPort = 8765
$TunnelPort = 9000
$RemotePort = 8765
$Tailscale = "C:\Program Files\Tailscale\tailscale.exe"
$Forward = "${TunnelPort}:127.0.0.1:${RemotePort}"
$Config = Join-Path $PSScriptRoot "monitores.config.ps1"
if (Test-Path $Config) { . $Config }

function Test-Listening($port) {
    return [bool](Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
}
function Test-PanelReady($port) {
    try { return [bool](Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/state" -TimeoutSec 4).scan.ready } catch { return $false }
}
function Wait-Panel($port, $seconds) {
    $limit = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $limit) {
        if (Test-PanelReady $port) { return $true }
        Start-Sleep -Seconds 1
    }
    return $false
}
function Get-Tunnel {
    return Get-CimInstance Win32_Process -Filter "Name='ssh.exe'" | Where-Object { $_.CommandLine -like "*$Forward*" }
}
function Test-Tailnet {
    if (-not $Tailnet) { return $true }
    # Solo comprueba: cambiar de red cortaría el acceso a las máquinas de la otra.
    if (-not (Test-Path $Tailscale)) { return $false }
    return [bool](& $Tailscale switch --list 2>$null | Where-Object { $_ -match '\*\s*$' -and $_ -match $Tailnet })
}

if ($Parar) {
    Get-Tunnel | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Write-Output "Túnel: parado."
    $owner = (Get-NetTCPConnection -LocalPort $LocalPort -State Listen -ErrorAction SilentlyContinue).OwningProcess | Select-Object -First 1
    if ($owner) {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId=$owner"
        if ($process.CommandLine -like "*monitor.py*") { Stop-Process -Id $owner -Force; Write-Output "Panel local: parado." }
        else { Write-Output "Panel local: el puerto $LocalPort lo usa otro programa; no lo toco." }
    } else { Write-Output "Panel local: no estaba en marcha." }
    if (-not $RemoteHost) { exit 0 }
    if (Test-Tailnet) {
        & ssh -o ConnectTimeout=15 $RemoteHost "systemctl --user stop claude-monitor.service"
        if ($LASTEXITCODE -eq 0) { Write-Output "Panel de ${RemoteName}: parado." } else { Write-Output "Panel de ${RemoteName}: no se ha podido parar." }
    } else { Write-Output "Panel de ${RemoteName}: sigue en marcha; este PC no está en la red $Tailnet." }
    exit 0
}

$failed = $false

# Panel local
if (Test-Listening $LocalPort) {
    Write-Output "Panel local: ya estaba en marcha."
} else {
    Start-Process -FilePath "python" -ArgumentList "monitor.py" -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
    Write-Output "Panel local: arrancando..."
}
if (Wait-Panel $LocalPort 90) { Write-Output "Panel local: listo en http://localhost:$LocalPort/" }
else { Write-Output "Panel local: NO responde. Prueba 'python monitor.py' para ver el error."; $failed = $true }

# Panel remoto
$remoteReady = $false
if (-not $RemoteHost) {
    Write-Output "Sin panel remoto configurado (monitores.config.ps1)."
} elseif (-not (Test-Tailnet)) {
    Write-Output "${RemoteName}: este PC no está en la red de Tailscale '$Tailnet'."
    Write-Output "  Cámbiala y repite esta orden. Solo ha quedado levantado el panel local."
    $failed = $true
} else {
    # En primer plano: si Tailscale pide aprobación, su enlace se ve aquí.
    & ssh -o ConnectTimeout=15 $RemoteHost "systemctl --user start claude-monitor.service"
    if ($LASTEXITCODE -ne 0) {
        Write-Output "${RemoteName}: no se ha podido arrancar su panel (¿máquina apagada?)."
        $failed = $true
    } else {
        if (-not (Get-Tunnel)) {
            if (Test-Listening $TunnelPort) {
                Write-Output "Túnel: el puerto $TunnelPort lo usa otro programa."
                $failed = $true
            } else {
                $tunnelArgs = @("-N", "-L", $Forward, "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3", $RemoteHost)
                Start-Process -FilePath "ssh" -ArgumentList $tunnelArgs -WindowStyle Hidden
            }
        }
        if (-not $failed) {
            if (Wait-Panel $TunnelPort 60) { Write-Output "${RemoteName}: listo en http://localhost:$TunnelPort/"; $remoteReady = $true }
            else { Write-Output "${RemoteName}: el túnel no responde."; $failed = $true }
        }
    }
}

if (-not $SinAbrir) {
    if (Test-PanelReady $LocalPort) { Start-Process "http://localhost:$LocalPort/" }
    if ($remoteReady) { Start-Process "http://localhost:$TunnelPort/" }
}
if ($failed) { exit 1 }
