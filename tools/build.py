"""Build a standalone Windows directory, verifying the pinned native runtime."""
from pathlib import Path
import hashlib
import json
import shutil
import subprocess
import sys
from urllib.request import Request, urlopen
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]


def prepare_runtime():
    lock = json.loads((ROOT / "config/runtime-lock.json").read_text(encoding="utf-8"))
    cache = ROOT / ".build"
    cache.mkdir(exist_ok=True)
    archive = cache / "llama-cpu.zip"
    if not archive.exists():
        partial = archive.with_suffix(".partial")
        try:
            with urlopen(Request(lock["url"], headers={"User-Agent": "AIToolbox-build"}), timeout=60) as source, partial.open("wb") as dest:
                shutil.copyfileobj(source, dest)
            partial.replace(archive)
        finally:
            partial.unlink(missing_ok=True)
    if archive.stat().st_size != lock["bytes"] or hashlib.sha256(archive.read_bytes()).hexdigest() != lock["sha256"]:
        raise ValueError("native_archive_hash_mismatch")
    runtime = ROOT / "runtime/llama"
    runtime.mkdir(parents=True, exist_ok=True)
    with ZipFile(archive) as z:
        for info in z.infolist():
            target = (runtime / info.filename).resolve()
            if not target.is_relative_to(runtime.resolve()):
                raise ValueError("invalid_native_archive_path")
        for info in z.infolist():
            name = Path(info.filename).name
            if (name.startswith("ggml") and name.endswith(".dll")) or name in {
                    "llama-server.exe", "llama-server-impl.dll", "llama-fit-params.exe",
                    "llama-fit-params-impl.dll", "llama-common.dll", "llama.dll", "mtmd.dll",
                    "libomp.dll", "LICENSE-LLVM-OpenMP"}:
                z.extract(info, runtime)
    for required in ("llama-server.exe", "llama-fit-params.exe", "llama-fit-params-impl.dll"):
        if not (runtime / required).is_file():
            raise FileNotFoundError(required + " missing from pinned archive")
    return runtime


def main():
    if sys.platform != "win32":
        raise SystemExit("Build on Windows x64 with Python 3.11 and Tk support.")
    prepare_runtime()
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--windowed",
        "--name", "AIToolbox", "--paths", str(ROOT / "src"),
        "--distpath", str(ROOT / "dist"), "--workpath", str(ROOT / ".build/pyinstaller"),
        "--specpath", str(ROOT / ".build"),
        "--add-data", str(ROOT / "src/aitoolbox_data/migrations") + ";aitoolbox_data/migrations",
        "--add-data", str(ROOT / "licenses") + ";licenses",
        *sum((["--add-binary", str(file) + ";runtime/llama"] for file in sorted((ROOT / "runtime/llama").iterdir())
              if (file.name.startswith("ggml") and file.suffix == ".dll") or file.name in {
                  "llama-server.exe", "llama-server-impl.dll", "llama-fit-params.exe",
                  "llama-fit-params-impl.dll", "llama-common.dll", "llama.dll", "mtmd.dll", "libomp.dll"}), []),
        str(ROOT / "src/main.py")], cwd=ROOT, check=True)
    destination = ROOT / "dist/AIToolbox"
    for name in ("README.md", "LICENSE", "THIRD_PARTY_NOTICES.md"):
        shutil.copyfile(ROOT / name, destination / name)
    shutil.copytree(ROOT / "licenses", destination / "licenses", dirs_exist_ok=True)
    manifest = {"version": "10.3.0", "python": sys.version.split()[0], "runtime": json.loads((ROOT / "config/runtime-lock.json").read_text()),
                "files": {p.relative_to(destination).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in sorted(destination.rglob("*")) if p.is_file() and p.name != "manifest.json"}}
    (destination / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    shutil.make_archive(str(ROOT / "dist/AIToolbox-Windows-x64"), "zip", ROOT / "dist", "AIToolbox")
    print("Built:", ROOT / "dist/AIToolbox-Windows-x64.zip")


if __name__ == "__main__":
    main()
