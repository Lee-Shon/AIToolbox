# Third-party notices

The AIToolbox application source is covered by the root MIT LICENSE. The following components retain their own licenses. No model weights, third-party recorded audio, provider credentials, historical request datasets, LocalAI/Ray/HAMi patches, or CUDA libraries are included in this standalone distribution.

| Component | Source / version | License copy |
| --- | --- | --- |
| llama.cpp CPU runtime | [b10621](https://github.com/ggml-org/llama.cpp/releases/tag/b10621), official Windows CPU x64 archive; SHA256 in `config/runtime-lock.json` | `licenses/llama.cpp-MIT.txt` and bundled vendor notices |
| LLVM OpenMP runtime | Unmodified `libomp.dll` included by the above official archive | `licenses/LLVM-OpenMP.txt`, Apache 2.0 with LLVM exceptions and additional notices |
| CPython | [3.11.9](https://www.python.org/downloads/release/python-3119/), unmodified Windows distribution used for this build | `licenses/Python.txt`, PSF and included third-party notices |
| Tcl / Tk | 8.6.13, shipped with the above CPython distribution | `licenses/Tcl.txt`, `licenses/Tk.txt` |
| OpenSSL | 3.0.13, shipped with the above CPython distribution | `licenses/OpenSSL.txt`, Apache 2.0 |
| libffi | 3.4.4, bundled by CPython for ctypes | `licenses/libffi.txt`, MIT |
| Native vendor libraries | cpp-httplib, nlohmann JSON 3.12.0, miniaudio, stb_image, subprocess, SHA-1/SHA-256, xxHash, and embedded UI vendor notices from llama.cpp b10621 | Corresponding files in `licenses/` |
| SQLite | Bundled through CPython's SQLite module; [public-domain dedication](https://www.sqlite.org/copyright.html) | Public domain; see source link |
| PyInstaller | 6.22.3 bootloader and build tooling | `licenses/PyInstaller.txt`; its executable-bundling exception permits distributing this application under its own license |
| Microsoft VC runtime / Windows compatibility DLLs | Collected by the Windows build from the installed Python/native runtime dependencies | `licenses/Microsoft-VC-2015-2022.txt`, `licenses/Microsoft-VC-2026.txt`; proprietary terms, not covered by MIT |

The Windows platform and its system components retain their respective terms. The application uses installed Windows speech synthesis to generate a temporary probe from its own short text; it does not ship a recording or a speech voice. Users supply their own model files and cloud service subscriptions.

The native runtime is included unchanged. AIToolbox source was extracted from the existing AIToolbox cloud capture, business catalog, local registry and desktop interface, with new independent initialization, user-managed providers, bundled CPU operation and generated audio validation. It does not incorporate the old project's upstream patch sets.

Redistribute the complete license directory with the binary package. Building with different component versions requires reviewing and updating these notices and copies for the actual build; the application's MIT license does not replace third-party terms.

## Binary publication status

The current executable package is a tested local-use build. Public binary redistribution is **not cleared**: merely including Microsoft's runtime license text does not grant redistribution rights. Microsoft requires an applicable separate redistribution entitlement for those DLLs; this project has not verified such an entitlement for the publisher. Before publicly publishing this binary package, establish that entitlement or change the packaging to obtain the Microsoft runtime separately under its applicable terms. See [Microsoft's redistribution guidance](https://learn.microsoft.com/en-us/cpp/windows/redistributing-visual-cpp-files?view=msvc-170).

The source repository contains none of these DLLs; the source publication and local-use binary package are separate deliverables. This limitation does not authorize bundling users' model weights, provider subscriptions or private data.
