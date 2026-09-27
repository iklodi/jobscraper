"""Jobs from employers' own applicant-tracking systems (issue #22).

Many employers publish their openings through an ATS that exposes a public,
documented feed meant to be read by programs. Reading it needs no browser and
no account, it never touches LinkedIn, and it carries the employer's own
application URL - which safe mode cannot get from LinkedIn (#15). So jobs from
here can be filled in either mode.

Feeds are per company, not per search: the candidate keeps a watch list in
CVS_DIR/companies.yaml (never the repo):

    companies:
      - name: Example AG
        ats: greenhouse          # greenhouse, lever, ashby, personio,
        board: example           #   smartrecruiters or recruitee
        keywords: [Architect]    # optional; default: search_criteria.md
        locations: [Zurich]      # optional; default: search_criteria.md

`python ats_feeds.py --detect <careers page URL>` works out ats and board from
a link. Each feed is read once per run, and only postings whose title matches a
keyword and whose location matches a location are stored.
"""

import html
import os
import random
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET

import requests
import yaml
from lxml import html as lxml_html

import db
from dedupe import already_listed

USER_AGENT = 'Mozilla/5.0 (compatible; personal job search)'
COMPANY_GAP = (1.0, 2.5)          # seconds between companies

# Words that do not decide whether a title matches a keyword.
STOPWORDS = {'of', 'the', 'and', 'for', 'in', 'a', 'an', 'de', 'la', 'le', 'des', 'du',
             'et', 'für', 'und', 'der', 'die', 'das'}

# Locations as written in search_criteria.md -> names a feed may use instead.
LOCATION_ALIASES = {
    'switzerland': ['switzerland', 'schweiz', 'suisse', 'svizzera', 'zurich', 'zürich',
                    'geneva', 'genève', 'geneve', 'genf', 'basel', 'bâle', 'bern', 'berne',
                    'lausanne', 'zug', 'lugano', 'lucerne', 'luzern', 'winterthur',
                    'st. gallen', 'st gallen', 'baden', 'aarau', 'neuchâtel', 'fribourg'],
    'germany': ['germany', 'deutschland', 'berlin', 'munich', 'münchen', 'hamburg',
                'frankfurt', 'cologne', 'köln', 'stuttgart', 'düsseldorf'],
    'austria': ['austria', 'österreich', 'vienna', 'wien', 'graz', 'linz', 'salzburg'],
    'france': ['france', 'paris', 'lyon', 'marseille', 'toulouse', 'lille'],
    'united kingdom': ['united kingdom', 'uk', 'england', 'london', 'manchester', 'edinburgh'],
    'netherlands': ['netherlands', 'amsterdam', 'rotterdam', 'utrecht', 'the hague'],
    'remote': ['remote'],
}
EUROPEAN = ['europe', 'emea', 'eu', 'belgium', 'brussels', 'spain', 'madrid', 'barcelona',
            'italy', 'milan', 'rome', 'portugal', 'lisbon', 'porto', 'ireland', 'dublin',
            'sweden', 'stockholm', 'denmark', 'copenhagen', 'norway', 'oslo', 'finland',
            'helsinki', 'poland', 'warsaw', 'krakow', 'czech', 'prague', 'luxembourg',
            'hungary', 'budapest', 'romania', 'greece', 'estonia', 'latvia', 'lithuania']
LOCATION_ALIASES['europe'] = LOCATION_ALIASES['emea'] = EUROPEAN + [
    name for country, names in LOCATION_ALIASES.items() if country != 'remote' for name in names]
WIDE = {'worldwide', 'anywhere'}

BLOCK_TAGS = {'p', 'div', 'li', 'br', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'tr', 'ul', 'ol'}


def companies_path():
    return os.path.join(os.environ.get('CVS_DIR', 'cvs'), 'companies.yaml')


def load_companies(path=None):
    path = path or companies_path()
    if not os.path.exists(path):
        return []
    with open(path, encoding='utf-8') as f:
        data = yaml.safe_load(f) or {}
    return [c for c in (data.get('companies') or []) if c.get('ats') and c.get('board')]


def configured():
    return bool(load_companies())


# --- text -------------------------------------------------------------------

def html_text(markup):
    """Readable text from a posting's HTML, one block per line."""
    if not markup or not markup.strip():
        return ''
    tree = lxml_html.fromstring(f'<div>{markup}</div>')
    for element in tree.iter():
        if element.tag in BLOCK_TAGS:
            element.tail = '\n' + (element.tail or '')
    lines = (re.sub(r'[ \t\xa0]+', ' ', l).strip() for l in tree.text_content().splitlines())
    return '\n'.join(l for l in lines if l)


# --- matching ---------------------------------------------------------------

def _words(text):
    return {w for w in re.findall(r'\w+', (text or '').lower()) if w not in STOPWORDS}


