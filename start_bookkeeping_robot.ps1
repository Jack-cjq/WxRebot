param(
    [switch]$InstallDeps
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$Python = Get-Command python -ErrorAction SilentlyContinue
if (-not $Python) {
    Write-Host "Python was not found. Please install Python 3.9+ and add python to PATH." -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}

if ($InstallDeps) {
    Write-Host "Installing dependencies..." -ForegroundColor Cyan
    & python -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Dependency installation failed. Please check your network and Python environment." -ForegroundColor Red
        Read-Host "Press Enter to exit"
        exit $LASTEXITCODE
    }
}

if (-not $InstallDeps) {
    $PreviousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & python -c "import PySide6, win32api, comtypes, pyperclip" *> $null
    $DependencyCheckCode = $LASTEXITCODE
    $ErrorActionPreference = $PreviousErrorActionPreference

    if ($DependencyCheckCode -ne 0) {
        Write-Host "Some dependencies are missing. Installing dependencies now..." -ForegroundColor Yellow
        & python -m pip install -r requirements.txt
        if ($LASTEXITCODE -ne 0) {
            Write-Host "Dependency installation failed. Please check your network and Python environment." -ForegroundColor Red
            Write-Host "You can also run this manually:"
            Write-Host "python -m pip install -r requirements.txt"
            Read-Host "Press Enter to exit"
            exit $LASTEXITCODE
        }
    }
}

$RunDir = Join-Path $Root ".run"
New-Item -ItemType Directory -Force -Path $RunDir | Out-Null

$LogDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

Write-Host "Starting WeChat bookkeeping robot..." -ForegroundColor Green
Write-Host "Project root: $Root"
Write-Host "The control panel and listener bot will start together."
Write-Host "Closing this window will try to stop the robot."
Write-Host ""

try {
    & python "examples\messaging\start_bookkeeping_robot.py"
    $code = $LASTEXITCODE
} catch {
    Write-Host "Start failed: $($_.Exception.Message)" -ForegroundColor Red
    $code = 1
}

if ($code -ne 0) {
    Write-Host ""
    Write-Host "Startup script exited with code: $code" -ForegroundColor Red
    Write-Host "First run command:"
    Write-Host "powershell -ExecutionPolicy Bypass -File .\start_bookkeeping_robot.ps1 -InstallDeps"
    Read-Host "Press Enter to exit"
}

exit $code
