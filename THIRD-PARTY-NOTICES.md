# Third-party notices

QuickCode is Python, installed via pip/uv (or the Windows installer, which
creates a private venv and pip-installs into it) rather than frozen into a
single binary. The table below lists QuickCode's direct dependencies as
declared in `pyproject.toml`, with the license each one actually publishes
in its package metadata — read from the installed virtual environment, not
guessed. Full license text for each project is available from its
repository or PyPI page linked below.

## Direct dependencies

| Package | License | Homepage |
|---|---|---|
| [fastapi](https://pypi.org/project/fastapi/) | MIT | https://github.com/fastapi/fastapi |
| [uvicorn](https://pypi.org/project/uvicorn/) | BSD-3-Clause | https://github.com/encode/uvicorn |
| [websockets](https://pypi.org/project/websockets/) | BSD-3-Clause | https://github.com/python-websockets/websockets |
| [openai](https://pypi.org/project/openai/) | Apache-2.0 | https://github.com/openai/openai-python |
| [pydantic](https://pypi.org/project/pydantic/) | MIT | https://github.com/pydantic/pydantic |
| [httpx](https://pypi.org/project/httpx/) | BSD-3-Clause | https://github.com/encode/httpx |
| [pywebview](https://pypi.org/project/pywebview/) | BSD-3-Clause | https://github.com/r0x0r/pywebview |
| [pywinpty](https://pypi.org/project/pywinpty/) (optional `pty` extra, Windows only) | MIT | https://github.com/andfoy/pywinpty |

`pywebview` and `pywinpty` versions are pinned to a minimum in
`pyproject.toml`; the rest float on a floor version. Run
`.venv\Scripts\python.exe -m pip show <package>` (or
`importlib.metadata.version("<package>")`) against your installed
environment for the exact resolved version and its bundled `LICENSE` file.

## pywebview (native app window)

QuickCode's native window (`quickcode/ui/window.py`) is pywebview's
`webview.create_window` / `webview.start`, wrapping the local FastAPI server
in a real OS window — WebView2 on Windows, which ships with the OS/Edge, so
nothing extra is installed. pywebview is copyright (c) 2014-2017 Roman
Sirokov and contributors, released under the BSD 3-Clause License.

## pywinpty / winpty / ConPTY (optional PTY backend)

The optional `pty` extra pulls in `pywinpty`, which bundles prebuilt native
helper binaries alongside its Python extension module: `OpenConsole.exe`,
`winpty-agent.exe`, `conpty.dll`, and `winpty.dll`. These land on disk
wherever `pywinpty` is installed (the venv the installer creates, or your
own `.venv`) — QuickCode does not vendor or modify them itself.

- **pywinpty** (`_winpty` extension, Python bindings): copyright the
  Spyder project contributors, MIT License.
  https://github.com/andfoy/pywinpty
- **winpty** (`winpty.dll`, the legacy pre-ConPTY backend): copyright (c)
  2011-2016 Ryan Prichard, MIT License. https://github.com/rprichard/winpty
- **Windows Console / ConPTY host** (`OpenConsole.exe`, `conpty.dll`): built
  from Microsoft's Windows Terminal / Console repository, copyright (c)
  Microsoft Corporation, MIT License.
  https://github.com/microsoft/terminal

MIT License text (applies to all three above):

> Permission is hereby granted, free of charge, to any person obtaining a
> copy of this software and associated documentation files (the
> "Software"), to deal in the Software without restriction, including
> without limitation the rights to use, copy, modify, merge, publish,
> distribute, sublicense, and/or sell copies of the Software, and to permit
> persons to whom the Software is furnished to do so, subject to the
> following conditions:
>
> The above copyright notice and this permission notice shall be included
> in all copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
> IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
> FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
> THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
> LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
> FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
> DEALINGS IN THE SOFTWARE.
