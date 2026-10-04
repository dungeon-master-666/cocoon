#!/usr/bin/env python3
"""Step 11 GPU acceptance using the same Cocoon API/fault suite as SGLang."""
from pathlib import Path
import runpy
import sys

if __name__ == '__main__':
    sys.argv.extend(['--backend', 'vllm'])
    runpy.run_path(str(Path(__file__).with_name('test-pipeline-sglang-gpu.py')), run_name='__main__')
