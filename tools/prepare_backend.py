"""Prepare the existing V11 native build with pinned public upstream sources.

Run with Python 3.11+ in WSL, then docker build -t aitoolbox-localai:11.8.0 OUTPUT.
No old AIToolbox checkout, installed backend or model files are required.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tarfile
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]


def download(url, target):
    if target.exists():
        return
    temporary = target.with_suffix('.partial')
    try:
        with urlopen(Request(url, headers={'User-Agent': 'AIToolbox-source-build'}), timeout=120) as source, temporary.open('wb') as dest:
            shutil.copyfileobj(source, dest)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    lock = json.loads((ROOT / 'config/runtime-lock.json').read_text('utf-8'))
    upstream = output / 'LocalAI-upstream.tar.gz'
    download('https://codeload.github.com/mudler/LocalAI/tar.gz/' + lock['localai_commit'], upstream)
    # The inherited Dockerfile expects a flat git-archive layout.
    with tarfile.open(upstream) as source, tarfile.open(output / 'LocalAI.tar.gz', 'w:gz') as dest:
        prefix = 'LocalAI-' + lock['localai_commit'] + '/'
        checked = False
        for member in source:
            if member.name.rstrip('/') == prefix.rstrip('/'):
                continue
            if not member.name.startswith(prefix):
                raise ValueError('unexpected_localai_archive_root')
            member.name = member.name[len(prefix):]
            if not member.name:
                continue
            if member.name.startswith('/') or '..' in Path(member.name).parts:
                raise ValueError('invalid_archive_path')
            data = source.extractfile(member) if member.isfile() else None
            if member.name == 'backend/cpp/llama-cpp/grpc-server.cpp':
                if hashlib.sha256(data.read()).hexdigest() != 'c8255b692bef26b8dc690c69480b7ce0f16dd3a458bb36bdfe9effc602892977':
                    raise ValueError('unverified_native_source')
                data.seek(0)
                checked = True
            dest.addfile(member, data)
        if not checked:
            raise ValueError('native_source_missing')
    llama = output / 'llama.tar.gz'
    download('https://codeload.github.com/ggml-org/llama.cpp/tar.gz/' + lock['llama_commit'], llama)
    with tarfile.open(llama) as archive:
        if {p.name.split('/')[0] for p in archive} != {'llama.cpp-' + lock['llama_commit']}:
            raise ValueError('unverified_llama_source')
    native = ROOT / 'native'
    for file in (native / 'patches').glob('*.patch'):
        (output / file.name).write_bytes(file.read_bytes().replace(b'\r\n', b'\n'))
    for file in (native / 'extensions').rglob('*.go'):
        target = output / 'extensions' / file.relative_to(native / 'extensions')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(file.read_bytes().replace(b'\r\n', b'\n'))
    for source, target in [(native / 'Dockerfile', output / 'Dockerfile'),
                           (native / 'test_llama_preparation.cpp', output / 'test_llama_preparation.cpp'),
                           (native / 'extensions/backend/cpp/llama_prepared_input.h', output / 'llama_prepared_input.h')]:
        target.write_bytes(source.read_bytes().replace(b'\r\n', b'\n'))
    (output / 'python').mkdir(exist_ok=True)
    for name in ['vllm_execution.py', 'vllm_asr_bridge.py']:
        (output / 'python' / name).write_bytes((native / 'extensions/backend/python' / name).read_bytes().replace(b'\r\n', b'\n'))
    print('Prepared pinned V11 build context: ' + str(output))


if __name__ == '__main__':
    main()
