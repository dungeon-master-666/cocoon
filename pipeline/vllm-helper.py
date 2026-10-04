#!/usr/bin/env python3
"""Pinned vLLM bridge. Only head has an engine HTTP endpoint."""
import asyncio
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location('engine_helper', Path(__file__).with_name('engine-helper.py'))
engine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(engine)


class Helper(engine.Helper):
    async def backend_health(self):
        # The head's mandatory generation validates both ranks. A live headless
        # member alone is never evidence that the model/group is ready.
        return self.config['rank'] != 0 or await super().backend_health()

    def prepare_request(self, obj, rid):
        for name in ('use_beam_search', 'kv_transfer_params', 'cache_salt', 'priority',
                     'data_parallel_rank', 'lora_request', 'chat_template', 'prompt_embeds'):
            if name in obj:
                raise ValueError('unsupported vLLM extension')
        obj.pop('rid', None)
        obj['request_id'] = rid
        return f'X-Request-Id: {rid}\r\n'

    async def abort(self, rid):
        self.cancelled += 1
        try:
            # The private middleware cancels preprocessing/streaming and then
            # awaits engine.abort. A tombstone also covers registration in flight.
            code = await asyncio.wait_for(self.control('/pipeline/abort', {'rid': rid}), 2)
            if code != 200:
                raise RuntimeError('vLLM cancellation was not acknowledged')
        except Exception:
            self.abort_failures += 1
            self.fatal.set()


if __name__ == '__main__':
    engine.main(Helper, 'vllm')