def title_matches(title, keywords):
    """Every significant word of some keyword appears in the title."""
    words = _words(title)
    return any(_words(k) and _words(k) <= words for k in keywords)


def location_matches(location, wanted):
    if not wanted or any(w.strip().lower() in WIDE for w in wanted):
        return True
    place = (location or '').lower()
    for w in wanted:
        for name in LOCATION_ALIASES.get(w.strip().lower(), [w.strip().lower()]):
            if re.search(r'(?<!\w)' + re.escape(name) + r'(?!\w)', place):
                return True
    return False


# --- adapters: one feed -> [{id, title, location, description, url, apply_url}] -

def _greenhouse(board, get):
    data = get(f'https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true').json()
    for j in data.get('jobs', []):
        yield {'id': str(j['id']), 'title': j.get('title'),
               'location': (j.get('location') or {}).get('name', ''),
               'description': html_text(html.unescape(j.get('content') or '')),
               'url': j.get('absolute_url'), 'apply_url': j.get('absolute_url')}


def _lever(board, get):
    for j in get(f'https://api.lever.co/v0/postings/{board}?mode=json').json():
        cats = j.get('categories') or {}
        parts = [j.get('descriptionPlain') or '']
        for block in j.get('lists') or []:
            parts.append(block.get('text') or '')
            parts.append(html_text(block.get('content') or ''))
        parts.append(j.get('additionalPlain') or '')
        yield {'id': j['id'], 'title': j.get('text'),
               'location': ', '.join(cats.get('allLocations') or [cats.get('location') or '']),
               'description': '\n'.join(p.strip() for p in parts if p and p.strip()),
               'url': j.get('hostedUrl'), 'apply_url': j.get('applyUrl') or j.get('hostedUrl')}


def _ashby(board, get):
    data = get(f'https://api.ashbyhq.com/posting-api/job-board/{board}').json()
    for j in data.get('jobs', []):
        if j.get('isListed') is False:
            continue
        places = [j.get('location') or ''] + [s.get('location') or '' for s in j.get('secondaryLocations') or []]
        yield {'id': j['id'], 'title': j.get('title'), 'location': ', '.join(p for p in places if p),
               'description': j.get('descriptionPlain') or html_text(j.get('descriptionHtml') or ''),
               'url': j.get('jobUrl'), 'apply_url': j.get('applyUrl') or j.get('jobUrl')}


def _personio(board, get):
    root = ET.fromstring(get(f'https://{board}.jobs.personio.de/xml').content)
    for pos in root.findall('position'):
        job_id = (pos.findtext('id') or '').strip()
        offices = [pos.findtext('office') or ''] + [o.text or '' for o in pos.findall('additionalOffices/office')]
        sections = []
        for d in pos.findall('jobDescriptions/jobDescription'):
            sections.append((d.findtext('name') or '').strip())
            sections.append(html_text(d.findtext('value') or ''))
        url = f'https://{board}.jobs.personio.de/job/{job_id}'
        yield {'id': job_id, 'title': pos.findtext('name'), 'location': ', '.join(o for o in offices if o),
               'description': '\n'.join(s for s in sections if s), 'url': url, 'apply_url': url}


def _smartrecruiters(board, get, most=1000):
    postings, offset = [], 0
    while offset < most:              # 100 a page; a large employer lists thousands
        data = get(f'https://api.smartrecruiters.com/v1/companies/{board}/postings'
                   f'?limit=100&offset={offset}').json()
        postings += data.get('content', [])
        offset += 100
        if offset >= (data.get('totalFound') or 0):
            break
    for j in postings:
        loc = j.get('location') or {}
        yield {'id': str(j['id']), 'title': j.get('name'),
               'location': loc.get('fullLocation') or ', '.join(filter(None, [loc.get('city'), loc.get('country')])),
               'description': None,           # listing has none: fetched per match
               'detail': f'https://api.smartrecruiters.com/v1/companies/{board}/postings/{j["id"]}',
               'url': None, 'apply_url': None}


def _smartrecruiters_detail(job, get):
    data = get(job['detail']).json()
    sections = (data.get('jobAd') or {}).get('sections') or {}
    parts = []
    for key in ('companyDescription', 'jobDescription', 'qualifications', 'additionalInformation'):
        section = sections.get(key) or {}
        parts += [section.get('title') or '', html_text(section.get('text') or '')]
    job.update(description='\n'.join(p for p in parts if p),
               url=data.get('postingUrl'), apply_url=data.get('applyUrl') or data.get('postingUrl'))
    return job


def _recruitee(board, get):
    for j in get(f'https://{board}.recruitee.com/api/offers/').json().get('offers', []):
        if j.get('status') not in (None, 'published'):
            continue
        yield {'id': str(j['id']), 'title': j.get('title'),
               'location': j.get('location') or ', '.join(filter(None, [j.get('city'), j.get('country')])),
               'description': '\n'.join(filter(None, [html_text(j.get('description') or ''),
                                                      html_text(j.get('requirements') or '')])),
               'url': j.get('careers_url'), 'apply_url': j.get('careers_apply_url') or j.get('careers_url')}


