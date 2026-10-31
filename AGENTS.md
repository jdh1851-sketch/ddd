# Project delivery requirements

The user requires Windows PC changes to be delivered as usable executable distributions, not source-only archives or build instructions.

For PC releases:
- Build on Windows and provide both a portable ZIP containing Kkullim.exe and an installer EXE.
- Bundle Python, the Tesseract executable and its dependent DLLs, and Korean OCR data. End users must not need Python or Tesseract installation.
- Run the unit tests and packaged application selftest before publishing. The selftest must initialize both UI tabs and the bundled OCR runtime without sending game input.
- Publish versioned GitHub Release assets, include SHA256SUMS.txt, and give the user verified download links. A cloud-local /workspace path is not a download solution.
- Use the Windows executable release workflow and pc/packaging/build_release.ps1. Keep the source available, but do not present it as the runnable app.
- Distinguish packaging/UI validation from real game input validation. Never claim the latter without running it.
- After implementation, continue monitoring and fixing the Windows build until a downloadable executable is available, unless a concrete external blocker prevents it.
