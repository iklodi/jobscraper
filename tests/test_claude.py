"""The Claude provider and the provider order, with a stubbed client: no network."""

import json
import types

import pytest

import claude
import infomaniak
import llm


class FakeStream:
    def __init__(self, message, calls, kwargs):
        self.message, self.calls, self.kwargs = message, calls, kwargs

    def __enter__(self):
        self.calls.append(self.kwargs)
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.message


def fake_client(text, stop_reason='end_turn', calls=None):
    calls = calls if calls is not None else []
    usage = types.SimpleNamespace(input_tokens=1, cache_creation_input_tokens=0,
                                  cache_read_input_tokens=0, output_tokens=1)
    message = types.SimpleNamespace(stop_reason=stop_reason, usage=usage,
                                    content=[types.SimpleNamespace(type='thinking', thinking=''),
                                             types.SimpleNamespace(type='text', text=text)])
    messages = types.SimpleNamespace(stream=lambda **kw: FakeStream(message, calls, kw))
    return types.SimpleNamespace(beta=types.SimpleNamespace(messages=messages)), calls


SCHEMA = {'type': 'object',
          'properties': {'score': {'type': 'integer', 'minimum': 1, 'maximum': 10},
                         'pattern': {'type': 'string'},
                         'items': {'type': 'array',
                                   'items': {'type': 'object', 'properties': {'a': {'type': 'string'}}}}},
          'required': ['score']}


def test_strict_schema_closes_objects_and_drops_bounds():
    strict = claude.strict_schema(SCHEMA)
    assert strict['additionalProperties'] is False
    assert strict['properties']['items']['items']['additionalProperties'] is False
    assert 'minimum' not in strict['properties']['score']
    assert 'pattern' in strict['properties'], 'a field named like a keyword is kept'
    assert 'additionalProperties' not in SCHEMA, 'the original is left alone'


def test_unconfigured_returns_none(monkeypatch):
    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)
    assert claude.get_config() is None
    assert claude.chat_json('x', SCHEMA) is None


def test_chat_json_sends_schema_effort_and_fallbacks(monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'test-key')
    monkeypatch.setenv('CLAUDE_EFFORT', 'low')
    monkeypatch.delenv('CLAUDE_MODEL', raising=False)
    client, calls = fake_client(json.dumps({'score': 7}))
    monkeypatch.setattr(claude, '_get_client', lambda key: client)
    assert claude.chat_json('Score it', SCHEMA) == {'score': 7}
    sent = calls[0]
    assert sent['model'] == claude.DEFAULT_MODEL
    assert sent['output_config']['effort'] == 'low'
    assert sent['output_config']['format']['schema']['additionalProperties'] is False
    assert sent['fallbacks'] == 'default'
    assert 'temperature' not in sent


def test_refusal_and_truncation_return_none(monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'test-key')
    for reason in ('refusal', 'max_tokens'):
        client, _ = fake_client('{"score": 7}', stop_reason=reason)
        monkeypatch.setattr(claude, '_get_client', lambda key, c=client: c)
        assert claude.chat_json('x', SCHEMA) is None


@pytest.fixture
def all_configured(monkeypatch):
    monkeypatch.setattr(infomaniak, 'get_config', lambda: ('t', 'p', ['m']))
    monkeypatch.setattr(claude, 'get_config', lambda: ('k', 'm', 'medium'))
    monkeypatch.setenv('GEMINI_API_KEY', 'g')
    monkeypatch.setenv('GROQ_API_KEY', 'q')
    monkeypatch.delenv('LLM_PROVIDERS', raising=False)


def test_llm_order_follows_setting_and_skips_unconfigured(monkeypatch, all_configured):
    assert llm.order() == ['infomaniak', 'claude', 'gemini', 'groq']
    monkeypatch.setenv('LLM_PROVIDERS', 'Claude, gemini, infomaniak, nonsense, claude')
    assert llm.order() == ['claude', 'gemini', 'infomaniak'], 'groq left out: never asked'
    assert llm.order(private=True) == ['claude', 'infomaniak'], 'declines: never gemini or groq'
    monkeypatch.setenv('LLM_PROVIDERS', 'claude')
    assert llm.order() == ['claude']
    monkeypatch.setattr(claude, 'get_config', lambda: None)
    assert llm.order() == [] and not llm.configured()


