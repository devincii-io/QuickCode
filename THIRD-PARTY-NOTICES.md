# Third-party notices

QuickCode itself is MIT licensed (see `LICENSE`). This file lists the
third-party software QuickCode depends on, so that a redistributor or a
corporate reviewer can see the whole set without resolving it themselves.

QuickCode ships as Python source: a wheel/sdist installed with pip or uv, or
the Windows installer, which creates a private virtual environment and
pip-installs into it. It is **not** frozen into a single binary, so the
dependencies below are fetched from PyPI at install time rather than
redistributed inside the QuickCode artifacts. The obligation to reproduce the
notices below therefore falls on anyone who *does* redistribute an installed
environment (an image, a bundled venv, an offline mirror).

## Provenance of this file

The tables are generated from `uv.lock` (the resolved dependency graph) plus
the license metadata of the actually-installed distributions — not from
guesswork and not from PyPI's free-text `license` field alone. The machine-
readable equivalent is `sbom.cdx.json` (CycloneDX 1.6) in the repository root;
`docs/COMPLIANCE.md` documents how to regenerate it and verify it byte for
byte.

Version numbers are the resolution recorded in `uv.lock` at the time of
writing. `pyproject.toml` declares floor versions only, so a fresh install
will resolve newer versions; re-generate the SBOM against your own environment
if you need the exact set you deployed.

**No dependency in any table below is under GPL, LGPL, AGPL, SSPL, BUSL, the
Elastic License, or any non-commercial or field-of-use restriction.** The only
copyleft present is file-level MPL-2.0, in `certifi` (Mozilla's CA bundle) and
in one component of `tqdm`. Neither is modified by QuickCode and neither
imposes any obligation on code that merely imports it.

## Runtime dependencies (transitive closure, Windows)

Resolved for `sys_platform == "win32"`, CPython 3.12, including the optional
`pty` extra. 31 distributions.

| Package | Version | License | Project |
|---|---|---|---|
| annotated-doc | 0.0.5 | MIT | https://github.com/fastapi/annotated-doc |
| annotated-types | 0.7.0 | MIT | https://github.com/annotated-types/annotated-types |
| anyio | 4.14.2 | MIT | https://github.com/agronholm/anyio |
| bottle | 0.13.4 | MIT | https://github.com/bottlepy/bottle |
| certifi | 2026.7.22 | **MPL-2.0** | https://github.com/certifi/python-certifi |
| cffi | 2.1.1 | MIT-0 | https://github.com/python-cffi/cffi |
| click | 8.4.2 | BSD-3-Clause | https://github.com/pallets/click |
| clr-loader | 0.3.1 | MIT (see note 1) | https://github.com/pythonnet/clr-loader |
| colorama | 0.4.6 | BSD-3-Clause | https://github.com/tartley/colorama |
| distro | 1.9.0 | Apache-2.0 | https://github.com/python-distro/distro |
| fastapi | 0.141.1 | MIT | https://github.com/fastapi/fastapi |
| h11 | 0.16.0 | MIT | https://github.com/python-hyper/h11 |
| httpcore | 1.0.9 | BSD-3-Clause | https://github.com/encode/httpcore |
| httpx | 0.28.1 | BSD-3-Clause | https://github.com/encode/httpx |
| idna | 3.18 | BSD-3-Clause | https://github.com/kjd/idna |
| jiter | 0.16.0 | MIT | https://github.com/pydantic/jiter |
| openai | 2.47.0 | Apache-2.0 | https://github.com/openai/openai-python |
| proxy-tools | 0.1.0 | MIT (see note 2) | https://github.com/jtushman/proxy_tools |
| pycparser | 3.0 | BSD-3-Clause | https://github.com/eliben/pycparser |
| pydantic | 2.13.4 | MIT | https://github.com/pydantic/pydantic |
| pydantic-core | 2.46.4 | MIT | https://github.com/pydantic/pydantic-core |
| pythonnet | 3.1.0 | MIT | https://github.com/pythonnet/pythonnet |
| pywebview | 6.2.1 | BSD-3-Clause | https://github.com/r0x0r/pywebview |
| pywinpty | 3.0.5 | MIT | https://github.com/spyder-ide/pywinpty |
| sniffio | 1.3.1 | MIT OR Apache-2.0 | https://github.com/python-trio/sniffio |
| starlette | 1.6.0 | BSD-3-Clause | https://github.com/Kludex/starlette |
| tqdm | 4.69.0 | **MPL-2.0 AND MIT** | https://github.com/tqdm/tqdm |
| typing-extensions | 4.16.0 | PSF-2.0 | https://github.com/python/typing_extensions |
| typing-inspection | 0.4.2 | MIT | https://github.com/pydantic/typing-inspection |
| uvicorn | 0.52.3 | BSD-3-Clause | https://github.com/Kludex/uvicorn |
| websockets | 17.0.1 | BSD-3-Clause | https://github.com/python-websockets/websockets |

Notes:

1. **clr-loader 0.3.1 declares no license in its package metadata** — no
   `License-Expression`, no `License` field, no license classifier. It does
   ship `LICENSE`, whose text is the MIT License, copyright (c) 2019-2026
   Benedikt Reinartz. The table records MIT on the strength of that file, not
   the metadata. Automated license scanners will report this package as
   "unknown"; that is a metadata defect upstream, not a licensing problem.
2. **proxy-tools 0.1.0 ships no license file at all.** Its metadata and PyPI
   page state `MIT`, and its repository is `jtushman/proxy_tools`, but the
   installed distribution contains no `LICENSE`. The package is a ~50-line
   lazy-property helper, last released in 2013, pulled in transitively by
   `pywebview`. Scanners that require a license file present will flag it.
3. `certifi` is MPL-2.0. It is the Mozilla CA root bundle plus a loader.
   QuickCode does not modify it, so MPL-2.0's source-disclosure trigger (which
   applies per modified file) is not engaged. Redistributing it requires
   passing the MPL-2.0 text along.
