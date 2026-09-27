"""Employers' ATS feeds (issue #22), against anonymised copies of each feed."""
import json
import pathlib

import pytest

import ats_feeds
import db

FIX = pathlib.Path(__file__).parent / 'fixtures' / 'ats'
CRITERIA = ([('Enterprise Architect', None), ('Head of AI', None), ('AI Consultant', None),
             ('Interim Manager', None), ('Program Manager', None), ('Business Transformation', None)],
            ['Switzerland'], [])

FEEDS = {
    'https://boards-api.greenhouse.io/v1/boards/example/jobs?content=true': 'greenhouse.json',
    'https://api.lever.co/v0/postings/example?mode=json': 'lever.json',
    'https://api.ashbyhq.com/posting-api/job-board/example': 'ashby.json',
    'https://example.jobs.personio.de/xml': 'personio.xml',
    'https://api.smartrecruiters.com/v1/companies/example/postings?limit=100&offset=0': 'smartrecruiters_list.json',
    'https://api.smartrecruiters.com/v1/companies/example/postings/900': 'smartrecruiters_detail.json',
    'https://example.recruitee.com/api/offers/': 'recruitee.json',
}


class Response:
    def __init__(self, body):
        self.content = body.encode()
        self.status_code = 200

    def json(self):
        return json.loads(self.content)

    def raise_for_status(self):
        pass


class FakeSession:
    def __init__(self):
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        if url not in FEEDS:
            raise RuntimeError(f'unexpected request {url}')
        return Response((FIX / FEEDS[url]).read_text(encoding='utf-8'))


@pytest.fixture
def scratch_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_FILE', str(tmp_path / 'jobs.db'))
    db.init_db()


def rows():
    conn = db.get_connection()
    out = {r[0]: r[1:] for r in conn.execute(
        "SELECT job_id, title, company, location, link, apply_url, source, status, length(description) "
        "FROM jobs WHERE job_id LIKE 'ats-%'")}
    conn.close()
    return out


@pytest.mark.parametrize('ats', sorted(ats_feeds.ADAPTERS))
def test_each_feed_yields_matching_jobs_with_employer_links(scratch_db, ats):
    company = {'name': 'Example AG', 'ats': ats, 'board': 'example'}
    stats, problems = ats_feeds.ingest(CRITERIA, companies=[company], session=FakeSession(),
                                       sleep=lambda s: None)
    assert problems == []
    assert stats == {'Example AG (ATS)': 1}
    (job_id, row), = rows().items()
    title, company_name, location, link, apply_url, source, status, length = row
    assert job_id.startswith(f'ats-{ats}-example-')
    assert company_name == 'Example AG' and source == 'ats: Example AG' and status == 'new'
    assert apply_url.startswith('https://') and 'linkedin' not in apply_url
    assert length >= 200


def test_greenhouse_filters_by_title_and_location(scratch_db):
    session = FakeSession()
    ats_feeds.ingest(CRITERIA, companies=[{'name': 'Example AG', 'ats': 'greenhouse', 'board': 'example'}],
                     session=session, sleep=lambda s: None)
    # 101 matches; 102 is in India, 103 is the wrong role.
    assert list(rows()) == ['ats-greenhouse-example-101']
    description = db.get_connection().execute(
        "SELECT description FROM jobs WHERE job_id = 'ats-greenhouse-example-101'").fetchone()[0]
    assert description.startswith('About the role\nDesign the target architecture')
    assert '<' not in description and '&lt;' not in description


def test_a_second_run_adds_nothing_and_skips_known_details(scratch_db):
    company = [{'name': 'Example AG', 'ats': 'smartrecruiters', 'board': 'example'}]
    ats_feeds.ingest(CRITERIA, companies=company, session=FakeSession(), sleep=lambda s: None)
    session = FakeSession()
    stats, _ = ats_feeds.ingest(CRITERIA, companies=company, session=session, sleep=lambda s: None)
    assert stats == {'Example AG (ATS)': 0}
    assert not any(u.endswith('/postings/900') for u in session.urls)


def test_a_broken_feed_is_reported_not_fatal(scratch_db):
    stats, problems = ats_feeds.ingest(
        CRITERIA, companies=[{'name': 'Gone AG', 'ats': 'lever', 'board': 'gone'},
                             {'name': 'Odd AG', 'ats': 'nosuchats', 'board': 'x'}],
        session=FakeSession(), sleep=lambda s: None)
    assert len(problems) == 2 and 'Gone AG' in problems[0] and 'nosuchats' in problems[1]


@pytest.mark.parametrize('url, expected', [
    ('https://job-boards.greenhouse.io/example/jobs/1', ('greenhouse', 'example')),
    ('https://boards.greenhouse.io/example', ('greenhouse', 'example')),
    ('https://jobs.lever.co/example/abc', ('lever', 'example')),
    ('https://jobs.ashbyhq.com/Example%20Co/xyz', ('ashby', 'Example Co')),
    ('https://example.jobs.personio.de/job/5', ('personio', 'example')),
    ('https://jobs.smartrecruiters.com/ExampleGroup/900-x', ('smartrecruiters', 'ExampleGroup')),
    ('https://example.recruitee.com/o/lead', ('recruitee', 'example')),
    ('https://careers.example.com/jobs', None),
])
def test_detect_reads_ats_and_board_from_a_link(url, expected):
    assert ats_feeds.detect(url) == expected


@pytest.mark.parametrize('title, keywords, expected', [
    ('Head of AI & Data', ['Head of AI'], True),
    ('Senior Program Manager (m/w/d)', ['Program Manager'], True),
    ('Manager, AI Platform', ['AI Manager'], True),
    ('Sales Manager', ['AI Manager'], False),
])
def test_title_matching(title, keywords, expected):
    assert ats_feeds.title_matches(title, keywords) is expected


@pytest.mark.parametrize('location, wanted, expected', [
    ('Zürich, ZH, Switzerland', ['Switzerland'], True),
    ('Munich, Germany', ['Switzerland'], False),
    ('Remote, Germany', ['Europe'], True),
    ('Bangalore, India', ['Europe'], False),
    ('New York', ['Worldwide'], True),
    ('Remote, United States', ['Remote'], True),
])
def test_location_matching(location, wanted, expected):
    assert ats_feeds.location_matches(location, wanted) is expected
