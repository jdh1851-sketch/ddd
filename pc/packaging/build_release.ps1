$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
$version = '0.10.0'
python packaging/prepare_assets.py
if ($LASTEXITCODE) { throw 'Asset preparation failed' }
$ocr = Join-Path $env:ProgramFiles 'Tesseract-OCR'
if (!(Test-Path "$ocr\tesseract.exe")) { throw 'Build machine needs Tesseract-OCR. Run choco install tesseract -y.' }
# Collect the entire OCR runtime, including dependent DLLs, into the portable app.
python -m PyInstaller --noconfirm --clean --onedir --windowed --name Kkullim --icon app_icon.ico --add-data 'app_icon.ico;.' --add-data '..\assets\templates;templates' --add-data 'tessdata;tessdata' --add-data "$ocr;ocr" kkullim_pc.py
if ($LASTEXITCODE) { throw 'PyInstaller failed' }
$process = Start-Process -FilePath '.\dist\Kkullim\Kkullim.exe' -ArgumentList '--selftest' -Wait -PassThru
if ($process.ExitCode -ne 0 -or !(Test-Path 'dist\Kkullim\selftest.txt')) { throw 'Packaged application selftest failed' }
Remove-Item 'dist\Kkullim\selftest.txt'
Copy-Item 'packaging\START_HERE.txt' 'dist\Kkullim\START_HERE.txt'
Copy-Item '낚시_사용설명서.md' 'dist\Kkullim\낚시_사용설명서.md'
New-Item -ItemType Directory -Force release | Out-Null
Compress-Archive -Path 'dist\Kkullim' -DestinationPath "release\Kkullim-PC-$version-Windows-x64.zip" -Force
$iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
if (!(Test-Path $iscc)) { throw 'Inno Setup 6 is required on the build machine' }
& $iscc 'packaging\installer.iss'
if ($LASTEXITCODE) { throw 'Installer compilation failed' }
Get-ChildItem release -File | Where-Object {$_.Extension -in '.zip','.exe'} | ForEach-Object {
    $hash = (Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    "$hash  $($_.Name)"
} | Set-Content 'release\SHA256SUMS.txt' -Encoding ascii
