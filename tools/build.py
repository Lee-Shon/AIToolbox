"""Package the standalone desktop; the migrated LocalAI backend is prepared separately."""
from pathlib import Path
import hashlib
import json
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    if sys.platform != 'win32':
        raise SystemExit('Build on Windows x64 with Python 3.11 and Tk support.')
    additions = [(ROOT / 'src/aitoolbox_data/migrations', 'aitoolbox_data/migrations'),
                 (ROOT / 'src/local_product/fixtures', 'local_product/fixtures'),
                 (ROOT / 'src/local_product/native-contract.json', 'local_product'),
                 (ROOT / 'licenses', 'licenses')]
    subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir', '--windowed',
        '--name', 'AIToolbox', '--paths', str(ROOT / 'src'),
        '--distpath', str(ROOT / 'dist'), '--workpath', str(ROOT / '.build/pyinstaller'),
        '--specpath', str(ROOT / '.build'),
        *sum((['--add-data', str(source) + ';' + target] for source, target in additions), []),
        str(ROOT / 'src/main.py')], cwd=ROOT, check=True)
    destination = ROOT / 'dist/AIToolbox'
    for name in ('README.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md'):
        shutil.copyfile(ROOT / name, destination / name)
    for name in ('licenses', 'config', 'native'):
        shutil.copytree(ROOT / name, destination / name, dirs_exist_ok=True)
    (destination / 'tools').mkdir(exist_ok=True)
    for name in ('localai.py', 'prepare_backend.py', 'migrate_backend.py'):
        shutil.copyfile(ROOT / 'tools' / name, destination / 'tools' / name)
    manifest = {'version': '11.8.0', 'python': sys.version.split()[0],
                'runtime': json.loads((ROOT / 'config/runtime-lock.json').read_text()),
                'files': {p.relative_to(destination).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in sorted(destination.rglob('*')) if p.is_file() and p.name != 'manifest.json'}}
    (destination / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    shutil.make_archive(str(ROOT / 'dist/AIToolbox-Windows-x64'), 'zip', ROOT / 'dist', 'AIToolbox')
    print('Built:', ROOT / 'dist/AIToolbox-Windows-x64.zip')


if __name__ == '__main__':
    main()
