# Dev GPU pipeline pilot (plan step 2)

This experiment runs **SGLang and vLLM independently** on two Linux hosts with one
NVIDIA GPU each. It measures Qwen3-0.6B PP=1 versus PP=2, then tests Qwen3-14B BF16
at PP=2, TP=1, context 4096, concurrency 1, without CPU weight offload. It does not
implement the Cocoon agent or confidential deployment.

Measured results and qualified settings: [REPORT.md](REPORT.md).

`profiles.json` pins image digests, model revisions, dtype and memory settings.
The SGLang profile disables chunked prefill: the pinned release failed the PP=2
long-prefill check with a KV allocator leak when chunking was enabled. vLLM uses
a 30-second model RPC deadline instead of its 300-second default, so losing a
remote rank can become an API error before the test client's 60-second timeout.
`lab.json` contains only dev host addresses, the SSH key *path* and tunnel settings;
it is separate from production profiles. Inspect or adapt that file before use.
The controller needs Python 3, SSH and SCP. Hosts need passwordless sudo, Docker,
the NVIDIA container runtime and a compatible NVIDIA driver. No PCI passthrough
or VM setup is needed for this host-container experiment.

From the repository root:

```sh
# Offline checks: no SSH, GPU or mutation of the hosts.
python3 -m unittest discover -s experiments/gpu-pipeline -p 'test_*.py' -v
python3 experiments/gpu-pipeline/pilot.py plan --backend sglang --model large --pp 2

# Host preparation: installs WG/iperf3/tcpdump, pulls pinned images/models.
# This explicit flag stops ONLY the existing GPU containers named in lab.json.
# Their data and containers survive; the pilot does not restart them afterwards.
python3 experiments/gpu-pipeline/pilot.py prepare --stop-existing

# Both engines, six trials, small-model PP comparison, independent failure reports.
python3 experiments/gpu-pipeline/pilot.py matrix
```

For one trial, upload scripts first, then select a backend/model/topology:

```sh
python3 experiments/gpu-pipeline/pilot.py upload
python3 experiments/gpu-pipeline/pilot.py run --backend vllm --model small --pp 1
python3 experiments/gpu-pipeline/pilot.py run --backend vllm --model small --pp 2 --network
python3 experiments/gpu-pipeline/pilot.py run --backend vllm --model large --pp 2
python3 experiments/gpu-pipeline/pilot.py compare --pp1-dir PATH_TO_PP1 --pp2-dir PATH_TO_PP2
```

Use `--backend sglang` for the other engine. Trials are sequential because each
host has one GPU. Do not run two controllers against the same lab simultaneously.

## What is checked

- Read-only identical model snapshots, SHA-256 manifests on both hosts, pinned
  runtime versions, actual tensor dtype/size from safetensors headers.
  For NCCL, inspect INIT logs as well as the number returned by PyTorch: the
  runtime library version in these images differs from `torch.cuda.nccl.version()`.
- A Docker `--network none` namespace per rank, with only `lo` and `wg0`. WireGuard
  is born in the underlay namespace and then moved into the container namespace.
  API requests originate inside head and connect to `127.0.0.1:30000`; there is
  no published API port or default route. The container has no `NET_ADMIN` or
  Docker socket. Only the host helper configures the network.
- `NCCL_NET=Socket`, `NCCL_SOCKET_IFNAME` set to the exact-match value `=wg0`, Gloo on `wg0`, disabled
  IB/P2P/SHM transports. Rendezvous and backend host addresses use overlay IPs.
- LAN and WG ping/iperf, including CPU counters, an MTU-sized no-fragment ping;
  two-rank CUDA send/recv correctness and timings for 16 KiB, 64 KiB and 64 MiB.
  Underlay capture is bounded to 50,000 packets and collected with NCCL logs.
- API warmup, greedy JSON fixtures with logprobs, incremental SSE with terminal
  `[DONE]`/finish/usage, EOS, stop string, cancel followed by a bounded successful
  request, and a prefill exceeding 2048 tokens.
- PP=1 versus PP=2 compares the *same* backend/dtype/revision: fixture text, token
  counts, finish reasons, generated tokens and logprobs (absolute tolerance 0.15).
  It does not require equality between different engines.
  These are numerical/topology fixtures, not a model-quality benchmark: a small
  model can produce the same incorrect answer in both topologies.
- During an active long stream, rank 1 backend processes are killed. The request
  must fail at the transport/API level; a client timeout alone does not pass.
  PyTorch async error handling is enabled and diagnostic-dump wait is capped at
  one second. GPU memory, rank logs, WG counters and cleanup are retained.

`results/<UTC-run-name>/` contains effective argv, profiles, versions, hashes,
raw measurements, captures, logs and result/cleanup JSON. It is ignored by Git;
the checked-in report records selected evidence and its run IDs. Inter-chunk
latencies are SSE arrival measurements, not claims about individual tokens in
engines that batch more than one token per event. Network measurements taken
during model/image downloads are not a clean performance baseline.

Private test keys stay root-only under `/etc/wireguard/cocoon-gpu-pilot/<run>/`
(allowed by Ubuntu's enforced WireGuard AppArmor profile). Cleanup removes the
trial's containers, interfaces, keys and bounded host monitoring processes;
models, existing workloads and experiment artifacts are retained. An interrupted
controller normally executes cleanup in `finally`. After a controller kill or
SSH outage, use the exact run ID on each host:

```sh
printf '%s\n' '{"action":"stop","run":"RUN_ID"}' |
  sudo python3 /home/ruslixag/cocoon-pipeline-dev/host.py
```

This is a dev fixture with host-visible keys and ordinary GPU/host memory. It
does not prove attestation, private RAM or NCCL compatibility with GPU CC.

References: [WireGuard namespaces](https://www.wireguard.com/netns/),
[SGLang pinned server arguments](https://github.com/sgl-project/sglang/blob/v0.5.10.post1/python/sglang/srt/server_args.py),
[vLLM pinned engine arguments](https://github.com/vllm-project/vllm/blob/v0.29.0/vllm/engine/arg_utils.py).
