$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
$version = '0.10.0'
python packaging/prepare_assets.py
if ($LASTEXITCODE) { throw 'Asset preparation failed' }
$ocr = Join-Path $env:ProgramFiles 'Tesseract-OCR'
if (!(Test-Path "$ocr\tesseract.exe")) { throw 'Build machine needs Tesseract-OCR. Run choco install tesseract -y.' }
# One-file distribution: Python, templates and OCR are extracted privately at runtime.
python -m PyInstaller --noconfirm --clean --onefile --windowed --name Kkullim --icon app_icon.ico --add-data 'app_icon.ico;.' --add-data '..\assets\templates;templates' --add-data 'tessdata;tessdata' --add-data "$ocr;ocr" kkullim_pc.py
if ($LASTEXITCODE) { throw 'PyInstaller failed' }
# Test a copy in an otherwise empty folder. No _internal or installed OCR may be required.
$tempRoot = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { $env:TEMP }
$smoke = Join-Path $tempRoot ('kkullim-single-exe-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force $smoke | Out-Null
Copy-Item 'dist\Kkullim.exe' "$smoke\Kkullim.exe"
$process = Start-Process -FilePath "$smoke\Kkullim.exe" -WorkingDirectory $smoke -ArgumentList '--selftest' -PassThru
if (!$process.WaitForExit(120000)) {
    Stop-Process -Id $process.Id -Force
    throw 'Single-file application selftest timed out'
}
$process.Refresh()
if ($process.ExitCode -ne 0 -or !(Test-Path "$smoke\selftest.txt")) { throw 'Single-file application selftest failed' }
Get-Content "$smoke\selftest.txt"
New-Item -ItemType Directory -Force ui-check | Out-Null
Copy-Item "$smoke\selftest-ui.json" ui-check
Copy-Item "$smoke\preview-*.png" ui-check
Get-Content "$smoke\selftest-ui.json"
Remove-Item -Recurse -Force $smoke
if (Test-Path release) { Remove-Item -Recurse -Force release }
New-Item -ItemType Directory -Force release | Out-Null
Copy-Item 'dist\Kkullim.exe' "release\Kkullim-PC-$version.exe"
Get-ChildItem release -Filter '*.exe' | ForEach-Object {
    $hash = (Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    "$hash  $($_.Name)"
} | Set-Content 'release\SHA256SUMS.txt' -Encoding ascii
