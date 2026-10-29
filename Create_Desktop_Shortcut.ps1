$ErrorActionPreference = 'Stop'
$here   = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $here 'air_mouse.py'

# 1. Prefer pythonw.exe inside the local .venv created by setup.py
$pyw = $null
$venvPyw = Join-Path $here '.venv\Scripts\pythonw.exe'
if (Test-Path $venvPyw) {
    $pyw = $venvPyw
}

# 2. Fallback to system Python if .venv is not present
if (-not $pyw) {
    try {
        $exe = & py -3 -c "import sys; print(sys.executable)" 2>$null
        if ($exe) {
            $cand = Join-Path (Split-Path $exe.Trim()) 'pythonw.exe'
            if (Test-Path $cand) { $pyw = $cand }
        }
    } catch {}
}
if (-not $pyw) {
    $cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($cmd) { $pyw = $cmd.Source }
}
if (-not $pyw) {
    Write-Host 'Python not found. Run "python setup.py" first, or install Python 3 from python.org.' -ForegroundColor Red
    exit 1
}

$desktop = [Environment]::GetFolderPath('Desktop')
$lnk     = Join-Path $desktop 'AirGesture Pro.lnk'
$ws      = New-Object -ComObject WScript.Shell
$sc      = $ws.CreateShortcut($lnk)
$sc.TargetPath       = $pyw
$sc.Arguments        = '"' + $script + '"'
$sc.WorkingDirectory = $here
$sc.Description      = 'AirGesture Pro - hand mouse'
$icon = Join-Path $here 'airgesture.ico'
if (Test-Path $icon) { $sc.IconLocation = $icon }
$sc.Save()

Write-Host "Done. Shortcut made: $lnk (using $pyw)" -ForegroundColor Green
