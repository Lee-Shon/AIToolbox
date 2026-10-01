"""Copy an existing verified V11.7 image and installed backends into an independent image.

Run in WSL. Reads the old backends without modifying them; copies no model,
configuration, key or data-service volumes. No backend is rebuilt or upgraded.
"""
import argparse
import hashlib
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-image', default='aitoolbox-localai:v4.10.0-contract-v4')
    parser.add_argument('--backends', type=Path, required=True)
    parser.add_argument('--tag', default='aitoolbox-localai:11.15.0')
    parser.add_argument('--verify-only', action='store_true')
    args = parser.parse_args()
    backend = args.backends.resolve(strict=True)
    for name in ['vllm_execution.py', 'vllm_asr_bridge.py']:
        expected = (ROOT / 'native/extensions/backend/python' / name).read_bytes().replace(b'\r\n', b'\n')
        actual = (backend / 'cuda13-vllm' / name).read_bytes()
        if hashlib.sha256(actual).digest() != hashlib.sha256(expected).digest():
            raise ValueError('Existing V11 adapter differs: ' + name)
    binary = backend / 'cuda12-llama-cpp/llama-cpp-cpu-all'
    expected = subprocess.check_output(['docker', 'run', '--rm', '--entrypoint', 'sha256sum',
        args.source_image, '/opt/aitoolbox-contract/llama-cpp-contract'], text=True).split()[0]
    for binary in [binary, binary.with_name('llama-cpp-grpc')]:
        if hashlib.sha256(binary.read_bytes()).hexdigest() != expected:
            raise ValueError('Existing llama adapter differs from the V11 image')
    for name in ['localai-vllm-execution.patch', 'localai-vllm-worker-state.patch']:
        subprocess.run(['git', 'apply', '--reverse', '--check', str(ROOT / 'native/patches' / name)],
                       cwd=backend / 'cuda13-vllm', check=True)
    if not (backend / 'cuda13-vllm/venv/bin/python').is_file():
        raise ValueError('Installed vLLM environment missing')
    if args.verify_only:
        print('Existing V11 adapters and patches verified; no image build requested.')
        return
    dockerfile = '''ARG SOURCE_IMAGE
FROM ${SOURCE_IMAGE}
COPY cuda12-llama-cpp/ /backends/cuda12-llama-cpp/
COPY cuda13-vllm/ /backends/cuda13-vllm/
LABEL org.aitoolbox.standalone="11.15.0" org.aitoolbox.migrated-from="v11.7.0"
'''
    subprocess.run(['docker', 'build', '-f', '-', '--build-arg', 'SOURCE_IMAGE=' + args.source_image,
                    '-t', args.tag, str(backend)], input=dockerfile, text=True, check=True)


if __name__ == '__main__':
    main()
