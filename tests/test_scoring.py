"""Factor scores, the composite built from them, and per-model weight sets: no network."""

import json
import sqlite3

import pytest

import db
import evaluate
import llm
import scoring


@pytest.fixture
def weights(tmp_path, monkeypatch):
    """No candidate file; the repo's defaults with two model sets."""
    monkeypatch.setenv('CVS_DIR', str(tmp_path / 'none'))
    base = {'default': dict(scoring.DEFAULT_WEIGHTS), 'models': {}}
    double = {f: w * 2 for f, w in scoring.DEFAULT_WEIGHTS.items()}
    base['models'] = {'generous-model': dict(scoring.DEFAULT_WEIGHTS), 'strict-model': double}
    monkeypatch.setattr(scoring, 'load_weights', lambda: {
        'default': {f: float(w) for f, w in base['default'].items()}, 'models': base['models']})
    return base


def flat(value):
    return {f: value for f in scoring.FACTORS}


def test_composite_is_the_weighted_mean_on_a_0_10_scale(weights):
    assert scoring.composite(flat(70)) == 7.0
    assert scoring.composite(flat(100)) == 10.0
    assert scoring.composite(dict(flat(0), role_fit=100)) == 2.2, 'role_fit is 22 of 100'
    assert scoring.composite(dict(flat(70), skills=None)) == pytest.approx(6.8), 'missing counts 50'


def test_a_models_own_set_scales_its_composite(weights):
    assert scoring.composite(flat(40), 'strict-model') == 8.0, 'a set summing twice the default doubles it'
    assert scoring.composite(flat(70), 'strict-model') == 10.0, 'never above 10'
    assert scoring.composite(flat(40), 'unknown-model') == 4.0, 'no set: the default weights'


def test_fit_makes_every_models_average_the_same(weights, monkeypatch):
    monkeypatch.setattr(scoring, 'load_weights', lambda: {
        'default': {f: float(w) for f, w in scoring.DEFAULT_WEIGHTS.items()}, 'models': {}})
    results = {'high': [flat(80), flat(90)], 'low': [flat(40), flat(50)]}
    sets, means, _ = scoring.fit(results)
    loaded = {'default': {f: float(w) for f, w in scoring.DEFAULT_WEIGHTS.items()}, 'models': sets}
    high = [scoring.composite(r, 'high', loaded) for r in results['high']]
    low = [scoring.composite(r, 'low', loaded) for r in results['low']]
    assert sum(high) / 2 == pytest.approx(sum(low) / 2, abs=0.01)
    assert means['high']['role_fit'] == 85


def test_the_candidates_file_overrides_the_repos(tmp_path, monkeypatch):
    (tmp_path / 'scoring_weights.yaml').write_text(
        'default:\n  role_fit: 50\nmodels:\n  some-model:\n    salary: 0\n')
    monkeypatch.setenv('CVS_DIR', str(tmp_path))
    loaded = scoring.load_weights()
    assert loaded['default']['role_fit'] == 50 and loaded['default']['skills'] == 10
    assert scoring.weights_for('some-model', loaded)['salary'] == 0
    assert scoring.weights_for('some-model', loaded)['role_fit'] == 50


def answer(**extra):
    return dict({'factors': flat(80), 'rule_reason': None, 'overall': 8,
                 'reasoning': 'Strong fit.', 'work_arrangement': 'remote', 'country': None,
                 'region': None}, **extra)


