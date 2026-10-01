# Third-party notices

AIToolbox's own application and migrated integration code use the root MIT LICENSE. This V11.8 standalone source directly migrates the V11.7 LocalAI integration, scheduling and native contract patches. It contains no model weights, third-party recordings, credentials, request datasets, native DLLs or Docker image layers.

| Component | Pinned source / license |
| --- | --- |
| LocalAI | `7ad0cbf259f0c7bf9920fe2438fc3630ecd6c672`, MIT; `licenses/LocalAI-MIT.txt` |
| llama.cpp | `38a5b42d9a3e82e0a586bcd1caed121f36c87a73`, MIT; `licenses/llama.cpp-MIT.txt` |
| vLLM backend and GPU libraries | Immutable LocalAI backend OCI references in `config/runtime-lock.json`; upstream components retain their own licenses and notices in those images |
| CPython | Windows local packaging uses 3.11.9; `licenses/Python.txt` includes PSF and third-party notices |
| Tcl / Tk | 8.6.13 from that CPython distribution; `licenses/Tcl.txt`, `licenses/Tk.txt` |
| OpenSSL / libffi | CPython dependencies; `licenses/OpenSSL.txt`, `licenses/libffi.txt` |
| SQLite | CPython SQLite module; [public-domain dedication](https://www.sqlite.org/copyright.html) |
| PyInstaller | Build tooling pinned in `requirements-build.txt`; `licenses/PyInstaller.txt`, including the bootloader bundling exception |
| Microsoft Windows runtime DLLs | May be collected by local Windows packaging; `licenses/Microsoft-VC-2015-2022.txt`, `licenses/Microsoft-VC-2026.txt` contain separate proprietary terms |

`native/patches/` and `native/extensions/` carry the existing V11 changes against the above upstream revisions. They are now part of this independent source distribution. The Docker recipe downloads its fixed upstream dependencies; those dependencies, CUDA libraries, Docker, WSL, NVIDIA drivers and Windows speech voices are not relicensed by AIToolbox's MIT license.

The `licenses/` directory also retains prior CPU-build vendor notices for historical traceability. The V11 desktop build no longer bundles that CPU runtime or OpenMP DLL. Rebuilding with different Python or native dependencies requires matching notices to the actual artifacts. Keep all applicable upstream notices when redistributing a built image or binary.

The Windows speech probe is generated locally from AIToolbox's own short sentence. No recording or voice package is distributed. Model files and cloud accounts remain user-owned assets with their own terms.

This GitHub update publishes source only. Windows binary redistribution remains separate: this project has not verified the publisher's Microsoft runtime redistribution entitlement. Local build tests do not establish that entitlement. See [Microsoft redistribution guidance](https://learn.microsoft.com/en-us/cpp/windows/redistributing-visual-cpp-files?view=msvc-170).
