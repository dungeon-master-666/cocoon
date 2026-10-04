#!/usr/bin/env python3
"""Lab transfer: omit only layers of the pinned base already loaded on BOTH hosts.

Docker 29.1.3 accepts an archive referring to existing layers. The receiving
driver must check base_image before load and exact image_id afterwards. Public
deployment instructions use the ordinary complete docker save/load archive.
"""
import argparse
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--artifact',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
args=p.parse_args();artifact=json.loads(args.artifact.read_text())
def inspect(image):return json.loads(subprocess.check_output(['docker','image','inspect',image]))[0]
base=inspect(artifact['base_image']);image=inspect(artifact['image_id'])
assert image['Id']==artifact['image_id']
layers=base['RootFS']['Layers'];assert image['RootFS']['Layers'][:len(layers)]==layers
with tempfile.TemporaryDirectory(prefix='cocoon-image-export-',dir=args.output.parent) as tmp:
    full=Path(tmp)/'full.tar'
    subprocess.run(['docker','save','-o',str(full),artifact['image_id']],check=True)
    with tarfile.open(full) as source,tarfile.open(args.output,'w') as dest:
        manifest=json.load(source.extractfile('manifest.json'))
        assert len(manifest)==1
        omitted=set(manifest[0]['Layers'][:len(layers)])
        for entry in source:
            if entry.name not in omitted:dest.addfile(entry,source.extractfile(entry) if entry.isfile() else None)
print(json.dumps({'image_id':image['Id'],'requires_base':artifact['base_image'],'bytes':args.output.stat().st_size}))
