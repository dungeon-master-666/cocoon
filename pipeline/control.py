#!/usr/bin/env python3
"""Read local pipeline-agent status or request its bounded shutdown."""

import argparse
import json
import socket
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run_dir', type=Path)
    parser.add_argument('operation', choices=('status', 'stop'))
    args = parser.parse_args()
    try:
        with socket.socket(socket.AF_UNIX) as conn:
            conn.settimeout(2)
            conn.connect(str(args.run_dir / 'control.sock'))
            conn.sendall(json.dumps({'op': args.operation}).encode() + b'\n')
            with conn.makefile('rb') as reader:
                response = reader.readline(8193)
                if len(response) > 8192 or not response.endswith(b'\n'):
                    raise ValueError('incomplete or oversized agent response')
                body = json.loads(response)
        print(json.dumps(body, indent=2))
        return 1 if 'error' in body else 0
    except (OSError, ValueError) as error:
        parser.exit(1, f'pipeline control: {error}\n')


if __name__ == '__main__':
    raise SystemExit(main())
