"""Anthropic's Claude API as a JSON-producing LLM provider.

Every caller hands over a JSON Schema (as for Infomaniak), and the answer is
constrained to it with structured outputs (`output_config.format`), so what
comes back always parses. The schemas here are written loosely - objects
without `additionalProperties`, `minimum` / `maximum` bounds - so a strict
copy is made for the API (`strict_schema`).

Set in .env to enable:
    ANTHROPIC_API_KEY=...        from platform.claude.com
    CLAUDE_MODEL=...             optional, default claude-opus-5-5
    CLAUDE_MODEL_SCORING=...     optional, for scoring jobs (else CLAUDE_MODEL)
    CLAUDE_MODEL_WRITING=...     optional, for CVs, letters and translation (else CLAUDE_MODEL)
    CLAUDE_EFFORT=...            optional: low | medium (default) | high | xhigh | max

Prompt caching: a caller may name the start of its prompt that is the same
for every job (`cache_prefix` - the candidate's CV and dossier). That part
is sent as its own block marked for caching, so the next request within five
minutes reads it from the cache at a tenth of the input price or less. A
batch of jobs scored back to back pays for the dossier once. `USAGE` adds up
the tokens per model, cache reads and writes included.

The SDK retries 408/409/429/5xx and connection errors itself. With no key
the helpers return None and the callers fall through to the next provider.
"""

import json
import os
import threading

DEFAULT_MODEL = 'claude-opus-5-5'
DEFAULT_EFFORT = 'medium'
MAX_TOKENS = 32000
TIMEOUT = 600
RETRIES = 4
# Keywords structured outputs does not accept; the prompts state any bound.
UNSUPPORTED = {'minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum', 'multipleOf',
               'minLength', 'maxLength', 'minItems', 'maxItems', 'uniqueItems', 'pattern'}

TASKS = {'scoring': 'CLAUDE_MODEL_SCORING', 'writing': 'CLAUDE_MODEL_WRITING'}

_client = None
# model -> {'requests', 'input_tokens', 'cache_creation_input_tokens',
#           'cache_read_input_tokens', 'output_tokens'}
USAGE = {}
_last = threading.local()


def last_model():
    """The model that gave this thread's last answer."""
    return getattr(_last, 'model', None)


def get_config(task=None):
    """(api key, model, effort) or None when not configured. `task`
    ('scoring', 'writing') picks that task's model when one is set."""
    key = os.environ.get('ANTHROPIC_API_KEY', '').strip()
    if not key:
        return None
    model = (os.environ.get(TASKS.get(task, ''), '').strip() if task in TASKS else '') \
        or os.environ.get('CLAUDE_MODEL', '').strip() or DEFAULT_MODEL
    effort = os.environ.get('CLAUDE_EFFORT', '').strip().lower() or DEFAULT_EFFORT
    return key, model, effort


def _record(model, usage):
    totals = USAGE.setdefault(model, {'requests': 0, 'input_tokens': 0, 'cache_creation_input_tokens': 0,
                                      'cache_read_input_tokens': 0, 'output_tokens': 0})
    totals['requests'] += 1
    for field in ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens', 'output_tokens'):
        totals[field] += getattr(usage, field, 0) or 0


def content_blocks(prompt, cache_prefix=None):
    """The prompt as one text block, or two with the shared start marked for
    caching - only when the prompt really starts with it."""
    if cache_prefix and len(cache_prefix) < len(prompt) and prompt.startswith(cache_prefix):
        return [{'type': 'text', 'text': cache_prefix, 'cache_control': {'type': 'ephemeral'}},
                {'type': 'text', 'text': prompt[len(cache_prefix):]}]
    return [{'type': 'text', 'text': prompt}]


def strict_schema(schema):
    """A copy of `schema` that structured outputs accepts: every object closed
    with additionalProperties: false, unsupported constraints removed."""
    if isinstance(schema, list):
        return [strict_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    out = {}
    for key, value in schema.items():
        if key in ('properties', '$defs', 'definitions') and isinstance(value, dict):
            # Field names, not keywords: a field may be called "pattern".
            out[key] = {name: strict_schema(sub) for name, sub in value.items()}
        elif key not in UNSUPPORTED:
            out[key] = strict_schema(value)
    types = out.get('type')
    if types == 'object' or (isinstance(types, list) and 'object' in types) or \
            isinstance(out.get('properties'), dict):
        out['additionalProperties'] = False
    return out


def _get_client(key):
    global _client
    if _client is None or _client.api_key != key:
        import anthropic
        _client = anthropic.Anthropic(api_key=key, timeout=TIMEOUT, max_retries=RETRIES)
    return _client


def chat_json(prompt, schema, temperature=None, models=None, task=None, cache_prefix=None):
    """Ask Claude for an object matching `schema` (a JSON Schema dict).

    `temperature` is accepted for the callers' sake and not sent: current
    Claude models reject sampling parameters. `models` overrides the model
    (first entry); `task` picks CLAUDE_MODEL_SCORING / _WRITING;
    `cache_prefix` is the start of `prompt` to cache. Returns None if
    unconfigured or on failure.
    """
    config = get_config(task)
    if not config:
        return None
    key, model, effort = config
    model = (models or [model])[0]
    import anthropic
    try:
        # Streamed: a generator answer with thinking can run long, and the
        # SDK refuses large max_tokens on a plain request.
        with _get_client(key).beta.messages.stream(
            model=model,
            max_tokens=MAX_TOKENS,
            messages=[{'role': 'user', 'content': content_blocks(prompt, cache_prefix)}],
            output_config={'effort': effort,
                           'format': {'type': 'json_schema', 'schema': strict_schema(schema)}},
            # A safety classifier's false positive is re-run on the
            # recommended fallback model instead of failing the job.
            betas=['server-side-fallback-2026-07-01'],
            fallbacks='default',
        ) as stream:
            message = stream.get_final_message()
    except anthropic.APIStatusError as e:
        print(f'  -> Claude {model} failed: HTTP {e.status_code} {e.message}')
        return None
    except anthropic.APIConnectionError as e:
        print(f'  -> Claude {model} unreachable: {e}')
        return None
    _record(model, message.usage)
    if message.stop_reason == 'refusal':
        print(f'  -> Claude {model} declined the request')
        return None
    if message.stop_reason == 'max_tokens':
        print(f'  -> Claude {model} ran out of output tokens')
        return None
    text = next((block.text for block in message.content if block.type == 'text'), '')
    _last.model = getattr(message, 'model', None) or model
    try:
        return json.loads(text)
    except ValueError as e:
        print(f'  -> Claude {model} returned unparseable JSON: {e}')
        return None


if __name__ == '__main__':
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))
    if not get_config():
        print('Not configured: set ANTHROPIC_API_KEY in .env')
        raise SystemExit(1)
    print('Model:', get_config()[1], '- effort:', get_config()[2])
    print('JSON round-trip:', chat_json(
        'Score this job 1-10 and say why in one sentence: "Fractional CTO, remote".',
        {'type': 'object',
         'properties': {'score': {'type': 'integer'},
                        'reasoning': {'type': 'string'}},
         'required': ['score', 'reasoning']}))
