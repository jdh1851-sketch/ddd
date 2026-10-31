# Windows single-file distribution

Preserve the original delivery format: download `Kkullim-PC-0.10.0.exe` and double-click it. This is a portable app, not a setup program. It bundles Python, Tesseract, dependent DLLs, templates and Korean OCR data. Users do not need an installer, an adjacent `_internal` folder, Python or Tesseract. Settings and logs are created next to the EXE as before.

The `Windows executable release` workflow runs the unit tests on Windows and builds with PyInstaller `--onefile`. It copies only the finished EXE into an empty directory and initializes both UI tabs and the embedded OCR runtime without game input. Only after that test passes does it publish the EXE and SHA256SUMS.txt to a versioned GitHub Release.

The workflow runs on PC changes to `main` or `codex/fishing-windows-release`; manual dispatch is available after it is merged into the default branch. Developer builds use `pc/build_windows.bat` and require Windows x64, Python 3.12 and Tesseract on the build machine. These requirements do not apply to end users.

For a new version update `$version` in build_release.ps1, the release tag/title in windows-release.yml and the app title together. Actual game input requires separate validation. Third-party OCR notices remain embedded alongside the runtime and Korean model.