def test_llm_falls_through_in_order(monkeypatch, all_configured):
    asked = []

    def provider(name, answer):
        def chat_json(*a, **k):
            asked.append(name)
            return answer
        return chat_json
    monkeypatch.setattr(infomaniak, 'chat_json', provider('infomaniak', None))
    monkeypatch.setattr(claude, 'chat_json', provider('claude', None))
    monkeypatch.setattr(llm.Gemini, 'chat_json', provider('gemini', {'score': 5}))
    monkeypatch.setattr(llm.Groq, 'chat_json', provider('groq', {'score': 1}))
    monkeypatch.setenv('LLM_PROVIDERS', 'groq,claude,gemini,infomaniak')
    monkeypatch.setattr(llm.Groq, 'chat_json', provider('groq', None))
    assert llm.chat_json('x', SCHEMA) == {'score': 5}
    assert asked == ['groq', 'claude', 'gemini'], 'infomaniak, last, was not needed'


def test_private_text_never_reaches_gemini_or_groq(monkeypatch, all_configured):
    monkeypatch.setattr(infomaniak, 'chat_json', lambda *a, **k: None)
    monkeypatch.setattr(claude, 'chat_json', lambda *a, **k: None)
    monkeypatch.setattr(llm.Gemini, 'chat_json', lambda *a, **k: pytest.fail('sent to Gemini'))
    monkeypatch.setattr(llm.Groq, 'chat_json', lambda *a, **k: pytest.fail('sent to Groq'))
    assert llm.chat_json('an email', SCHEMA, private=True) is None


def test_task_models_fall_back_to_the_general_one(monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'test-key')
    monkeypatch.setenv('CLAUDE_MODEL', 'general-model')
    monkeypatch.setenv('CLAUDE_MODEL_SCORING', 'scoring-model')
    monkeypatch.delenv('CLAUDE_MODEL_WRITING', raising=False)
    assert claude.get_config('scoring')[1] == 'scoring-model'
    assert claude.get_config('writing')[1] == 'general-model'
    assert claude.get_config()[1] == 'general-model'


def test_the_shared_start_is_marked_for_caching_and_counted(monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'test-key')
    monkeypatch.setenv('CLAUDE_MODEL_SCORING', 'scoring-model')
    client, calls = fake_client('{"score": 6}')
    monkeypatch.setattr(claude, '_get_client', lambda key: client)
    monkeypatch.setattr(claude, 'USAGE', {})
    usage = types.SimpleNamespace(input_tokens=100, cache_creation_input_tokens=0,
                                  cache_read_input_tokens=5000, output_tokens=40)
    original = client.beta.messages.stream

    def stream(**kw):
        s = original(**kw)
        s.message.usage = usage
        return s
    client.beta.messages.stream = stream
    assert claude.chat_json('My CV: ...\nJob: X', SCHEMA, task='scoring', cache_prefix='My CV: ...\n') == {'score': 6}
    blocks = calls[0]['messages'][0]['content']
    assert blocks[0] == {'type': 'text', 'text': 'My CV: ...\n', 'cache_control': {'type': 'ephemeral'}}
    assert blocks[1]['text'] == 'Job: X' and 'cache_control' not in blocks[1]
    assert calls[0]['model'] == 'scoring-model'
    assert claude.USAGE['scoring-model']['cache_read_input_tokens'] == 5000
    # A prefix the prompt does not start with is not used.
    assert claude.content_blocks('Other prompt', 'My CV') == [{'type': 'text', 'text': 'Other prompt'}]


def test_scoring_and_writing_prompts_start_with_the_shared_part():
    import evaluate
    template = evaluate.load_prompt('eval_prompt.txt')
    head = template[:template.index('THE JOB\nJob Title: {job_title}')]
    for job_specific in ('{job_', '{is_promoted}', '{previous_applications}'):
        assert job_specific not in head, 'nothing job-specific before the job'
    gen = evaluate.load_prompt('generator_prompt.txt')
    head = gen[:gen.index("I am applying for the '{title}'")]
    assert '{' not in head.replace('{cv_text}', '').replace('{dossier_text}', '')
