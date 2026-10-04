"""Record installed runtime versions and actual checkpoint dtype/size, without loading weights."""
import importlib.metadata
import json
import math
from pathlib import Path
import platform
import struct
import sys
import torch

model = Path(sys.argv[1])
dtypes = {}
for path in sorted(model.glob('*.safetensors')):
    with path.open('rb') as source:
        header = json.loads(source.read(struct.unpack('<Q', source.read(8))[0]))
    for key, tensor in header.items():
        if key == '__metadata__':
            continue
        item = dtypes.setdefault(tensor['dtype'], {'parameters': 0, 'bytes': 0})
        item['parameters'] += math.prod(tensor['shape'])
        item['bytes'] += tensor['data_offsets'][1]-tensor['data_offsets'][0]
versions = {}
for name in ['vllm', 'sglang', 'transformers', 'triton', 'huggingface-hub']:
    try:
        versions[name] = importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        pass
print(json.dumps({'python': platform.python_version(), 'torch': torch.__version__,
                  'cuda': torch.version.cuda, 'nccl': torch.cuda.nccl.version(),
                  'packages': versions, 'weight_tensors': dtypes,
                  'model_config': json.loads((model/'config.json').read_text())}, indent=2))
