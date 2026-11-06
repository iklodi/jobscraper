"""How a CV is expected to look in the job's country.

Application norms split along anti-discrimination law and hiring tradition:
what the document is called, whether it carries a photo, how long it is, which
personal details it states or leaves out, and what goes with it. They are
written down here, by region, and `for_job` picks the job's - from the job's
location, else the candidate's HOME_COUNTRY. The generator then applies them
(`generator.apply_cv_norms`): some in code (the photo, personal-detail lines,
a referees line, the file name, the page target, the DACH dossier PDF), the
rest as instructions in the writing prompt (`prompt_block`). Each job's
history records what was done.

Two cross-border rules sit on top of the table:
- "CV" in the US and Canada means a long academic record; an industry
  application there is a résumé.
- A US- or UK-style employer (headquartered in a photo-free country) hiring in
  English in a photo-standard country usually keeps its own photo-free
  standard: `for_job` then drops the photo, and says so, since only the
  company's local careers page can confirm it.
"""

import locales

# photo: 'no' (strictly - removed from the CV), 'optional' (the template
# decides), 'expected' / 'mandatory' (kept; flagged when the template has none).
# omit: personal details to leave off. include: personal details to state.
# pages: (fewest, most). statement: open with a short personal statement.
# referees: end with "Referees available upon request".
# dossier: one PDF of letter, CV, diplomas and references is expected upfront.
REGIONS = {
    'north_america': {
        'label': 'US & Canada',
        'countries': ('united states', 'canada'),
        'term': 'Résumé', 'file_term': 'Resume', 'photo': 'no', 'pages': (1, 2),
        'omit': ('date_of_birth', 'age', 'marital_status', 'nationality', 'gender'),
        'notes': ('Call the document a résumé, in the cover letter too: in the US and Canada a "CV" '
                  'is an exhaustive academic or medical record.',
                  'Leave out age, date of birth, marital status, nationality and gender '
                  '(anti-discrimination norms).',
                  'Keep a clean single-column text layout: applicant-tracking systems garble '
                  'graphics and images.'),
    },
    'uk_ireland': {
        'label': 'UK & Ireland',
        'countries': ('united kingdom', 'ireland'),
        'term': 'CV', 'photo': 'no', 'pages': (2, 2), 'statement': True,
        'omit': ('date_of_birth', 'age', 'gender', 'marital_status'),
        'notes': ('Make the CV summary a 3-4 line Personal Statement.',
                  'Leave out date of birth, gender and photos (Equality Act guidelines).',
                  'Keep a clean single-column text layout: applicant-tracking systems garble '
                  'graphics and images.'),
    },
    'oceania': {
        'label': 'Australia & New Zealand',
        'countries': ('australia', 'new zealand'),
        'term': 'CV', 'photo': 'no', 'pages': (2, 4), 'referees': True,
        'omit': ('date_of_birth', 'age', 'marital_status', 'gender'),
        'notes': ('Give detailed achievement bullets for each role.',
                  'The CV ends with "Referees available upon request".'),
    },
    'dach': {
        'label': 'DACH',
        'countries': ('germany', 'austria', 'switzerland', 'liechtenstein'),
        'term': 'CV', 'local_term': 'Lebenslauf', 'photo': 'expected', 'pages': (1, 2), 'dossier': True,
        'notes': ('A professional headshot is standard, though legally optional.',
                  'Employers expect the whole dossier upfront: the cover letter may say that the CV, '
                  'diplomas and reference letters (Arbeitszeugnisse / certificats de travail) are '
                  'enclosed.'),
    },
    'southern_europe_benelux': {
        'label': 'Southern Europe & Benelux',
        'countries': ('france', 'spain', 'italy', 'portugal', 'belgium', 'netherlands', 'luxembourg'),
        'term': 'CV', 'photo': 'expected', 'pages': (1, 2),
        # Photos are shifting toward optional in the Netherlands and Belgium.
        'photo_by_country': {'netherlands': 'optional', 'belgium': 'optional'},
        'notes': (),
    },
    'nordics': {
        'label': 'Nordics',
        'countries': ('sweden', 'norway', 'denmark', 'finland', 'iceland'),
        'term': 'CV', 'photo': 'optional', 'pages': (1, 2),
        'notes': ('A photo is acceptable, though increasingly omitted at multinational tech firms.',),
    },
    'japan': {
        'label': 'Japan',
        'countries': ('japan',),
        'term': 'Rirekisho / Shokumu Keirekisho', 'photo': 'mandatory', 'pages': (1, 2),
        'warning': ('Japanese employers expect two documents: the standard Rirekisho form, with a formal '
                    '3x4 cm ID photo, and a free-form work history (Shokumu Keirekisho). Only the '
                    'work history is generated; fill in the Rirekisho yourself.'),
        'notes': ('Write the CV as a Shokumu Keirekisho: a chronological work history with '
                  'responsibilities and results per role.',),
    },
    'south_korea': {
        'label': 'South Korea',
        'countries': ('south korea',),
        'term': 'Iryeokseo (CV)', 'photo': 'expected', 'pages': (1, 2),
        'warning': ('Private employers expect a formal headshot; the public sector and the large '
                    'groups with blind hiring ban it. Check which this employer is.'),
        'notes': (),
    },
    'east_asia_hubs': {
        'label': 'China, Singapore & Hong Kong',
        'countries': ('china', 'singapore', 'hong kong'),
        'term': 'CV', 'photo': 'optional', 'pages': (1, 2),
        'notes': ('Local firms expect a photo and basic personal details; Western multinationals in '
                  'Singapore and Hong Kong follow photo-free US/UK rules.',),
    },
    'middle_east': {
        'label': 'Middle East',
        'countries': ('united arab emirates', 'saudi arabia', 'qatar', 'bahrain', 'kuwait', 'oman'),
        'term': 'CV', 'photo': 'expected', 'pages': (2, 3),
        'include': ('nationality', 'visa_status'),
        'notes': ('State nationality and visa status: employment visas and quotas are tied to '
                  'citizenship.',),
    },
    'latam_india': {
        'label': 'Latin America & India',
        'countries': ('mexico', 'brazil', 'argentina', 'chile', 'colombia', 'peru', 'india'),
        'term': 'CV', 'photo': 'optional', 'pages': (1, 2),
        'notes': ('Photos and personal details remain common in traditional domestic industries; tech '
                  'and multinational employers use photo-free US formats.',),
    },
}

