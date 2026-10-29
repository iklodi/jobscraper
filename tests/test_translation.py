"""Documents in a language picked on the dashboard (Regenerate's dropdowns)."""
import json

import pytest
from docx import Document
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls

import db
import generator


@pytest.fixture
def fake_translator(monkeypatch):
    """Upper-cases every string, keeping [PLACEHOLDERS]; records the prompts."""
    prompts = []

    def ask(prompt, schema):
        prompts.append(prompt)
        items = json.loads(prompt[prompt.index('\n\n["') + 2:])
        return {'translations': [i.upper().replace('[COMPANY]', '[COMPANY]') for i in items]}
    monkeypatch.setattr(generator, 'ask_json', ask)
    return prompts


def test_formatting_stretches_are_kept(fake_translator):
    doc = Document()
    p = doc.add_paragraph()
    p.add_run('Responsibilities:').bold = True
    p.add_run(' advise customers')
    generator.translate_paragraphs([p], 'German')
    assert [(r.text, r.bold) for r in p.runs] == [('RESPONSIBILITIES:', True), (' ADVISE CUSTOMERS', None)]


def test_runs_holding_a_drawing_are_left_alone(fake_translator):
    doc = Document()
    p = doc.add_paragraph()
    p.add_run('Name')
    run = p.add_run()
    run._r.append(parse_xml(f'<w:drawing {nsdecls("w")}/>'))
    generator.translate_paragraphs([p], 'German')
    assert p.runs[0].text == 'NAME'
    assert p.runs[1]._r.find('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}drawing') is not None


def test_a_short_answer_raises_rather_than_half_translating(monkeypatch):
    monkeypatch.setattr(generator, 'ask_json', lambda prompt, schema: {'translations': ['one']})
    with pytest.raises(RuntimeError):
        generator.translate_texts(['one', 'two'], 'German')


def test_english_is_no_translation():
    assert generator.is_english(None) and generator.is_english('English')
    assert not generator.is_english('German')


def letter_template(path):
    doc = Document()
    doc.add_paragraph('[COMPANY] Hiring Team\n[LOCATION]\n[DATE]')
    doc.add_paragraph('Dear hiring team,')
    doc.add_paragraph('x' * 60)
    doc.save(path)


def test_letter_header_follows_the_letter_language(tmp_path, fake_translator):
    base, out = tmp_path / 'base.docx', tmp_path / 'out.docx'
    letter_template(base)
    generator.adapt_cl(str(base), str(out), 'Bonjour,\n\nTexte.', 'Example AG', 'Example AG',
                       'Bern, Switzerland', None, 'French')
    header = Document(out).paragraphs[0].text
    # The company's name is filled in after translation, so it is never sent.
    assert header.startswith('Example AG HIRING TEAM\nBERN, SWITZERLAND')
    assert not any('Example AG' in p for p in fake_translator)


def test_english_letter_header_is_not_translated(tmp_path, fake_translator):
    base, out = tmp_path / 'base.docx', tmp_path / 'out.docx'
    letter_template(base)
    generator.adapt_cl(str(base), str(out), 'Dear Oliver,\n\nText.', 'Example AG', 'Example AG',
                       'Bern, Switzerland', None, 'English')
    assert Document(out).paragraphs[0].text.startswith('Example AG Hiring Team\nBern, Switzerland')
    assert fake_translator == []


def test_requested_letter_language_goes_into_the_prompt(monkeypatch):
    seen = []
    monkeypatch.setattr(generator.infomaniak, 'chat_json',
                        lambda prompt, schema: seen.append(prompt) or {
                            'cv_summary': 's', 'cv_replacements': [], 'cv_removals': [],
                            'cover_letter_body': 'b', 'company_hq': 'h', 'display_company': 'd'})
    job = ('1', 'Role', 'Example AG', 'Bern', 'desc', 'x', 8, '', 'approved', '', 0, '', 0, None, 'French', '')
    generator.generate_tailored_texts(None, None, job, 'cv', 'dossier', None, 'Italian')
    assert 'cover letter in Italian' in seen[0]
    generator.generate_tailored_texts(None, None, job, 'cv', 'dossier')
    assert 'ENTIRELY IN French' in seen[1]


def test_regenerate_passes_the_dropdowns(tmp_path, monkeypatch):
    import app as dashboard
    path = str(tmp_path / 'jobs.db')
    monkeypatch.setattr(db, 'DB_FILE', path)
    monkeypatch.setattr(dashboard, 'DB_FILE', path)
    db.init_db()
    db.add_job('1', 'Role', 'Example AG', '', 'text', 'x')
    calls = []

    class Inline:
        def __init__(self, target, args, kwargs):
            calls.append((args, kwargs))

        def start(self):
            pass
    monkeypatch.setattr('threading.Thread', Inline)
    client = dashboard.app.test_client()
    client.post('/api/jobs/1/regenerate', json={'instructions': '', 'cv_language': 'German',
                                                 'cl_language': 'bad; value'})
    client.post('/api/jobs/1/regenerate', json={'instructions': ''})
    assert calls[0][1]['cv_language'] == 'German' and calls[0][1]['cl_language'] is None
    assert calls[1][1]['cv_language'] is None and calls[1][1]['cl_language'] is None


def test_swiss_german_has_no_eszett(tmp_path):
    path = tmp_path / 'letter.docx'
    doc = Document()
    p = doc.add_paragraph()
    p.add_run('Mit großem Interesse').bold = True
    p.add_run(' und freundlichen Grüßen')
    doc.save(path)
    generator.swiss_spelling(str(path))
    runs = Document(path).paragraphs[0].runs
    assert [(r.text, r.bold) for r in runs] == [('Mit grossem Interesse', True), (' und freundlichen Grüssen', None)]


def test_swiss_places():
    assert generator.is_swiss('Zollikofen, Switzerland') and generator.is_swiss('', 'Bern, Schweiz')
    assert not generator.is_swiss('Berlin, Germany', None)
