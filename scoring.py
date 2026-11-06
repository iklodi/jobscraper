"""Multi-factor job scores and the composite built from them.

The scorer rates every job on several factors, each 0-100, in the same single
model turn that reports the location facts and the salary - no extra calls.
The composite that goes on the board (0-10, two decimals) is computed here:

    composite = sum(weight[f] * factor[f]) / sum(default weight[f]) / 10

Nothing caps it: a weak factor is outweighed by strong ones. The location
rules set the location factor (evaluate.apply_factors), and only a reason in
HARD_RULES ("Already applied") rules a job out.

Weights come from `scoring_weights.yaml` (this folder), overridden key by key
by `CVS_DIR/scoring_weights.yaml` when that exists:

    default:            # how much each factor matters
      role_fit: 25
      ...
    models:             # per-model sets, for the model that answered
      claude-sonnet-5-5:
        role_fit: 31.2
        ...

Models do not grade alike: one's 70 is another's 85. A model's own set is
the default weights times (all models' mean for the factor / this model's
mean), measured on the same jobs, so the composite comes out about the same
whichever model answered. Its total is divided by the *default* total, so a
set that sums higher lifts that model's composites - that is the calibration.
Each set is then scaled as a whole so every model passes the same share of
the comparison's jobs at one pass mark. `python scoring.py --fit results.json
--pass-share 0.5` computes the sets (and that mark) from a comparison.
"""

import json
import os

FACTORS = {
    'role_fit': "how well the job's actual function matches what I have done and one of my search tracks",
    'seniority': 'whether the level (scope, reporting line, team and budget) matches mine: 100 = my level; '
                 'lower when the role is clearly junior to me or beyond my experience',
    'experience': 'how well my years and kind of experience meet what the advert asks for',
    'skills': 'how many of the required skills, tools, methods and certifications I have',
    'industry': "how well I know the employer's sector and domain",
    'location': 'how good the place and work arrangement are for me under my location rules '
                '(100 = near home or genuinely open remote; low = a move I would not make)',
    'salary': 'the likely pay against my level and my bars (50 when it cannot be judged)',
    'language': 'whether I meet the language requirements (100 = all met; 0 = a required language I lack)',
    'employer': "the employer's standing: reputation, stability, a direct employer rather than an agency, "
                'a clear and credible advert',
    'growth': 'what the role adds to my career: scope, impact, visibility, learning',
}

DEFAULT_WEIGHTS = {'role_fit': 22, 'seniority': 13, 'experience': 10, 'skills': 10, 'industry': 5,
                   'location': 20, 'salary': 8, 'language': 6, 'employer': 3, 'growth': 3}

HERE = os.path.dirname(os.path.abspath(__file__))
WEIGHTS_FILE = 'scoring_weights.yaml'


def _read(path):
    if not os.path.exists(path):
        return {}
    import yaml
    with open(path, encoding='utf-8') as f:
        return yaml.safe_load(f) or {}


def load_weights():
    """{'default': {...}, 'models': {model: {...}}} - the repo's file, then the
    candidate's own (CVS_DIR) on top."""
    base = _read(os.path.join(HERE, WEIGHTS_FILE))
    own = _read(os.path.join(os.environ.get('CVS_DIR', 'cvs'), WEIGHTS_FILE))
    default = {**DEFAULT_WEIGHTS, **(base.get('default') or {}), **(own.get('default') or {})}
    models = dict(base.get('models') or {})
    for model, weights in (own.get('models') or {}).items():
        models[model] = dict(models.get(model) or {}, **(weights or {}))
    return {'default': {f: float(default.get(f, 0) or 0) for f in FACTORS}, 'models': models}


def weights_for(model, weights=None):
    """The weight set for `model` (its own, else the default), every factor present."""
    weights = weights or load_weights()
    own = weights['models'].get(model) or {}
    return {f: float(own.get(f, weights['default'][f]) or 0) for f in FACTORS}


def clean_factors(raw):
    """The factors as integers 0-100; one missing or unreadable counts 50."""
    out = {}
    for name in FACTORS:
        value = (raw or {}).get(name)
        try:
            out[name] = max(0, min(100, int(round(float(value)))))
        except (TypeError, ValueError):
            out[name] = 50
    return out


def composite(factors, model=None, weights=None):
    """The 0-10 score, two decimals, from factors (0-100) under `model`'s weights."""
    weights = weights or load_weights()
    total = sum(weights['default'].values()) or 1
    used = weights_for(model, weights)
    factors = clean_factors(factors)
    value = sum(used[f] * factors[f] for f in FACTORS) / total / 10
    return round(max(0.0, min(10.0, value)), 2)


def schema():
    """The factors' part of the evaluator's JSON schema."""
    return {'type': 'object',
            'properties': {f: {'type': 'integer'} for f in FACTORS},
            'required': list(FACTORS)}


def prompt_lines():
    """The factor list for the prompt."""
    return '\n'.join(f'- "{name}": {about}' for name, about in FACTORS.items())


def fit(results, pass_share=None, reference=None):
    """Per-model weight sets from a comparison: {model: [factors, ...]}, the
    same jobs for every model.

    1. Each factor is put on a common scale: the default weight times the
       reference mean over this model's mean (the reference: every model's
       average, or one model's).
    2. With `pass_share` (e.g. 0.51: the share of these jobs that passed
       before), each set is then scaled as a whole so that exactly that share
       of the model's jobs reaches one common pass mark - the average of the
       models' own marks for that share, so the scale barely moves.

    Returns ({model: weights}, {model: {factor: mean}}, pass mark or None)."""
    weights = load_weights()
    default = weights['default']
    means = {m: {f: sum(r[f] for r in rows) / len(rows) for f in FACTORS}
             for m, rows in results.items() if rows}
    if reference:
        target = means[reference]
    else:
        target = {f: sum(m[f] for m in means.values()) / len(means) for f in FACTORS}
    sets = {m: {f: default[f] * (target[f] / mean[f] if mean[f] else 1) for f in FACTORS}
            for m, mean in means.items()}
    bar = None
    if pass_share:
        marks = {}
        for m, rows in results.items():
            scored = sorted((composite(r, m, {'default': default, 'models': sets}) for r in rows), reverse=True)
            marks[m] = scored[max(0, min(len(scored), round(pass_share * len(scored))) - 1)]
        bar = round(sum(marks.values()) / len(marks), 2)
        sets = {m: {f: w * bar / marks[m] for f, w in ws.items()} if marks[m] else ws
                for m, ws in sets.items()}
    sets = {m: {f: round(w, 2) for f, w in ws.items()} for m, ws in sets.items()}
    return sets, means, bar


if __name__ == '__main__':
    import sys
    if '--fit' in sys.argv:
        # results.json: {model: [factors, ...]}; --pass-share 0.5 to match a pass rate.
        data = json.load(open(sys.argv[sys.argv.index('--fit') + 1]))
        share = float(sys.argv[sys.argv.index('--pass-share') + 1]) if '--pass-share' in sys.argv else None
        sets, means, bar = fit(data, share)
        import yaml
        if bar:
            print(f'# MIN_PASS_SCORE={bar}')
        print(yaml.safe_dump({'models': sets}, sort_keys=False))
    else:
        print(json.dumps(load_weights(), indent=1))
