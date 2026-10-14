$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$taskPython = $null
$taskPythonArgs = @()
if (Test-Path -LiteralPath '.venv\Scripts\python.exe') {
    $taskPython = (Join-Path $PSScriptRoot '.venv\Scripts\python.exe')
} else {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($taskVersion in @('-3.12', '-3.11', '-3.10')) {
            $ErrorActionPreference = 'Continue'
            & py $taskVersion -c 'import sys; assert (3,10) <= sys.version_info[:2] <= (3,12)' 2>$null
            $taskProbeExit = $LASTEXITCODE
            $ErrorActionPreference = 'Stop'
            if ($taskProbeExit -eq 0) {
                $taskPython = 'py'
                $taskPythonArgs = @($taskVersion)
                break
            }
        }
    }
    if (-not $taskPython -and (Get-Command python -ErrorAction SilentlyContinue)) {
        $ErrorActionPreference = 'Continue'
        & python -c 'import sys; assert (3,10) <= sys.version_info[:2] <= (3,12)' 2>$null
        $taskProbeExit = $LASTEXITCODE
        $ErrorActionPreference = 'Stop'
        if ($taskProbeExit -eq 0) { $taskPython = 'python' }
    }
    if (-not $taskPython) {
        throw 'Please install Python 3.12 (64-bit) with pip and Tcl/Tk, then run this installer again. https://www.python.org/downloads/'
    }
    & $taskPython @taskPythonArgs -m venv '.venv'
    if ($LASTEXITCODE -ne 0) { throw 'Failed to create the local Python environment.' }
    $taskPython = (Join-Path $PSScriptRoot '.venv\Scripts\python.exe')
}
& $taskPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw 'Failed to update pip. Check the Internet connection.' }
# Legacy OpenCV and headless OpenCV share the same cv2 files. Remove both
# when upgrading from the GUI package, then install one consistent version.
$taskHadLegacyOpenCV = (& $taskPython -c 'import importlib.metadata as m; print("yes" if any(d.metadata.get("Name", "").lower().replace("_", "-") == "opencv-python" for d in m.distributions()) else "no")').Trim()
if ($LASTEXITCODE -ne 0) { throw 'Failed to check the existing OpenCV installation.' }
& $taskPython -m pip uninstall --yes rapidocr-onnxruntime
if ($LASTEXITCODE -ne 0) { throw 'Failed to remove the previous OCR runtime.' }
if ($taskHadLegacyOpenCV -eq 'yes') {
    & $taskPython -m pip uninstall --yes opencv-python opencv-python-headless
    if ($LASTEXITCODE -ne 0) { throw 'Failed to replace the previous OpenCV installation.' }
}
& $taskPython -m pip install -r 'requirements.txt'
if ($LASTEXITCODE -ne 0) { throw 'Failed to install dependencies. Check the Internet connection and retry.' }
& $taskPython 'fetch_models.py'
if ($LASTEXITCODE -ne 0) { throw 'Failed to download and verify the official OCR models.' }
& $taskPython -c 'import tkinter, rapidocr, pyautogui, mss, docx; from ocr_backend import build_ocr_backend; build_ocr_backend(); print("Installation completed. Local OCR models are ready. Run the launcher to open the app.")'
if ($LASTEXITCODE -ne 0) { throw 'Dependency check failed.' }
