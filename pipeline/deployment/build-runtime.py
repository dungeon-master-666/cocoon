#!/usr/bin/env python3
"""Run only inside the preparation container; no build tools needed at serving time."""
import json
from pathlib import Path
import platform
import shutil
import subprocess

src=Path('/work/cocoon');out=Path('/export/runtime');build=Path('/work/build')
arch='x86-64' if platform.machine()=='x86_64' else 'armv8-a'
subprocess.run(['cmake','-S',str(src),'-B',str(build),'-G','Ninja','-USECP256K1_LIBRARY',
    '-DCMAKE_C_COMPILER=clang','-DCMAKE_CXX_COMPILER=clang++',
    '-DCMAKE_BUILD_TYPE=Release','-DTON_ONLY_TONLIB=ON','-DTON_USE_ROCKSDB=ON','-DTDDB_USE_ROCKSDB=ON',
    '-DBUILD_TESTING=OFF','-DCOCOON_ARCH='+arch,'-DTON_ARCH='+arch,'-DPORTABLE='+arch,
    '-DPython3_EXECUTABLE=/usr/bin/python3','-DCOCOON_PIPELINE_RUNTIME_DIR=/opt/cocoon/pipeline',
    '-DCOCOON_PIPELINE_SANDBOX=/opt/cocoon/bin/pipeline-backend-sandbox'],check=True)
targets=['pipeline-agent-dev','pipeline-backend-sandbox','worker-runner','proxy-runner',
         'client-runner','key-manager-runner','router','encrypt-message']
subprocess.run(['cmake','--build',str(build),'--target',*targets,'-j','4'],check=True)
check=Path('/export/agent-build-check.json')
check.write_text(json.dumps({'profile':'simulator-dev-pp2-wg-v1','rank':0,'role':'head',
    'group':{'peer_port':12310},'network':{'underlay_ip':'198.18.0.1','peer_ip':'198.18.0.2'},
    'deployment_id':'0'*64}))
effective=json.loads(subprocess.check_output([str(build/'pipeline/pipeline-agent-dev'),
    '--config',str(check),'--check-config'],text=True))
assert effective['effective_config']['deployment_id']=='0'*64
if out.exists():shutil.rmtree(out)
(out/'bin').mkdir(parents=True)
for target in targets:
    parent='pipeline' if target.startswith('pipeline-') else 'tee' if target=='router' else ''
    shutil.copy2(build/parent/target,out/'bin'/target)
(out/'pipeline/deployment').mkdir(parents=True)
for file in (src/'pipeline').glob('*.py'):shutil.copy2(file,out/'pipeline'/file.name)
shutil.copy2(src/'pipeline/sglang-models.json',out/'pipeline/sglang-models.json')
for file in (src/'pipeline/deployment').glob('*.py'):shutil.copy2(file,out/'pipeline/deployment'/file.name)
for role in ('worker','proxy','client','key-manager'):
    target=out/'spec'/('spec-'+role);target.mkdir(parents=True)
    shutil.copy2(src/'spec'/('spec-'+role)/(role+'-config.json'),target)
(out/'spec/fake-ton').mkdir()
for name in ('fake-ton-config.json','runtime.vars'):
    shutil.copy2(src/'spec/fake-ton'/name,out/'spec/fake-ton'/name)
