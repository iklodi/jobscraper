"""The scorer's hard location rules and its list of past applications."""
import pytest

import db
import evaluate


@pytest.fixture
def rules(monkeypatch):
    monkeypatch.setenv('ONSITE_CANTONS', 'VD,VS,FR')
    monkeypatch.setenv('HYBRID_COUNTRIES', 'Switzerland')


@pytest.mark.parametrize('facts, broken', [
    ({'work_arrangement': 'on-site', 'country': 'Switzerland', 'canton': 'GE'}, True),
    ({'work_arrangement': 'on-site', 'country': 'Switzerland', 'canton': 'VD'}, False),
    ({'work_arrangement': 'on-site', 'country': 'Switzerland', 'canton': None}, True),
    ({'work_arrangement': 'hybrid', 'country': 'Switzerland', 'canton': 'ZH'}, False),
    ({'work_arrangement': 'hybrid', 'country': 'Germany', 'canton': None}, True),
    ({'work_arrangement': 'remote', 'country': 'United Kingdom', 'canton': None}, False),
    ({'work_arrangement': 'unknown', 'country': None, 'canton': None}, False),
    ({}, False),                                     # a model that sent no facts
])
def test_location_gate(rules, facts, broken):
    assert bool(evaluate.location_gate(facts)) is broken


def test_no_settings_no_rule(monkeypatch):
    monkeypatch.delenv('ONSITE_CANTONS', raising=False)
    monkeypatch.delenv('HYBRID_COUNTRIES', raising=False)
    assert evaluate.location_gate({'work_arrangement': 'on-site', 'canton': 'GE'}) is None


def test_a_broken_rule_caps_the_score_whatever_the_model_said(rules, monkeypatch):
    monkeypatch.setattr(evaluate, '_ask_evaluator', lambda *a, **k: {
        'score': 9, 'reasoning': 'Exceptional fit.', 'work_arrangement': 'on-site',
        'country': 'Switzerland', 'canton': 'GE'})
    result = evaluate.evaluate_job(None, None, 'Role', 'Example AG', 'Geneva (On-site)', 'text',
                                   'cv', [], 'rules', False)
    assert result['score'] == 2
    assert result['reasoning'].startswith('Location mismatch: on-site in GE. Exceptional fit.')


