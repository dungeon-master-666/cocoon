"""Run inside the pinned image; download immutable public model revisions."""
import hashlib
import json
from pathlib import Path
from huggingface_hub import snapshot_download

profiles = json.loads(Path('/pilot/profiles.json').read_text())
for model in profiles['models'].values():
    target = Path('/models') / model['id'].split('/')[-1] / model['revision']
    snapshot_download(model['id'], revision=model['revision'], local_dir=target,
                      allow_patterns=['*.json', '*.safetensors', '*.txt', '*.jinja', '*.model'], max_workers=4)
    files = {}
    for path in sorted(target.rglob('*')):
        if path.is_file() and '.cache' not in path.parts and path.name != 'manifest.json':
            digest = hashlib.sha256()
            with path.open('rb') as source:
                while chunk := source.read(16 << 20):
                    digest.update(chunk)
            files[str(path.relative_to(target))] = {'sha256': digest.hexdigest(), 'bytes': path.stat().st_size}
    manifest = {'model': model, 'files': files}
    (target / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps({'model': model, 'files': len(files), 'bytes': sum(f['bytes'] for f in files.values())}), flush=True)
