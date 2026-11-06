"""Every model provider, asked in the candidate's order.

LLM_PROVIDERS in .env names the providers in the order they are tried: the
first is the default, the rest its fallbacks. The default order is

    infomaniak,claude,gemini,groq

and a provider missing from the setting is never asked. One that is not
configured (no key) is skipped. Each answers `chat_json(prompt, schema)`
with a dict, or None so the next one is tried:

- infomaniak, claude: the answer is constrained to the JSON Schema.
- gemini, groq: JSON mode only; the schema is not sent (as before), so the
  prompt's own description of the shape is what they follow.

`private=True` keeps text to the schema providers (Infomaniak, Claude) -
used for the decline emails, which never go to Gemini or Groq.
"""

import json
import os
import threading

import claude
import infomaniak

GEMINI_MODELS = [
#    'gemini-3.1-pro-preview',
    'gemini-3.5-flash',
    'gemini-3-flash-preview',
    'gemini-3.1-flash-lite',
]
GROQ_MODELS = [
    'openai/gpt-oss-120b',
    'openai/gpt-oss-20b',
]
DEFAULT_ORDER = ('infomaniak', 'claude', 'gemini', 'groq')
PRIVATE = ('infomaniak', 'claude')


def _json_from(text):
    """The object in a reply that may be fenced or have words around it."""
    text = (text or '').strip()
    start, end = text.find('{'), text.rfind('}')
    if start != -1 and end != -1:
        text = text[start:end + 1]
    return json.loads(text)


_state = threading.local()


def answered_by():
    """(provider, model) that gave this thread's last llm.chat_json answer,
    or (None, None). The scorer picks its weight set by the model."""
    return getattr(_state, 'answered', (None, None))


class Gemini:
    _clients = {}
    _last = threading.local()

    @classmethod
    def last_model(cls):
        return getattr(cls._last, 'model', None)

    @staticmethod
    def get_config():
        return os.environ.get('GEMINI_API_KEY', '').strip() or None

    @classmethod
    def chat_json(cls, prompt, schema, temperature=None):
        key = cls.get_config()
        if not key:
            return None
        from google import genai
        from google.genai import types
        client = cls._clients.get(key) or cls._clients.setdefault(key, genai.Client(api_key=key))
        for model in GEMINI_MODELS:
            try:
                response = client.models.generate_content(
                    model=model, contents=prompt,
                    config=types.GenerateContentConfig(response_mime_type='application/json'))
                answer = _json_from(response.text)
                cls._last.model = model
                return answer
            except Exception as e:
                text = str(e).lower()
                if '429' in text or 'resource_exhausted' in text or 'quota' in text:
                    print(f'  -> Gemini {model} rate limited. Cascading to next model...')
                elif '503' in text or 'unavailable' in text:
                    print(f'  -> Gemini {model} overloaded. Cascading to next model...')
                else:
                    print(f'  -> Gemini error on {model}: {e}')
        return None


class Groq:
    _clients = {}
    _last = threading.local()

    @classmethod
    def last_model(cls):
        return getattr(cls._last, 'model', None)

    @staticmethod
    def get_config():
        return os.environ.get('GROQ_API_KEY', '').strip() or None

    @classmethod
    def chat_json(cls, prompt, schema, temperature=0.1):
        key = cls.get_config()
        if not key:
            return None
        from groq import Groq as Client
        client = cls._clients.get(key) or cls._clients.setdefault(key, Client(api_key=key))
        for model in GROQ_MODELS:
            try:
                response = client.chat.completions.create(
                    model=model, messages=[{'role': 'user', 'content': prompt}],
                    response_format={'type': 'json_object'},
                    temperature=0.1 if temperature is None else temperature)
                answer = _json_from(response.choices[0].message.content)
                cls._last.model = model
                return answer
            except Exception as e:
                text = str(e).lower()
                if '429' in text or 'rate_limit' in text:
                    print(f'  -> Groq {model} rate limited. Cascading...')
                elif '503' in text:
                    print(f'  -> Groq {model} overloaded. Cascading...')
                else:
                    print(f'  -> Groq error on {model}: {e}')
        return None


PROVIDERS = {'infomaniak': infomaniak, 'claude': claude, 'gemini': Gemini, 'groq': Groq}


def order(private=False):
    """Configured provider names, in the order they are asked."""
    setting = os.environ.get('LLM_PROVIDERS', '')
    names = [n.strip().lower() for n in setting.split(',') if n.strip()]
    unknown = [n for n in names if n not in PROVIDERS]
    if unknown:
        print(f"  -> LLM_PROVIDERS: unknown provider(s) {', '.join(unknown)} ignored "
              f"(known: {', '.join(PROVIDERS)})")
    names = list(dict.fromkeys(n for n in names if n in PROVIDERS)) or list(DEFAULT_ORDER)
    if private:
        names = [n for n in names if n in PRIVATE]
    return [n for n in names if PROVIDERS[n].get_config()]


def configured(private=False):
    return bool(order(private))


def chat_json(prompt, schema, temperature=0.1, private=False, task=None, cache_prefix=None):
    """The first answer matching `schema`, trying providers in order; None
    when none answers. `task` ('scoring', 'writing') and `cache_prefix` (the
    start of the prompt shared by every job) are for Claude: its per-task
    model and prompt caching."""
    _state.answered = (None, None)
    for name in order(private):
        extra = {'task': task, 'cache_prefix': cache_prefix} if name == 'claude' else {}
        provider = PROVIDERS[name]
        result = provider.chat_json(prompt, schema, temperature=temperature, **extra)
        if result:
            last = getattr(provider, 'last_model', None)
            _state.answered = (name, last() if last else None)
            return result
    return None
