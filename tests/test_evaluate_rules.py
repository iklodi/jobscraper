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
