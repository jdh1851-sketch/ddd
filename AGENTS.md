# Project delivery requirements

The user requires Windows PC changes to be delivered as usable executable distributions, not source-only archives or build instructions.

For PC releases:
- Build on Windows and provide a single portable EXE, matching the original app. Do not replace it with an installer or folder distribution unless explicitly requested.
- Bundle Python, the Tesseract executable and its dependent DLLs, and Korean OCR data. End users must not need Python or Tesseract installation.
- Run the unit tests and packaged application selftest before publishing. The selftest must copy only the EXE into an empty directory, then initialize both UI tabs and the bundled OCR runtime without sending game input. No adjacent _internal folder may be required.
- Publish versioned GitHub Release assets, include SHA256SUMS.txt, and give the user verified download links. A cloud-local /workspace path is not a download solution.
- Use the Windows executable release workflow and pc/packaging/build_release.ps1. Keep the source available, but do not present it as the runnable app.
- Distinguish packaging/UI validation from real game input validation. Never claim the latter without running it.
- After implementation, continue monitoring and fixing the Windows build until a downloadable executable is available, unless a concrete external blocker prevents it.
