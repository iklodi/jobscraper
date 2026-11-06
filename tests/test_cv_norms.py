"""Regional CV norms: which apply to a job, and what they change in the CV."""
import base64
import io

import pytest
import PyPDF2
from docx import Document

import cv_norms
import generator

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==')


@pytest.mark.parametrize('location, region, photo', [
    ('London, United Kingdom', 'uk_ireland', 'no'),
    ('New York, USA', 'north_america', 'no'),
    ('Zurich, Switzerland', 'dach', 'expected'),
    ('Amsterdam, Netherlands', 'southern_europe_benelux', 'optional'),
    ('Paris, France', 'southern_europe_benelux', 'expected'),
    ('Dubai, UAE', 'middle_east', 'expected'),
    ('Tokyo, Japan', 'japan', 'mandatory'),
])
def test_the_jobs_country_picks_the_norms(location, region, photo):
    norms = cv_norms.for_job(location, home='')
    assert (norms['region'], norms['photo']) == (region, photo)


def test_the_first_place_naming_a_country_decides():
    # The scorer's country, then the location line, then the addressed office.
    assert cv_norms.for_job('United Kingdom', 'London (Hybrid)', home='switzerland')['region'] == 'uk_ireland'
    assert cv_norms.for_job(None, 'London (Hybrid)', 'London, United Kingdom', home='switzerland')['region'] == 'uk_ireland'


def test_an_unknown_place_falls_back_to_the_home_country():
    assert cv_norms.for_job('Lausanne', home='switzerland')['region'] == 'dach'
    assert cv_norms.for_job('Lausanne', home='') is None


def test_a_us_employer_hiring_in_english_keeps_its_photo_free_standard():
    norms = cv_norms.for_job('Berlin, Germany', hq_country='United States', language='English', home='')
    assert norms['photo'] == 'no' and 'careers page' in norms['reasons'][0]
    # In German, or for a German employer, the local standard holds.
    assert cv_norms.for_job('Berlin, Germany', hq_country='United States', language='German', home='')['photo'] == 'expected'
    assert cv_norms.for_job('Berlin, Germany', hq_country='Germany', language='English', home='')['photo'] == 'expected'


def test_page_target_never_exceeds_the_template():
    assert cv_norms.page_target(cv_norms.for_job('London, UK', home=''), 3) == 2
    assert cv_norms.page_target(cv_norms.for_job('Sydney, Australia', home=''), 3) == 3
    assert cv_norms.page_target(None, 3) == 3


def test_the_prompt_gets_the_regions_instructions():
    block = cv_norms.prompt_block(cv_norms.for_job('Chicago, United States', home=''), 2)
    assert 'résumé' in block and 'fit on 2 pages' in block and 'age, date of birth' in block
    assert cv_norms.prompt_block(None) == ''


def cv(tmp_path, *lines, photo=True):
    doc = Document()
    first = doc.add_paragraph('Jane Doe')
    if photo:
        first.add_run().add_picture(io.BytesIO(PNG))
    for line in lines:
        doc.add_paragraph(line)
    doc.add_paragraph('')
    doc.add_paragraph('Summary ' * 20)
    path = tmp_path / 'cv.docx'
    doc.save(path)
    return str(path)


def texts(path):
    return [p.text for p in Document(path).paragraphs if p.text.strip()]


def test_uk_cv_loses_its_photo_and_date_of_birth(tmp_path):
    path = cv(tmp_path, '+41 12 345 67 89', 'Date of birth: 01.01.1990', 'Example (Switzerland)')
    done = generator.apply_cv_norms(path, cv_norms.for_job('London, UK', home=''))
    assert done == ['photo removed', 'date of birth left out']
    assert not generator._pictures(Document(path))
    assert 'Date of birth: 01.01.1990' not in texts(path)


def test_dach_cv_keeps_its_photo_or_flags_its_absence(tmp_path):
    norms = cv_norms.for_job('Zurich, Switzerland', home='')
    path = cv(tmp_path, 'Date of birth: 01.01.1990')
    assert generator.apply_cv_norms(path, norms) == []
    assert generator._pictures(Document(path)) and 'Date of birth: 01.01.1990' in texts(path)
    bare = cv(tmp_path, 'x', photo=False)
    assert generator.apply_cv_norms(bare, norms) == ['a photo is expected there, and the CV template has none']


def test_gulf_cv_states_nationality_from_the_profile(tmp_path):
    path = cv(tmp_path, '+41 12 345 67 89', 'Work permit: C')
    profile = {'work_authorization': {'nationality': 'Examplish'}}
    done = generator.apply_cv_norms(path, cv_norms.for_job('Dubai, UAE', home=''), profile)
    assert 'nationality added from profile.yaml' in done
    assert texts(path)[:4] == ['Jane Doe', '+41 12 345 67 89', 'Work permit: C', 'Nationality: Examplish']


def test_australian_cv_ends_with_referees(tmp_path):
    path = cv(tmp_path, 'x', photo=False)
    generator.apply_cv_norms(path, cv_norms.for_job('Sydney, Australia', home=''))
    assert texts(path)[-1] == 'Referees available upon request.'


def test_japan_says_what_is_not_generated(tmp_path):
    done = generator.apply_cv_norms(cv(tmp_path, 'x'), cv_norms.for_job('Tokyo, Japan', home=''))
    assert any('Rirekisho' in line for line in done)


def pdf(path, pages):
    writer = PyPDF2.PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=100, height=100)
    with open(path, 'wb') as f:
        writer.write(f)
    return str(path)


def test_the_dossier_joins_letter_cv_and_chosen_attachments(tmp_path, monkeypatch):
    monkeypatch.setattr(generator, 'CVS_DIR', str(tmp_path))
    (tmp_path / 'attach').mkdir()
    pdf(tmp_path / 'attach' / 'diplomas.pdf', 3)
    pdf(tmp_path / 'attach' / 'reference.pdf', 1)
    profile = {'attachments': {'diplomas': 'attach/diplomas.pdf', 'reference_letter': 'attach/reference.pdf'},
               'dossier': ['diplomas']}
    out = tmp_path / 'dossier.pdf'
    included = generator.build_dossier(str(out), pdf(tmp_path / 'cl.pdf', 1), pdf(tmp_path / 'cv.pdf', 2), profile)
    assert included == ['diplomas'] and len(PyPDF2.PdfReader(str(out)).pages) == 6
    del profile['dossier']                         # default: every attachment
    assert generator.build_dossier(str(out), str(tmp_path / 'cl.pdf'), str(tmp_path / 'cv.pdf'),
                                   profile) == ['diplomas', 'reference_letter']