PHOTO_FREE = {key for key, region in REGIONS.items() if region['photo'] == 'no'}


def region_of(country):
    """The REGIONS key for a locales country key, or None."""
    return next((key for key, region in REGIONS.items() if country in region['countries']), None)


def for_job(*places, hq_country=None, language=None, home=None):
    """The norms for a job: a dict with the region's fields plus 'country',
    'region', 'photo' (after the per-country rule and the multinational
    override) and 'reasons' (what decided). None when no region is known.

    `places`: what says where the job is, best first - the scorer's country,
    the advert's location, the office the letter is addressed to; the first
    that names a country decides, else HOME_COUNTRY. `hq_country`: where the
    employer is headquartered, from the writing model; `language`: the
    advert's language."""
    home = home if home is not None else locales.home_country()
    country = locales.country_of(*places, default=home)
    key = region_of(country)
    if not key:
        return None
    region = REGIONS[key]
    norms = dict(region, country=country, region=key, reasons=[])
    norms['photo'] = region.get('photo_by_country', {}).get(country, region['photo'])
    hq_region = region_of(locales.country_of(hq_country or ''))
    english = 'english' in (language or '').lower()
    if norms['photo'] != 'no' and hq_region in PHOTO_FREE and english:
        norms['photo'] = 'no'
        norms['reasons'].append(
            f"no photo: an employer headquartered in {locales.country_of(hq_country).title()} hiring in "
            "English usually keeps its photo-free standard in every office - check the company's local "
            "careers page")
    return norms


def page_target(norms, template_pages):
    """The most pages the CV should run to: the region's maximum, never more
    than the template's own length."""
    if not norms:
        return template_pages
    most = norms['pages'][1]
    return min(most, template_pages) if template_pages else most


def prompt_block(norms, page_limit=None):
    """The writing prompt's instructions for the job's region, or ''."""
    if not norms:
        return ''
    country = norms['country'].title()
    lines = [f"\nHIRING NORMS FOR {country.upper()} ({norms['label']}) - follow them:"]
    lines.append(f"- The document is called a {norms['term']}"
                 + (f" ({norms['local_term']})" if norms.get('local_term') else '') + '.')
    if page_limit:
        lines.append(f"- The CV must fit on {page_limit} page{'s' if page_limit > 1 else ''}: shorten bullet "
                     "points, and use cv_removals for the oldest or least relevant detail if needed.")
    lines += [f'- {note}' for note in norms.get('notes', ())]
    return '\n'.join(lines) + '\n'
