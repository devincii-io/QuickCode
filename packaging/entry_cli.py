"""Frozen entry point for ``quickcode.exe`` — the console CLI.

The counterpart to ``entry_app.py``: same package, same process shape as the
``quickcode``/``qc`` console scripts a pip install puts on PATH, so
``quickcode -p``, ``quickcode doctor`` and ``--version`` behave identically
whether QuickCode was installed frozen or as a wheel.
"""

from quickcode.cli import main

if __name__ == "__main__":
    main()