4. `tqdm`'s own code is MPL-2.0; a vendored component is MIT. QuickCode never
   calls it directly — it arrives via the `openai` SDK's progress bars.
5. `typing-extensions` is under the Python Software Foundation License 2.0,
   a permissive OSI-approved licence.

## Optional and platform-conditional dependencies

| Package | Version | License | When installed |
|---|---|---|---|
| pywinpty | 3.0.5 | MIT | Windows, `pty` extra (the interactive terminal tool) |
| pyobjc-core | 12.2.2 | MIT | macOS only, via pywebview |
| pyobjc-framework-Cocoa | 12.2.2 | MIT | macOS only, via pywebview |
| pyobjc-framework-Quartz | 12.2.2 | MIT | macOS only, via pywebview |
| pyobjc-framework-Security | 12.2.2 | MIT | macOS only, via pywebview |
| pyobjc-framework-UniformTypeIdentifiers | 12.2.2 | MIT | macOS only, via pywebview |
| pyobjc-framework-WebKit | 12.2.2 | MIT | macOS only, via pywebview |
| qtpy | 2.4.3 | MIT | pywebview's Qt backend marker (OpenBSD); not installed on Windows, macOS or Linux |
| packaging | 26.2 | Apache-2.0 OR BSD-2-Clause | pulled by qtpy under the same marker |

**Linux note.** QuickCode declares no Linux GUI backend. `pywebview` on Linux
requires a system-provided GTK/WebKitGTK stack (and `PyGObject`) that the user
installs separately. Those components are **LGPL-2.1-or-later**, not shipped by
QuickCode and not resolved by `pyproject.toml`. If you deploy QuickCode's
windowed mode on Linux, that stack is yours to license and account for.
QuickCode's `--no-browser` / browser-tab mode needs no GUI backend at all.

## Development-only dependencies

Installed by `uv sync --dev`; not present in a user installation and not
redistributed.

| Package | Version | License |
|---|---|---|
| pytest | 9.1.1 | MIT |
| pytest-asyncio | 1.4.0 | Apache-2.0 |
| ruff | 0.15.22 | MIT |
| iniconfig | 2.3.0 | MIT |
| pluggy | 1.6.0 | MIT |
| pygments | 2.20.0 | BSD-2-Clause |

## Bundled native binaries

Three Python packages ship prebuilt native code inside their wheels. These land
on disk wherever they are installed (the venv the Windows installer creates, or
your own). QuickCode does not vendor, patch or re-sign any of them.

### pywebview → Microsoft Edge WebView2 SDK

`pywebview` bundles the WebView2 SDK, version **1.0.3856.49**:

- `webview/lib/Microsoft.Web.WebView2.Core.dll`
- `webview/lib/Microsoft.Web.WebView2.WinForms.dll`
- `webview/lib/runtimes/{win-x64,win-x86,win-arm64}/native/WebView2Loader.dll`

Copyright (C) Microsoft Corporation. All rights reserved. Distributed under
Microsoft's BSD-3-Clause-style terms for the WebView2 SDK NuGet package. The
pywebview wheel ships these DLLs **without a copy of that licence**, so the
notice is reproduced here:

