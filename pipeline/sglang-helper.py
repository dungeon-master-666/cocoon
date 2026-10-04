#!/usr/bin/env python3
"""SGLang entry point; common bridge/process ownership lives in engine-helper."""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location('engine_helper', Path(__file__).with_name('engine-helper.py'))
engine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(engine)

if __name__ == '__main__':
    engine.main()
