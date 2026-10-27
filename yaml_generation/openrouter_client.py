"""OpenRouter chat-completions transport with universal PDF and JSON fallbacks."""
from __future__ import annotations
import asyncio
import base64
import json
import logging
import os
import random
import re
from pathlib import Path
from typing import Any

import httpx

LOG = logging.getLogger('manim-generator')
ENDPOINT = 'https://openrouter.ai/api/v1'


def pdf_part(data: bytes, filename='source.pdf') -> dict:
    """OpenRouter universal PDF input (including text-only models via file-parser)."""
    return {'type': 'file', 'file': {'filename': filename,
            'file_data': 'data:application/pdf;base64,' + base64.b64encode(data).decode('ascii')}}


def extract_json(content: str) -> Any:
    """Parse a JSON answer; accept Markdown fences or surrounding prose as fallback."""
    text = content.strip()
    if text.startswith('```'):
        text = re.sub(r'^```(?:json)?\s*', '', text, flags=re.I)
        text = re.sub(r'\s*```$', '', text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        starts = [m.start() for m in re.finditer(r'[\[{]', text)]
        for start in starts:
            try:
                parsed, _ = decoder.raw_decode(text[start:])
                return parsed
            except json.JSONDecodeError:
                pass
        raise ValueError('No valid JSON object found in model reply')


class OpenRouterClient:
    def __init__(self, *, api_key: str, timeout: float = 600, format_mode='auto',
                 pdf_engine='cloudflare-ai', max_tokens=12000, max_retries=3,
                 app_name='Manim YAML Pipeline'):
        if not api_key:
            raise ValueError('OPENROUTER_API_KEY not set')
        self.format_mode = format_mode
        self.pdf_engine = pdf_engine
        self.max_tokens = max_tokens
        self.max_retries = max_retries
        self.http = httpx.AsyncClient(
            base_url=ENDPOINT,
            headers={'Authorization': f'Bearer {api_key}', 'X-Title': app_name,
                     'Content-Type': 'application/json'},
            timeout=httpx.Timeout(timeout, connect=30),
            limits=httpx.Limits(max_connections=30, max_keepalive_connections=20))
        self.usage = []

    async def close(self):
        await self.http.aclose()

    async def list_models(self, query=''):
        response = await self.http.get('/models')
        response.raise_for_status()
        return [x for x in response.json().get('data', [])
                if query.lower() in (x.get('id') or '').lower()]

    async def json(self, model: str, schema: dict, name: str,
                   text: str, pdf: bytes | None = None, filename='source.pdf') -> dict:
        """Use schema when available, then permissive mode for any text-output model."""
        modes = ({'auto': ['json_schema', 'json_object', 'prompt'],
                  'json_schema': ['json_schema'],
                  'json_object': ['json_object'],
                  'prompt': ['prompt']}[self.format_mode])
        parts = [{'type': 'text', 'text':
            f'{text}\n\nReturn ONLY a JSON object conforming to this schema:\n'
            + json.dumps(schema, ensure_ascii=False)}]
        if pdf is not None:
            parts.append(pdf_part(pdf, filename))
        last_error = None
        for mode in modes:
            request = {
                'model': model,
                'messages': [{'role': 'user', 'content': parts}],
                'max_tokens': self.max_tokens,
            }
            if pdf is not None:
                request['plugins'] = [{'id': 'file-parser',
                                       'pdf': {'engine': self.pdf_engine}}]
            if mode == 'json_schema':
                request['response_format'] = {'type': 'json_schema', 'json_schema': {
                    'name': name, 'strict': True, 'schema': schema}}
                request['provider'] = {'require_parameters': True}
            elif mode == 'json_object':
                request['response_format'] = {'type': 'json_object'}
            for attempt in range(self.max_retries):
                try:
                    resp = await self.http.post('/chat/completions', json=request)
                    if resp.status_code in (400, 404, 422) and self.format_mode == 'auto':
                        last_error = f'{mode} unsupported: HTTP {resp.status_code}: {resp.text[:350]}'
                        LOG.warning('%s; trying less restrictive output mode', last_error)
                        break
                    if resp.status_code in (408, 409, 425, 429, 500, 502, 503, 504):
                        raise httpx.HTTPStatusError(f'{resp.status_code}: {resp.text[:500]}',
                                                     request=resp.request, response=resp)
                    resp.raise_for_status()
                    body = resp.json()
                    choice = body['choices'][0]
                    content = choice['message'].get('content')
                    if isinstance(content, list):
                        content = '\n'.join(c.get('text', '') for c in content if isinstance(c, dict))
                    if not isinstance(content, str) or not content.strip():
                        raise ValueError(f'Empty completion; finish_reason={choice.get("finish_reason")}')
                    result = extract_json(content)
                    if not isinstance(result, dict):
                        raise ValueError('Expected JSON object')
                    try:
                        import jsonschema
                        jsonschema.validate(result, schema)
                    except ImportError:
                        for field in schema.get('required', []):
                            if field not in result:
                                raise ValueError(f'Missing JSON field {field}')
                    usage = body.get('usage') or {}
                    self.usage.append({'stage': name, 'model_requested': model,
                                       'model_actual': body.get('model'), 'usage': usage,
                                       'provider': body.get('provider'), 'mode': mode})
                    LOG.info('%s model=%s mode=%s tokens=%s', name, model, mode,
                             usage.get('total_tokens', 'unknown'))
                    return result
                except (httpx.TimeoutException, httpx.TransportError,
                        httpx.HTTPStatusError, ValueError, KeyError, json.JSONDecodeError) as exc:
                    last_error = str(exc)
                    LOG.warning('%s (%s) attempt %s/%s: %s', name, mode,
                                attempt + 1, self.max_retries, last_error[:350])
                    if attempt + 1 < self.max_retries:
                        await asyncio.sleep(min(45, (2 ** attempt) + random.random()))
            # Attempt the next response format; if the model simply produced invalid JSON,
            # prompt mode can sometimes recover without a schema.
        raise RuntimeError(f'{name} failed on {model}: {last_error}')