> Copyright (C) Microsoft Corporation. All rights reserved.
>
> Redistribution and use in source and binary forms, with or without
> modification, are permitted provided that the following conditions are met:
>
> * Redistributions of source code must retain the above copyright notice,
>   this list of conditions and the following disclaimer.
> * Redistributions in binary form must reproduce the above copyright notice,
>   this list of conditions and the following disclaimer in the documentation
>   and/or other materials provided with the distribution.
> * The name of Microsoft Corporation, or the names of its contributors may
>   not be used to endorse or promote products derived from this software
>   without specific prior written permission.
>
> THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
> AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
> IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
> ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE
> LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
> CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
> SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
> INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
> CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
> ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
> POSSIBILITY OF SUCH DAMAGE.

`webview/lib/WebBrowserInterop.{x64,x86}.dll` are pywebview's own interop
shims, covered by pywebview's BSD-3-Clause licence.
`webview/lib/pywebview-android.jar` is pywebview's Android backend and is never
loaded on desktop.

The **WebView2 Runtime** — the actual Chromium-based rendering engine the
window uses — is a separate Microsoft component that ships with Windows 11 and
with Microsoft Edge. QuickCode does not download, install or redistribute it,
and it is governed by Microsoft's own terms for that runtime.

### pythonnet → .NET Standard 2.0 reference assemblies

`pythonnet` (pulled in by pywebview to reach WinForms/WebView2) bundles
`Python.Runtime.dll` plus roughly 97 Microsoft reference assemblies under
`pythonnet/runtime/` (`netstandard.dll`, `System.*.dll`,
`Microsoft.Win32.Primitives.dll`, …), version-stamped "Microsoft .NET
Framework", copyright (c) Microsoft Corporation.

These are the `NETStandard.Library` 2.0.x reference assemblies, published by
Microsoft under the **MIT License** (`https://github.com/dotnet/standard`,
`LICENSE.TXT`). The pythonnet wheel ships them **without a licence or NOTICE
file**; the determination above is from the upstream NuGet package's declared
licence URL. Automated scanners inspecting the wheel will report these DLLs as
unlicensed.

`Python.Runtime.dll` itself is pythonnet's own code, MIT, copyright (c)
2006-2021 the contributors of the Python.NET project.

### pywinpty → winpty and ConPTY

The optional `pty` extra installs `pywinpty`, which bundles:

- `winpty/_winpty.cp3XX-win_amd64.pyd` — the Rust/PyO3 extension module
- `winpty/OpenConsole.exe`, `winpty/conpty.dll` — Windows Console / ConPTY host
- `winpty/winpty-agent.exe`, `winpty/winpty.dll` — the legacy pre-ConPTY backend

Attribution:

- **pywinpty** — copyright the Spyder project contributors, MIT License.
  https://github.com/spyder-ide/pywinpty (formerly `andfoy/pywinpty`)
- **winpty** (`winpty.dll`, `winpty-agent.exe`) — copyright (c) 2011-2016 Ryan
  Prichard, MIT License. https://github.com/rprichard/winpty
- **Windows Console / ConPTY host** (`OpenConsole.exe`, `conpty.dll`) — built
  from Microsoft's Windows Terminal / Console repository, copyright (c)
  Microsoft Corporation, MIT License. https://github.com/microsoft/terminal

pywinpty ships its own CycloneDX SBOM at
`pywinpty-<version>.dist-info/sboms/pywinpty.cyclonedx.json`, covering the Rust
crates statically linked into `_winpty`. Every crate in it is permissive:
`MIT`, `MIT OR Apache-2.0`, `Apache-2.0 WITH LLVM-exception`
(`target-lexicon`), `Unlicense OR MIT` (`memchr`), and
`(MIT OR Apache-2.0) AND Unicode-3.0` (`unicode-ident`). The principal crates
are `pyo3` 0.28.3, `winpty-rs` 1.0.6 and the `windows` 0.62.2 family, all
MIT OR Apache-2.0.

## Frontend

QuickCode's web UI vendors **no third-party JavaScript, CSS or fonts**. It
loads no CDN script, no external stylesheet, no web font and no remote image;
every asset under `quickcode/frontend/` is first-party and served from the
loopback server. The markdown renderer (`quickcode/frontend/js/markdown.js`)
and the JSON tokenizer (`quickcode/frontend/js/highlight.js`) are hand-written
in-house — despite the filename, the latter is not the `highlight.js` library.

## MIT License text

Applies to every package above marked MIT, and to QuickCode itself.

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

Full licence text for every other package is in that package's own
distribution, under `<package>-<version>.dist-info/licenses/`, and on the
project pages linked in the tables above. `proxy-tools` is the one exception —
it ships no licence file (see note 2).
