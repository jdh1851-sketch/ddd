# Windows executable distribution

End-user downloads are available under the repository's GitHub Releases. Distribute both `Kkullim-PC-0.10.0-Windows-x64.zip` (extract everything and run `Kkullim.exe`) and `Kkullim-PC-0.10.0-Setup-x64.exe` (installer). Both include Python, Tesseract and Korean OCR data. Do not distribute the app EXE without its `_internal` directory.

The `Windows executable release` GitHub Actions workflow builds and tests on Windows, initializes the packaged UI and OCR without game input, creates ZIP/installer assets and SHA256SUMS.txt, then publishes a versioned GitHub Release. It runs on PC changes to `main` or `codex/fishing-windows-release`, and supports manual dispatch once present on the default branch. Builds use unique `pc-v0.10.0-build.N` release tags to avoid overwriting downloads.

Developer builds require Windows x64, Python 3.12, Tesseract in Program Files and Inno Setup 6. `pc/build_windows.bat` runs tests and the same packaging script. Those developer requirements do not apply to end users.

For a new app version, update `AppVersion` in installer.iss, `$version` in build_release.ps1, the tag/title in windows-release.yml and the app window title together. Check portable startup, bundled OCR, and installation on Windows. Keep the separate game-input validation limitation visible until real-game testing is performed.

Third-party OCR notices are preserved by copying the Tesseract runtime directory and including the Korean model's Apache 2.0 LICENSE.
