"""Search tracks: read from search_criteria.md, filed per job by the scorer."""
import pytest

import db
import evaluate
import scraper

CRITERIA = """# Search Keywords
- Example Keyword @ Switzerland
# Search Locations
- Switzerland
# Job Types
- Full-time
# Tracks
# A comment inside the section.
- Analyst: business analysis roles
- Architect
"""


@pytest.fixture
def cvs(tmp_path, monkeypatch):
    (tmp_path / 'search_criteria.md').write_text(CRITERIA)
    monkeypatch.setenv('CVS_DIR', str(tmp_path))
    return tmp_path


def test_tracks_are_read_and_do_not_leak_into_job_types(cvs):
    assert scraper.get_tracks() == [('Analyst', 'business analysis roles'), ('Architect', '')]
    keywords, locations, job_types = scraper.get_search_criteria()
    assert job_types == ['Full-time']
    assert keywords == [('Example Keyword', ['Switzerland'])]


def test_no_tracks_section_means_no_tracks(tmp_path, monkeypatch):
    (tmp_path / 'search_criteria.md').write_text('# Search Keywords\n- X\n')
    monkeypatch.setenv('CVS_DIR', str(tmp_path))
    assert scraper.get_tracks() == []


@pytest.mark.parametrize('answer, filed', [
    ('Analyst', 'Analyst'), ('  analyst ', 'Analyst'), ('Data Scientist', 'Other'), (None, 'Other'),
])
def test_the_model_cannot_invent_a_track(answer, filed):
    tracks = [('Analyst', ''), ('Architect', '')]
    assert evaluate.normalise_track(answer, tracks) == filed


def test_no_configured_tracks_files_nothing():
    assert evaluate.normalise_track('Analyst', []) is None


def test_same_company_comparison_stays_within_a_track(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_FILE', str(tmp_path / 'jobs.db'))
    db.init_db()
    for job_id, track in [('1', 'Analyst'), ('2', 'Architect'), ('3', None)]:
        db.add_job(job_id, f'Role {job_id}', 'Example AG', '', 'text', 'x')
        db.update_job_score(job_id, 8, 'fit', track=track)
    found = {row[0] for row in db.get_competing_jobs('Example AG', 'new', 'Analyst')}
    assert found == {'1', '3'}          # same track, and the one filed before tracks existed


def test_a_rescore_without_a_track_keeps_the_one_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_FILE', str(tmp_path / 'jobs.db'))
    db.init_db()
    db.add_job('1', 'Role', 'Example AG', '', 'text', 'x')
    db.update_job_score('1', 8, 'fit', track='Analyst')
    db.update_job_score('1', 6, 'again')
    conn = db.get_connection()
    assert conn.execute("SELECT track FROM jobs WHERE job_id = '1'").fetchone()[0] == 'Analyst'
    conn.close()


def test_classify_tracks_files_only_unfiled_jobs_in_the_given_columns(cvs, tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_FILE', str(tmp_path / 'jobs.db'))
    db.init_db()
    for job_id, status in [('1', 'applied'), ('2', 'rejected'), ('3', 'to_apply')]:
        db.add_job(job_id, f'Role {job_id}', 'Example AG', '', 'text', 'x')
        db.update_job_status(job_id, status)
    asked = []
    counts = evaluate.classify_tracks(('applied', 'to_apply'),
                                      ask=lambda prompt: asked.append(prompt) or {'track': 'architect'})
    assert counts == {'Architect': 2} and len(asked) == 2
    assert '- Analyst: business analysis roles' in asked[0]
    conn = db.get_connection()
    assert dict(conn.execute('SELECT job_id, track FROM jobs').fetchall()) == \
        {'1': 'Architect', '2': None, '3': 'Architect'}
    conn.close()
