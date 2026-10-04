"""Private vLLM 0.29.0 ASGI cancellation control inside the engine namespace.

The UDS bridge allows only the two inference paths; this route is never exposed
through gate. No backend patches, arbitrary engine RPC, or client-selected IDs.
"""
import asyncio
import json
import re
import time


class CancellationMiddleware:
    def __init__(self, app):
        self.app = app
        self.active = {}
        self.cancelled = {}

    async def reply(self, send, status):
        await send({'type': 'http.response.start', 'status': status,
                    'headers': [(b'content-length', b'0')]})
        await send({'type': 'http.response.body', 'body': b''})

    def expire(self):
        now = time.monotonic()
        self.cancelled = {rid: deadline for rid, deadline in self.cancelled.items() if deadline > now}

    async def abort(self, scope, receive, send):
        if scope['method'] != 'POST':
            return await self.reply(send, 405)
        body = bytearray()
        try:
            while True:
                message = await asyncio.wait_for(receive(), 1)
                if message['type'] != 'http.request':
                    raise ValueError('invalid control request')
                body.extend(message.get('body', b''))
                if len(body) > 1024:
                    raise ValueError('control request too large')
                if not message.get('more_body', False):
                    break
            obj = json.loads(body)
            rid = obj['rid']
            if set(obj) != {'rid'} or not isinstance(rid, str) or not re.fullmatch('[0-9a-f]{32}', rid):
                raise ValueError('invalid request identity')
        except (ValueError, KeyError, TypeError, asyncio.TimeoutError):
            return await self.reply(send, 400)
        self.expire()
        if rid not in self.cancelled and len(self.cancelled) >= 1024:
            return await self.reply(send, 503)
        # Longer than the bridge's entire request deadline. A late request must
        # not be admitted after the cancellation endpoint has acknowledged it.
        self.cancelled[rid] = time.monotonic() + 180
        try:
            task = self.active.get(rid)
            if task:
                task.cancel()
                await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 1)
            engine = scope['app'].state.engine_client
            # Pinned OpenAI serving IDs: one chat prompt or one text prompt.
            await asyncio.wait_for(engine.abort([f'chatcmpl-{rid}', f'cmpl-{rid}-0']), 1)
        except Exception:
            return await self.reply(send, 503)
        await self.reply(send, 200)

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        if scope['path'] == '/pipeline/abort':
            return await self.abort(scope, receive, send)
        if scope['path'] not in ('/v1/chat/completions', '/v1/completions'):
            return await self.app(scope, receive, send)
        rid = dict(scope['headers']).get(b'x-request-id', b'').decode('ascii', errors='replace')
        if not re.fullmatch('[0-9a-f]{32}', rid):
            return await self.reply(send, 400)
        self.expire()
        if rid in self.cancelled or rid in self.active:
            return await self.reply(send, 409)
        if len(self.active) >= 16:
            return await self.reply(send, 429)
        task = asyncio.create_task(self.app(scope, receive, send))
        self.active[rid] = task
        try:
            await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self.active.pop(rid, None)