def test_only_real_applications_at_the_same_company_are_listed(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_FILE', str(tmp_path / 'jobs.db'))
    db.init_db()
    for job_id, company, status in [('1', 'Example AG', 'applied'), ('2', 'Example AG', 'rejected'),
                                    ('3', 'Other AG', 'applied'), ('4', 'Example AG', 'to_apply')]:
        db.add_job(job_id, f'Role {job_id}', company, '', 'text', 'x')
        if status == 'rejected':
            db.update_job_status(job_id, 'applied')      # applied, then declined
        db.update_job_status(job_id, status)
    db.add_job('5', 'Role 5', 'Example AG', '', 'text', 'x')
    db.update_job_status('5', 'rejected')                # rejected by hand: never applied
    previous = evaluate.get_previous_applications()
    assert sorted(previous) == [('Example AG', 'Role 1'), ('Example AG', 'Role 2'), ('Other AG', 'Role 3')]
    assert evaluate.applications_at('Example AG', previous) == ['Example AG - Role 1', 'Example AG - Role 2']
    assert evaluate.applications_at('Unrelated SA', previous) == ['(none at this company)']


@pytest.fixture
def relocation(rules, monkeypatch):
    monkeypatch.setenv('RELOCATION_COUNTRIES', 'Germany,United Kingdom,United Arab Emirates')
    monkeypatch.setenv('RELOCATION_MIN_SALARY_CHF', '150000')


@pytest.mark.parametrize('facts, broken', [
    # Abroad, in a country worth a move: the pay decides.
    ({'work_arrangement': 'on-site', 'country': 'Germany', 'salary_chf_annual': 160000}, False),
    ({'work_arrangement': 'hybrid', 'country': 'United Kingdom', 'salary_chf_annual': 150000}, False),
    ({'work_arrangement': 'on-site', 'country': 'Germany', 'salary_chf_annual': 120000}, True),
    ({'work_arrangement': 'on-site', 'country': 'United Arab Emirates', 'salary_chf_annual': None}, True),
    # Not on the list: no pay is enough.
    ({'work_arrangement': 'on-site', 'country': 'Singapore', 'salary_chf_annual': 400000}, True),
    # The ordinary rules still apply at home.
    ({'work_arrangement': 'on-site', 'country': 'Switzerland', 'canton': 'VD'}, False),
])
def test_a_move_abroad_needs_the_pay(relocation, facts, broken):
    assert bool(evaluate.location_gate(facts)) is broken


def test_the_reason_says_what_pay_was_needed(relocation):
    reason = evaluate.location_gate({'work_arrangement': 'on-site', 'country': 'Germany',
                                     'salary_chf_annual': 120000})
    assert reason == 'Location mismatch: on-site in Germany, worth a move only from CHF 150,000 a year (estimated: CHF 120,000)'


@pytest.fixture
def penalties(relocation, monkeypatch):
    monkeypatch.setenv('RELOCATION_PENALTY', '1')
    monkeypatch.setenv('HOME_CANTONS', 'VD,VS,FR')
    monkeypatch.setenv('SWISS_MOVE_PENALTY', '0')


@pytest.mark.parametrize('facts, points', [
    ({'work_arrangement': 'hybrid', 'country': 'United Kingdom'}, 1),
    ({'work_arrangement': 'on-site', 'country': 'United Arab Emirates'}, 1),
    ({'work_arrangement': 'unknown', 'country': 'Germany'}, 1),
    ({'work_arrangement': 'remote', 'country': 'United Kingdom'}, 0),     # no move
    ({'work_arrangement': 'hybrid', 'country': 'Switzerland', 'canton': 'ZH'}, 0),
    ({'work_arrangement': 'on-site', 'country': 'Switzerland', 'canton': 'VD'}, 0),
])
def test_a_move_abroad_costs_points(penalties, facts, points):
    assert evaluate.location_penalty(facts)[0] == points


def test_a_move_within_switzerland_can_cost_points_too(penalties, monkeypatch):
    monkeypatch.setenv('SWISS_MOVE_PENALTY', '1')
    assert evaluate.location_penalty({'work_arrangement': 'hybrid', 'country': 'Switzerland',
                                      'canton': 'ZH'})[0] == 1
    assert evaluate.location_penalty({'work_arrangement': 'hybrid', 'country': 'Switzerland',
                                      'canton': 'VD'})[0] == 0


def test_the_penalty_comes_off_the_score_and_says_so(penalties, monkeypatch):
    monkeypatch.setattr(evaluate, '_ask_evaluator', lambda *a, **k: {
        'score': 9, 'reasoning': 'Strong fit.', 'work_arrangement': 'hybrid',
        'country': 'United Kingdom', 'salary_chf_annual': 200000})
    monkeypatch.setattr(evaluate, 'get_tracks', lambda: [])
    result = evaluate.evaluate_job(None, None, 'Role', 'Example Ltd', 'London', 'text', 'cv', [], 'rules', False)
    assert result['score'] == 8
    assert result['reasoning'] == 'A move to United Kingdom: -1 (fit 9). Strong fit.'


def test_a_broken_rule_is_not_penalised_twice(penalties):
    result = evaluate.apply_location_rules({'score': 8, 'reasoning': '', 'work_arrangement': 'on-site',
                                            'country': 'Germany', 'salary_chf_annual': 100000})
    assert result['score'] == 2


def test_jobs_already_scored_get_the_penalty_once(penalties, tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_FILE', str(tmp_path / 'jobs.db'))
    monkeypatch.setenv('MIN_PASS_SCORE', '7')
    db.init_db()
    for job_id, score in [('uk9', 9), ('uk7', 7), ('ch9', 9)]:
        db.add_job(job_id, 'Role', 'Example', '', 'text', 'x')
        db.update_job_score(job_id, score, 'Fit.')
    facts = {'uk9': ('hybrid', 'United Kingdom', None), 'uk7': ('on-site', 'United Kingdom', None),
             'ch9': ('hybrid', 'Switzerland', 'VD')}
    order = iter(['uk9', 'uk7', 'ch9'])
    ask = lambda prompt: dict(zip(('work_arrangement', 'country', 'canton'), facts[next(order)]))
    assert evaluate.apply_move_penalty(ask=ask) == {'checked': 3, 'lowered': 2, 'dropped': 1}
    conn = db.get_connection()
    rows = dict((r[0], r[1:]) for r in conn.execute('SELECT job_id, score, status, country FROM jobs'))
    conn.close()
    assert rows == {'uk9': (8, 'to_apply', 'United Kingdom'), 'uk7': (6, 'scored', 'United Kingdom'),
                    'ch9': (9, 'to_apply', 'Switzerland')}
    # A second run finds nothing left to do.
    assert evaluate.apply_move_penalty(ask=lambda p: 1 / 0) == {'checked': 0, 'lowered': 0, 'dropped': 0}
