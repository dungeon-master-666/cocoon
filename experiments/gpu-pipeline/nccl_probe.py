"""Two-rank CUDA ping-pong correctness and timing, over the pinned Socket network."""
import datetime
import json
import os
from pathlib import Path
import sys
import time
import torch
import torch.distributed as dist

rank = int(sys.argv[1])
torch.cuda.set_device(0)
dist.init_process_group('nccl', init_method='tcp://10.231.0.1:29600', rank=rank,
                        world_size=2, timeout=datetime.timedelta(seconds=120))
measurements = []
for size, count in [(16 * 1024, 30), (64 * 1024, 30), (64 * 1024 * 1024, 4)]:
    sent = torch.full((size // 2,), 7, dtype=torch.bfloat16, device='cuda')
    received = torch.empty_like(sent)
    dist.barrier()
    torch.cuda.synchronize()
    start = time.monotonic()
    for _ in range(count):
        if rank == 0:
            dist.send(sent, 1)
            dist.recv(received, 1)
        else:
            dist.recv(received, 0)
            dist.send(received, 0)
    torch.cuda.synchronize()
    elapsed = time.monotonic() - start
    assert torch.all(received == 7).item(), 'corrupted transfer'
    measurements.append({'bytes': size, 'iterations': count, 'seconds': elapsed,
                         'round_trip_ms': elapsed * 1000 / count,
                         'payload_mbit_s': 2 * size * count * 8 / elapsed / 1e6})
result = {'rank': rank, 'torch': torch.__version__, 'cuda': torch.version.cuda,
          'nccl': torch.cuda.nccl.version(), 'measurements': measurements,
          'env': {k: v for k, v in os.environ.items() if k.startswith(('NCCL_', 'GLOO_'))}}
Path(f'/artifacts/nccl-{rank}.json').write_text(json.dumps(result, indent=2) + '\n')
dist.destroy_process_group()
