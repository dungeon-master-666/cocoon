#!/usr/bin/env python3
"""Prepare an immutable Linux dev image and artifact.json; serving is offline."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

sys.dont_write_bytecode=True
from bundle import digest

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[1]


def copy_source(root,source,excluded=()):
    def ignored(directory,names):
        return [name for name in names if name in ('.git','__pycache__','.venv','node_modules') or
                (Path(directory)==root and (name=='build' or name.startswith('cmake-build-'))) or
                (Path(directory)/name).resolve() in excluded]
    def copy_changed(src,dst):
        if os.path.isfile(dst) and os.stat(src).st_size==os.stat(dst).st_size:
            with open(src,'rb') as left,open(dst,'rb') as right:
                while True:
                    a,b=left.read(1024*1024),right.read(1024*1024)
                    if a!=b:break
                    if not a:return dst
        shutil.copy2(src,dst)
        # A checkout may change while a previous snapshot is compiling. Keeping
        # its original mtime can make changed bytes look older than the object.
        os.utime(dst,None)
        return dst
    if source.exists():
        for directory,dirs,files in os.walk(source,topdown=False):
            for name in files+dirs:
                path=Path(directory)/name
                if not (root/path.relative_to(source)).exists():
                    if path.is_dir():shutil.rmtree(path)
                    else:path.unlink()
    shutil.copytree(root,source,dirs_exist_ok=True,ignore=ignored,copy_function=copy_changed)


def run(args,capture=False):
    return subprocess.run([str(a) for a in args],check=True,text=True,
                          stdout=subprocess.PIPE if capture else None).stdout


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--backend',required=True,choices=('simulator','sglang','vllm'))
    p.add_argument('--base',help='simulator base, defaults to ubuntu:24.04 (resolved to digest)')
    p.add_argument('--build-dir',type=Path,required=True,help='persistent build cache, separate per architecture/backend')
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--save',action='store_true',help='also export image.tar for other hosts')
    args=p.parse_args()
    if args.backend!='simulator' and args.base:p.error('GPU backend base comes from the pinned profile catalogue')
    base=args.base or ('ubuntu:24.04' if args.backend=='simulator' else
        json.loads((ROOT/'experiments/gpu-pipeline/profiles.json').read_text())['backends'][args.backend]['image'])
    if '@sha256:' not in base:
        run(['docker','pull',base])
        base=json.loads(run(['docker','image','inspect',base],True))[0]['RepoDigests'][0]
    output=args.output.resolve();output.mkdir(parents=True,exist_ok=True)
    cache=args.build_dir.resolve();cache.mkdir(parents=True,exist_ok=True)
    # Pin the preparation image by ID too; mutable tags are never used to run ranks.
    iid=output/'builder.id'
    run(['docker','build','--iidfile',iid,'--build-arg','BASE='+base,'-f',HERE/'Dockerfile.build',HERE])
    builder=iid.read_text().strip()
    # CMake generates some TL/smart-contract files in its source tree. Give it
    # a private preparation snapshot so root builds never modify the checkout.
    source=output/'source'
    copy_source(ROOT,source,(output,cache))
    run(['docker','run','--rm',
         '--mount','type=bind,src='+str(source)+',dst=/work/cocoon',
         '--mount','type=bind,src='+str(cache)+',dst=/work/build',
         '--mount','type=bind,src='+str(output)+',dst=/export',
         builder,'/usr/bin/python3','-B','/work/cocoon/pipeline/deployment/build-runtime.py'])
    # Label the payload we actually built, even if the checkout changed while
    # the private preparation snapshot was compiling.
    catalog=digest(json.loads((output/'runtime/pipeline/sglang-models.json').read_text()))
    payload={str(p.relative_to(output/'runtime')):hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted((output/'runtime').rglob('*')) if p.is_file()}
    # Runtime uses the same distro/libraries as the builder, without source mounts.
    # Dev image deliberately retains tools needed by the sandbox and diagnostics.
    dockerfile=output/'Dockerfile.runtime'
    dockerfile.write_text('FROM '+builder+'\nCOPY runtime /opt/cocoon\n'+
        'LABEL org.cocoon.pipeline.backend="'+args.backend+'" org.cocoon.pipeline.catalog="'+catalog+'"\n'+
        'ENV PYTHONDONTWRITEBYTECODE=1 NVIDIA_DRIVER_CAPABILITIES=compute,utility\n'+
        'ENTRYPOINT ["/usr/bin/python3", "-I", "/opt/cocoon/pipeline/deployment/entry.py"]\n')
    # Only the runtime payload enters this context; exported archives/build cache cannot recurse.
    (output/'.dockerignore').write_text('*\n!Dockerfile.runtime\n!runtime\n!runtime/**\n')
    imagefile=output/'image.id'
    run(['docker','build','--iidfile',imagefile,'-f',dockerfile,output])
    image=imagefile.read_text().strip();info=json.loads(run(['docker','image','inspect',image],True))[0]
    artifact={'schema':1,'mode':'dev','backend':args.backend,'architecture':info['Architecture'],
              'image_id':image,'model_catalog_sha256':catalog,'base_image':base,'source_sha256':digest(payload)}
    (output/'artifact.json').write_text(json.dumps(artifact,indent=2)+'\n')
    (output/'runtime-files.sha256.json').write_text(json.dumps(payload,indent=2)+'\n')
    if args.save:run(['docker','save','-o',output/'image.tar',image])
    print(json.dumps(artifact,indent=2))


if __name__=='__main__':main()
