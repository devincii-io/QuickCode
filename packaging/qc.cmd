@echo off
rem `qc` — the short alias for the QuickCode CLI, installed beside quickcode.exe
rem and on the same PATH entry. A .cmd rather than a third PyInstaller target:
rem another executable would be another 10 MB of embedded Python runtime for a
rem two-letter spelling of the one next to it.
"%~dp0quickcode.exe" %*
