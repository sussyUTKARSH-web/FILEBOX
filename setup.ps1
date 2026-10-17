$ErrorActionPreference = 'Stop'
$Project = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Project

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw 'Python 3 is required. Install Python and enable it on PATH, then rerun setup.ps1.'
}
if (-not (Get-Command gcc -ErrorAction SilentlyContinue)) {
    throw 'GCC is required to compile the C file-system backend. Install MinGW-w64 and add gcc to PATH.'
}

$PythonExe = $env:FILEBOX_PYTHON
if (-not $PythonExe) {
    foreach ($CommandName in @('py', 'python3', 'python')) {
        $Command = Get-Command $CommandName -ErrorAction SilentlyContinue
        if (-not $Command -or $Command.Source -match '\\WindowsApps\\') { continue }
        try {
            & $Command.Source --version *> $null
            if ($LASTEXITCODE -eq 0) { $PythonExe = $Command.Source; break }
        } catch { }
    }
}
if (-not $PythonExe) {
    $CodexPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
    if (Test-Path -LiteralPath $CodexPython) { $PythonExe = $CodexPython }
}
if (-not $PythonExe -or -not (Test-Path -LiteralPath $PythonExe)) {
    throw 'A usable Python 3 installation was not found. Install Python 3.10+ or set FILEBOX_PYTHON to the full path of python.exe.'
}

& $PythonExe --version
if ($LASTEXITCODE -ne 0) { throw "Python could not be run: $PythonExe" }

if (-not (Test-Path '.venv\Scripts\python.exe')) {
    & $PythonExe -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the virtual environment.' }
}
& '.venv\Scripts\python.exe' -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw 'Could not upgrade pip in .venv.' }
& '.venv\Scripts\python.exe' -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Could not install project dependencies in .venv.' }
gcc -O2 -o 'c_backend\filebox_os.exe' 'c_backend\filebox_os.c'
if ($LASTEXITCODE -ne 0) { throw 'Could not compile the C file-system backend.' }
Write-Host 'Setup complete. Configure .env and start MySQL before running run.ps1.'