@pytest.fixture
def scorer(weights, monkeypatch):
    monkeypatch.setattr(evaluate, 'get_tracks', lambda: [])
    for name in ('HOME_COUNTRY', 'ONSITE_REGIONS', 'HYBRID_COUNTRIES', 'RELOCATION_COUNTRIES'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(llm, 'answered_by', lambda: ('claude', 'strict-model'))

    def run(reply):
        monkeypatch.setattr(evaluate, '_ask_evaluator', lambda *a, **k: reply)
        return evaluate.evaluate_job(None, None, 'T', 'C', 'L', 'D', 'cv', [], '', False)
    return run


def test_the_score_is_the_composite_of_the_answering_models_weights(scorer):
    result = scorer(answer(factors=flat(45)))
    assert result['score'] == 9.0 and result['scored_by'] == 'strict-model'
    assert result['factors'] == flat(45)


def test_only_a_hard_rule_rules_a_job_out(scorer, monkeypatch):
    assert scorer(answer(factors=flat(45), rule_reason='CTO role - not wanted'))['score'] == 9.0, \
        'other rules are factors'
    result = scorer(answer(rule_reason='Already applied'))
    assert result['score'] == 1.0 and result['reasoning'] == 'Already applied. Strong fit.'
    monkeypatch.setenv('HARD_RULES', '')
    assert scorer(answer(factors=flat(45), rule_reason='Already applied'))['score'] == 9.0


def test_a_broken_location_rule_zeroes_the_location_factor_only(scorer, monkeypatch):
    monkeypatch.setenv('HOME_COUNTRY', 'Switzerland')
    result = scorer(answer(factors=flat(45), work_arrangement='on-site', country='Spain'))
    assert result['factors']['location'] == 0 and result['factors']['role_fit'] == 45
    # strict-model doubles: 45 x 2 on 80 of the 100 default weight points.
    assert result['score'] == 7.2
    assert result['reasoning'].startswith('Location mismatch: on-site in Spain: location 0.')


def test_fit_matches_the_pass_share_at_one_mark(weights, monkeypatch):
    monkeypatch.setattr(scoring, 'load_weights', lambda: {
        'default': {f: float(w) for f, w in scoring.DEFAULT_WEIGHTS.items()}, 'models': {}})
    results = {'a': [flat(v) for v in (90, 80, 70, 60)], 'b': [flat(v) for v in (50, 45, 30, 20)]}
    sets, _, bar = scoring.fit(results, pass_share=0.5)
    loaded = {'default': {f: float(w) for f, w in scoring.DEFAULT_WEIGHTS.items()}, 'models': sets}
    for m, rows in results.items():
        assert sum(scoring.composite(r, m, loaded) >= bar - 0.01 for r in rows) == 2


def test_the_prompt_caches_everything_before_the_job(monkeypatch):
    seen = {}

    def chat_json(prompt, schema, **kw):
        seen.update(kw, prompt=prompt)
        return answer()
    monkeypatch.setattr(llm, 'chat_json', chat_json)
    monkeypatch.setattr(evaluate, 'get_tracks', lambda: [('Architect', 'designs systems')])
    evaluate._ask_evaluator(None, None, 'Job X', 'Firm Y', 'Town', 'Advert text', 'MY CV', ['(none)'],
                            'MY RULES', False)
    prefix = seen['cache_prefix']
    assert seen['prompt'].startswith(prefix)
    assert 'MY CV' in prefix and 'MY RULES' in prefix and '"role_fit"' in prefix
    assert 'Job X' not in prefix and 'Firm Y' not in prefix and 'Advert text' not in prefix
    assert '{' + 'factors}' not in seen['prompt']


def test_scores_keep_their_decimals_in_the_database(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_FILE', str(tmp_path / 'jobs.db'))
    monkeypatch.setenv('MIN_PASS_SCORE', '6.5')
    db.init_db()
    conn = sqlite3.connect(db.DB_FILE)
    conn.execute("INSERT INTO jobs (job_id, title, status) VALUES ('1', 'T', 'new'), ('2', 'U', 'new')")
    conn.commit()
    conn.close()
    db.update_job_score('1', 6.57, 'ok', factors=flat(66), scored_by='some-model')
    db.update_job_score('2', 6.43, 'no')
    conn = sqlite3.connect(db.DB_FILE)
    rows = dict((r[0], r[1:]) for r in conn.execute('SELECT job_id, score, status, factor_scores, scored_by FROM jobs'))
    conn.close()
    assert rows['1'][:2] == (6.57, 'to_apply') and json.loads(rows['1'][2]) == flat(66)
    assert rows['1'][3] == 'some-model'
    assert rows['2'][:2] == (6.43, 'scored')


def test_old_salary_estimates_get_an_annual_figure(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_FILE', str(tmp_path / 'jobs.db'))
    db.init_db()
    conn = sqlite3.connect(db.DB_FILE)
    conn.execute("INSERT INTO jobs (job_id, status, estimated_salary) VALUES "
                 "('1', 'to_apply', '800-1,000 EUR per day'), ('2', 'to_apply', 'Unknown'), "
                 "('3', 'scored', '100k EUR')")
    conn.commit()
    conn.close()
    prompts = []

    def ask(prompt):
        prompts.append(prompt)
        return {'figures': [{'id': '1', 'annual': 180000}, {'id': '9', 'annual': 5}]}
    assert evaluate.fill_salary_figures(('to_apply',), ask=ask) == 1
    assert '1: 800-1,000 EUR per day' in prompts[0] and 'Unknown' not in prompts[0]
    conn = sqlite3.connect(db.DB_FILE)
    assert dict(conn.execute('SELECT job_id, salary_annual FROM jobs')) == {'1': 180000, '2': None, '3': None}
    conn.close()