ADAPTERS = {'greenhouse': _greenhouse, 'lever': _lever, 'ashby': _ashby, 'personio': _personio,
            'smartrecruiters': _smartrecruiters, 'recruitee': _recruitee}

# Careers-page URLs -> (ats, board).
DETECT = [
    ('greenhouse', r'(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?([\w-]+)'),
    ('greenhouse', r'boards-api\.greenhouse\.io/v1/boards/([\w-]+)'),
    ('lever', r'jobs\.(?:eu\.)?lever\.co/([\w.-]+)'),
    ('ashby', r'jobs\.ashbyhq\.com/([\w.%-]+)'),
    ('personio', r'([\w-]+)\.jobs\.personio\.(?:de|com)'),
    ('smartrecruiters', r'(?:jobs|careers)\.smartrecruiters\.com/([\w-]+)'),
    ('recruitee', r'([\w-]+)\.recruitee\.com'),
]


def detect(url):
    """(ats, board) for a careers-page link, or None."""
    for ats, pattern in DETECT:
        m = re.search(pattern, url or '', re.I)
        if m:
            return ats, urllib.parse.unquote(m.group(1))
    return None


# --- the run ----------------------------------------------------------------

def _store(company, ats, board, job, source):
    job_id = f"ats-{ats}-{board}-{job['id']}"
    title = (job.get('title') or '').strip() or 'Unknown role'
    if db.job_exists(job_id) or already_listed(title, company):
        return False
    if not db.add_job(job_id, title, company, job.get('location') or '',
                      job.get('description') or '', job.get('url') or job.get('apply_url') or '', False):
        return False
    conn = db.get_connection()
    conn.execute('UPDATE jobs SET source = ?, apply_url = ? WHERE job_id = ?',
                 (source, job.get('apply_url'), job_id))
    conn.commit()
    conn.close()
    return True


def ingest(criteria=None, companies=None, session=None, sleep=time.sleep):
    """Read every watched company's feed. Returns ({'<company> (ATS)': added}, [problems])."""
    db.init_db()
    companies = load_companies() if companies is None else companies
    if not companies:
        return {}, []
    if criteria is None:
        from scraper import get_search_criteria
        criteria = get_search_criteria()
    keywords, locations, _ = criteria
    default_keywords = [k for k, _ in keywords]
    default_locations = sorted({l for _, pinned in keywords for l in (pinned or locations)})

    session = session or requests.Session()

    def get(url):
        response = session.get(url, headers={'User-Agent': USER_AGENT, 'Accept': 'application/json'},
                               timeout=30)
        response.raise_for_status()
        return response

    stats, problems = {}, []
    for index, c in enumerate(companies):
        if index:
            sleep(random.uniform(*COMPANY_GAP))
        name, ats, board = c.get('name') or c['board'], c['ats'].strip().lower(), str(c['board']).strip()
        key = f'{name} (ATS)'
        stats[key] = 0
        adapter = ADAPTERS.get(ats)
        if not adapter:
            problems.append(f"{name}: unknown ats '{ats}' (one of {', '.join(ADAPTERS)})")
            continue
        wanted_keywords = c.get('keywords') or default_keywords
        wanted_locations = c.get('locations') or default_locations
        try:
            postings = list(adapter(board, get))
        except Exception as e:
            problems.append(f'{name}: could not read the {ats} feed ({e})')
            continue
        matched = [j for j in postings
                   if title_matches(j.get('title'), wanted_keywords)
                   and location_matches(j.get('location'), wanted_locations)]
        for job in matched:
            if job.get('detail'):
                if db.job_exists(f"ats-{ats}-{board}-{job['id']}"):
                    continue
                try:
                    _smartrecruiters_detail(job, get)
                except Exception as e:
                    problems.append(f"{name}: could not read posting {job['id']} ({e})")
                    continue
            if len((job.get('description') or '').strip()) < 200:
                continue                    # never scored without a real description
            stats[key] += _store(name, ats, board, job, f'ats: {name}')
        print(f'{name} ({ats}): {len(postings)} open, {len(matched)} matching, {stats[key]} new')
    return stats, problems


if __name__ == '__main__':
    import sys
    from dotenv import load_dotenv
    load_dotenv()
    if len(sys.argv) > 2 and sys.argv[1] == '--detect':
        found = detect(sys.argv[2])
        if not found:
            raise SystemExit("Not a recognised ATS link. Supported: " + ', '.join(ADAPTERS))
        print(f"  - name: <Company>\n    ats: {found[0]}\n    board: {found[1]}")
        raise SystemExit(0)
    if not configured():
        raise SystemExit(f'No companies to watch: create {companies_path()} (see the docstring).')
    stats, problems = ingest()
    print(stats)
    for p in problems:
        print('  !', p)
