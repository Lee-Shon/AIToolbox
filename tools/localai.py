"""Start the migrated V11 backend with an independent data root and read-only assets."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
from urllib.request import Request, urlopen


def wsl_path(path):
    path = Path(path).resolve()
    if not path.drive or str(path).startswith('\\\\'):
        raise ValueError('Use a local Windows drive path')
    return '/mnt/' + path.drive[0].lower() + '/' + path.as_posix()[3:]


def docker(distribution, *args, required=True):
    result = subprocess.run(['wsl.exe', '-d', distribution, '-u', 'root', '--exec', 'docker', *args],
                            capture_output=True, text=True, encoding='utf-8', errors='replace',
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if required and result.returncode:
        # Do not echo command arguments: container environment contains a key.
        raise RuntimeError('Docker command failed: ' + result.stderr[-2000:])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['start', 'stop', 'status'])
    parser.add_argument('--data-dir', type=Path, default=Path(os.environ['LOCALAPPDATA']) / 'AIToolbox')
    parser.add_argument('--distribution', help='WSL2 distribution with Docker and NVIDIA Container Toolkit')
    parser.add_argument('--image', help='Migrated V11 contract image (not an unpatched LocalAI image)')
    parser.add_argument('--port', type=int)
    parser.add_argument('--asset-root', type=Path, action='append', help='Repeat for each read-only model directory')
    args = parser.parse_args()
    root = args.data_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    # The same data-directory lock as the desktop; never change mounts under a running API.
    import msvcrt
    with (root / 'application.lock').open('a+b') as owner:
        if owner.tell() == 0:
            owner.write(b'0')
            owner.flush()
        owner.seek(0)
        msvcrt.locking(owner.fileno(), msvcrt.LK_NBLCK, 1)
        try:
            run(args, root)
        finally:
            owner.seek(0)
            msvcrt.locking(owner.fileno(), msvcrt.LK_UNLCK, 1)


def run(args, root):
    path = root / 'settings.json'
    settings = json.loads(path.read_text('utf-8')) if path.exists() else dict(cloud_port=49777, local_port=49778)
    saved = settings.get('localai', {})
    distro = args.distribution or saved.get('distribution', 'Ubuntu')
    image = args.image or saved.get('image', 'aitoolbox-localai:11.15.0')
    port = args.port if args.port is not None else saved.get('port', 49779)
    if not 1 <= port <= 65535 or port in [settings['cloud_port'], settings['local_port']]:
        raise ValueError('LocalAI port must be distinct from the client API ports')
    own_assets = root / 'models'
    own_assets.mkdir(exist_ok=True)
    mounts = saved.get('asset_mounts', [{'host': str(own_assets), 'target': '/models/assets-user'}])
    if args.asset_root:
        mounts = [{'host': str(p.resolve(strict=True)), 'target': f'/models/assets-{i}'}
                  for i, p in enumerate(args.asset_root)]
    if any(not Path(m['host']).is_dir() for m in mounts):
        raise ValueError('Asset roots must be existing directories')
    identity = hashlib.sha256(str(root).lower().encode()).hexdigest()[:20]
    name = 'aitoolbox-' + identity
    backend = dict(distribution=distro, image=image, port=port, url=f'http://127.0.0.1:{port}',
                   container=name, asset_mounts=mounts)
    fingerprint = hashlib.sha256(json.dumps(backend, sort_keys=True).encode()).hexdigest()
    found = docker(distro, 'container', 'inspect', name, required=False)
    container = json.loads(found.stdout)[0] if found.returncode == 0 else None
    if container and container['Config'].get('Labels', {}).get('org.aitoolbox.owner') != identity:
        raise RuntimeError('Container belongs to a different installation')
    if args.action == 'status':
        print(json.dumps({'container': name, 'running': bool(container and container['State']['Running']),
                          'url': backend['url']}, ensure_ascii=False))
        return
    if args.action == 'stop':
        if container:
            docker(distro, 'stop', name)
        print('Stopped own backend; data and models retained.')
        return
    security = root / 'runtime/security'
    security.mkdir(parents=True, exist_ok=True)
    key_path = security / 'localai-api.key'
    if not key_path.exists():
        key_path.write_text(secrets.token_urlsafe(40), encoding='ascii')
    key = key_path.read_text('ascii').strip()
    if len(key) < 32:
        raise ValueError('Invalid LocalAI key file')
    if container:
        if (container['Config']['Labels'].get('org.aitoolbox.configuration') != fingerprint
                or 'LOCALAI_API_KEY=' + key not in container['Config']['Env']):
            raise RuntimeError('Existing backend configuration differs. Stop the app and backend, '
                               'remove only container ' + name + ', then rerun this command. Data remains on disk.')
        docker(distro, 'start', name)
    else:
        arguments = ['run', '-d', '--name', name, '--restart', 'unless-stopped', '--gpus', 'all',
                     '--ipc=host', '--shm-size=8g', '-p', f'127.0.0.1:{port}:8080',
                     '--label', 'org.aitoolbox.owner=' + identity,
                     '--label', 'org.aitoolbox.configuration=' + fingerprint]
        for directory, target in [('models', '/models'), ('configuration', '/configuration'), ('data', '/data')]:
            folder = root / 'runtime/localai' / directory
            folder.mkdir(parents=True, exist_ok=True)
            arguments += ['-v', wsl_path(folder) + ':' + target]
        for mount in mounts:
            arguments += ['-v', wsl_path(mount['host']) + ':' + mount['target'] + ':ro']
        for value in ['LOCALAI_API_KEY=' + key, 'LOCALAI_MODELS_PATH=/models', 'LOCALAI_BACKENDS_PATH=/backends',
                      'LOCALAI_AUTOLOAD_GALLERIES=false', 'LOCALAI_AUTOLOAD_BACKEND_GALLERIES=false',
                      'LOCALAI_DISABLE_HARDWARE_DEFAULTS=true', 'VLLM_USE_FLASHINFER_SAMPLER=0',
                      'VLLM_USE_V2_MODEL_RUNNER=0']:
            arguments += ['-e', value]
        docker(distro, *arguments, image, '--address', '0.0.0.0:8080')
    for _ in range(60):
        try:
            request = Request(backend['url'] + '/v1/models', headers={'Authorization': 'Bearer ' + key})
            with urlopen(request, timeout=3) as response:
                if response.status == 200:
                    break
        except OSError:
            time.sleep(2)
    else:
        raise RuntimeError('Backend did not become healthy; inspect its Docker logs')
    settings['localai'] = backend
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)
    print('V11 backend ready at ' + backend['url'] + '; start AIToolbox with the same --data-dir.')


if __name__ == '__main__':
    main()
