"""Infomaniak AI Services as a drop-in JSON-producing LLM provider.

Infomaniak exposes an OpenAI-compatible endpoint at

    https://api.infomaniak.com/2/ai/{product_id}/openai/v1/chat/completions

with one deviation that matters here: response_format only accepts
'json_schema'. Both 'json_object' (what Gemini and Groq use) and 'text' are
rejected with a 400, so every caller has to hand over a schema. That is why
chat_json takes one instead of defaulting like the other providers.

Kept dependency-free (requests, already a dependency) rather than pulling in
the openai SDK for one call.

Set in .env to enable:
    INFOMANIAK_API_TOKEN=...     token with the 'ai-tools' scope
    INFOMANIAK_PRODUCT_ID=...    from GET https://api.infomaniak.com/1/ai
    INFOMANIAK_MODELS=a,b,c      optional, overrides the cascade below

With no token the helpers below return None and the callers fall through to
Gemini and Groq exactly as before.
"""

import json
import os

import requests

API_ROOT = 'https://api.infomaniak.com'
# Tried in order. The 122B answers a scoring prompt in a few seconds; the
# 397B is markedly slower (~40s) and only worth reaching for when the
# smaller one is down.
DEFAULT_MODELS = [
    'Qwen/Qwen3.5-122B-A10B-FP8',
    'mistralai/Mistral-Small-4-119B-2603',
    'Qwen/Qwen3.5-397B-A17B-FP8',
]
TIMEOUT = 180


def get_config():
    """(token, product_id, [model, ...]) or None when not configured."""
    token = os.environ.get('INFOMANIAK_API_TOKEN')
    product_id = os.environ.get('INFOMANIAK_PRODUCT_ID')
    if not token or not product_id:
        return None
    models = [m.strip() for m in
              os.environ.get('INFOMANIAK_MODELS', '').split(',') if m.strip()]
    return token, product_id, models or DEFAULT_MODELS


def list_models():
    """Model ids the account can use - handy for picking INFOMANIAK_MODELS."""
    config = get_config()
    if not config:
        return []
    token, product_id, _ = config
    res = requests.get(f'{API_ROOT}/2/ai/{product_id}/openai/v1/models',
                       headers={'Authorization': f'Bearer {token}'}, timeout=30)
    res.raise_for_status()
    return [m['id'] for m in res.json().get('data', [])]


def _strip_fence(text):
    """Belt and braces: a model may still wrap the schema output in a fence."""
    text = text.strip()
    if text.startswith('```json'):
        text = text[7:]
    elif text.startswith('```'):
        text = text[3:]
    if text.endswith('```'):
        text = text[:-3]
    start, end = text.find('{'), text.rfind('}')
    if start != -1 and end != -1:
        text = text[start:end + 1]
    return text


def chat_json(prompt, schema, temperature=0.1):
    """Ask Infomaniak for an object matching `schema` (a JSON Schema dict).

    Returns None if unconfigured or if every model in the cascade fails, so
    the caller can fall through to its existing providers.
    """
    config = get_config()
    if not config:
        return None
    token, product_id, models = config
    url = f'{API_ROOT}/2/ai/{product_id}/openai/v1/chat/completions'
    headers = {'Authorization': f'Bearer {token}',
               'Content-Type': 'application/json'}
    response_format = {
        'type': 'json_schema',
        'json_schema': {'name': 'result', 'strict': True, 'schema': schema},
    }

    for model in models:
        try:
            res = requests.post(url, headers=headers, timeout=TIMEOUT, json={
                'model': model,
                'messages': [{'role': 'user', 'content': prompt}],
                'response_format': response_format,
                'temperature': temperature,
            })
            res.raise_for_status()
            content = res.json()['choices'][0]['message']['content']
            return json.loads(_strip_fence(content))
        except Exception as e:
            print(f"  -> Infomaniak {model} failed: {e}")
            continue
    return None


if __name__ == '__main__':
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))
    if not get_config():
        print('Not configured: set INFOMANIAK_API_TOKEN and INFOMANIAK_PRODUCT_ID in .env')
        raise SystemExit(1)
    print('Models available:')
    for m in list_models():
        print('  ', m)
    print('\nCascade in use:', ', '.join(get_config()[2]))
    print('JSON round-trip:', chat_json(
        'Score this job 1-10 and say why in one sentence: "Fractional CTO, remote".',
        {'type': 'object',
         'properties': {'score': {'type': 'integer'},
                        'reasoning': {'type': 'string'}},
         'required': ['score', 'reasoning']}))
