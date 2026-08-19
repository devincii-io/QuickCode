"""Frozen entry point for ``QuickCode.exe`` — the windowed app.

PyInstaller needs a *script* per executable, and ``quickcode/cli.py``'s own
``__main__`` guard runs ``main()`` (the CLI). Pointing the spec at that file
would build the console entry twice and give the Start-Menu shortcut the wrong
one, so the two entry points get one three-line shim each instead of a flag
buried in argv.
"""

from quickcode.cli import main_app

if __name__ == "__main__":
    main_app()
