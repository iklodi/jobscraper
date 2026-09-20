"""Infomaniak AI Services as a drop-in JSON-producing LLM provider.

Infomaniak exposes an OpenAI-compatible endpoint at

    https://api.infomaniak.com/2/ai/{product_id}/openai/v1/chat/completions

so the only thing this module has to do is post a prompt and hand back the
parsed JSON object. Kept dependency-free (requests, already a dependency)
rather than pulling in the openai SDK for one call.

Set in .env to enable:
    INFOMANIAK_API_TOKEN=...     token with the 'ai-tools' scope
    INFOMANIAK_PRODUCT_ID=...    from GET https://api.infomaniak.com/1/ai
    INFOMANIAK_MODELS=qwen3      optional, comma separated cascade

With no token the helpers below return None and the callers fall through to
Gemini and Groq exactly as before.
"""

import json
import os

import requests

API_ROOT = 'https://api.infomaniak.com'
DEFAULT_MODELS = ['qwen3']
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
    """Some models wrap JSON in a markdown fence despite response_format."""
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


def chat_json(prompt, temperature=0.1):
    """Ask Infomaniak for a JSON object. None if unconfigured or every model fails."""
    config = get_config()
    if not config:
        return None
    token, product_id, models = config
    url = f'{API_ROOT}/2/ai/{product_id}/openai/v1/chat/completions'
    headers = {'Authorization': f'Bearer {token}',
               'Content-Type': 'application/json'}

    for model in models:
        try:
            res = requests.post(url, headers=headers, timeout=TIMEOUT, json={
                'model': model,
                'messages': [{'role': 'user', 'content': prompt}],
                'response_format': {'type': 'json_object'},
                'temperature': temperature,
            })
            res.raise_for_status()
            content = res.json()['choices'][0]['message']['content']
            return json.loads(_strip_fence(content))
        except Exception as e:
            print(f"Infomaniak ({model}) failed: {e}")
            continue
    return None


if __name__ == '__main__':
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))
    if not get_config():
        print('Not configured: set INFOMANIAK_API_TOKEN and INFOMANIAK_PRODUCT_ID in .env')
        raise SystemExit(1)
    print('Models:', ', '.join(list_models()) or '(none returned)')
    print('JSON test:', chat_json(
        'Reply with a JSON object: {"ok": true, "provider": "<your model family>"}'))
