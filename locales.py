"""What differs by country: names, languages, letter conventions, dates, phones.

One table, so no other module needs to know about any one country. A job's
country is read off its location ("Zurich, Switzerland") with `country_of`;
where it cannot be told, the candidate's own HOME_COUNTRY (.env) is used,
and where that is unset too, the neutral defaults below.

Add a country by adding an entry. Every field is optional:
- names: how it is written in locations and company names (lower case, in
  English and its own languages).
- languages: the languages adverts there are written in, most common first.
- date: the letter date's strftime format.
- phone: its international dialling code; trunk: the digit national numbers
  start with instead (default "0", "" where there is none).
- closings: a letter's closing by language, where the country's usage
  differs from the language's standard one (`LETTER_FORMULAS`).
- spelling: (from, to) replacements by language, for national spelling.
"""

import os
import re

COUNTRIES = {
    'switzerland': {
        'names': ('switzerland', 'schweiz', 'suisse', 'svizzera', 'swiss'),
        'languages': ('German', 'French', 'Italian'),
        'date': '%d.%m.%Y', 'phone': '41',
        'closings': {'german': 'Freundliche Grüsse,', 'french': 'Meilleures salutations,'},
        # Swiss German has no ß: "grossem", not "großem".
        'spelling': {'german': (('ß', 'ss'), ('ẞ', 'SS'))},
    },
    'germany': {'names': ('germany', 'deutschland'), 'languages': ('German',),
                'date': '%d.%m.%Y', 'phone': '49'},
    'austria': {'names': ('austria', 'österreich', 'osterreich'), 'languages': ('German',),
                'date': '%d.%m.%Y', 'phone': '43'},
    'liechtenstein': {'names': ('liechtenstein',), 'languages': ('German',),
                      'date': '%d.%m.%Y', 'phone': '423',
                      'closings': {'german': 'Freundliche Grüsse,'},
                      'spelling': {'german': (('ß', 'ss'), ('ẞ', 'SS'))}},
    'france': {'names': ('france',), 'languages': ('French',), 'date': '%d/%m/%Y', 'phone': '33'},
    'belgium': {'names': ('belgium', 'belgique', 'belgië', 'belgie', 'belgien'),
                'languages': ('Dutch', 'French', 'German'), 'date': '%d/%m/%Y', 'phone': '32'},
    'luxembourg': {'names': ('luxembourg', 'luxemburg'), 'languages': ('French', 'German', 'Luxembourgish'),
                   'date': '%d/%m/%Y', 'phone': '352', 'trunk': ''},
    'netherlands': {'names': ('netherlands', 'the netherlands', 'nederland', 'holland'),
                    'languages': ('Dutch',), 'date': '%d-%m-%Y', 'phone': '31'},
    'italy': {'names': ('italy', 'italia'), 'languages': ('Italian',), 'date': '%d/%m/%Y',
              'phone': '39', 'trunk': ''},
    'spain': {'names': ('spain', 'españa', 'espana'), 'languages': ('Spanish',), 'date': '%d/%m/%Y',
              'phone': '34', 'trunk': ''},
    'portugal': {'names': ('portugal',), 'languages': ('Portuguese',), 'date': '%d/%m/%Y',
                 'phone': '351', 'trunk': ''},
    'united kingdom': {'names': ('united kingdom', 'uk', 'great britain', 'england', 'scotland', 'wales'),
                       'languages': ('English',), 'date': '%d/%m/%Y', 'phone': '44'},
    'ireland': {'names': ('ireland',), 'languages': ('English',), 'date': '%d/%m/%Y', 'phone': '353'},
    'united states': {'names': ('united states', 'usa', 'u.s.', 'america'), 'languages': ('English',),
                      'date': '%m/%d/%Y', 'phone': '1', 'trunk': ''},
    'canada': {'names': ('canada',), 'languages': ('English', 'French'), 'date': '%Y-%m-%d',
               'phone': '1', 'trunk': ''},
    'poland': {'names': ('poland', 'polska'), 'languages': ('Polish',), 'date': '%d.%m.%Y',
               'phone': '48', 'trunk': ''},
    'sweden': {'names': ('sweden', 'sverige'), 'languages': ('Swedish',), 'date': '%Y-%m-%d', 'phone': '46'},
    'denmark': {'names': ('denmark', 'danmark'), 'languages': ('Danish',), 'date': '%d.%m.%Y',
                'phone': '45', 'trunk': ''},
    'norway': {'names': ('norway', 'norge'), 'languages': ('Norwegian',), 'date': '%d.%m.%Y',
               'phone': '47', 'trunk': ''},
    'finland': {'names': ('finland', 'suomi'), 'languages': ('Finnish', 'Swedish'), 'date': '%d.%m.%Y',
                'phone': '358'},
    'united arab emirates': {'names': ('united arab emirates', 'uae'), 'languages': ('English', 'Arabic'),
                             'date': '%d/%m/%Y', 'phone': '971'},
    'saudi arabia': {'names': ('saudi arabia', 'ksa'), 'languages': ('English', 'Arabic'),
                     'date': '%d/%m/%Y', 'phone': '966'},
    'qatar': {'names': ('qatar',), 'languages': ('English', 'Arabic'), 'date': '%d/%m/%Y', 'phone': '974'},
    'bahrain': {'names': ('bahrain',), 'languages': ('English', 'Arabic'), 'date': '%d/%m/%Y', 'phone': '973'},
    'kuwait': {'names': ('kuwait',), 'languages': ('English', 'Arabic'), 'date': '%d/%m/%Y', 'phone': '965'},
    'oman': {'names': ('oman',), 'languages': ('English', 'Arabic'), 'date': '%d/%m/%Y', 'phone': '968'},
    'australia': {'names': ('australia',), 'languages': ('English',), 'date': '%d/%m/%Y', 'phone': '61'},
    'new zealand': {'names': ('new zealand', 'aotearoa'), 'languages': ('English',), 'date': '%d/%m/%Y',
                    'phone': '64'},
    'japan': {'names': ('japan', 'nippon'), 'languages': ('Japanese', 'English'), 'date': '%Y/%m/%d',
              'phone': '81'},
    'south korea': {'names': ('south korea', 'korea', 'republic of korea'), 'languages': ('Korean', 'English'),
                    'date': '%Y.%m.%d', 'phone': '82'},
    'china': {'names': ('china', 'prc'), 'languages': ('Chinese (Simplified)', 'English'), 'date': '%Y-%m-%d',
              'phone': '86'},
    'hong kong': {'names': ('hong kong', 'hk'), 'languages': ('English', 'Chinese (Traditional)'),
                  'date': '%d/%m/%Y', 'phone': '852', 'trunk': ''},
    'singapore': {'names': ('singapore',), 'languages': ('English',), 'date': '%d/%m/%Y', 'phone': '65',
                  'trunk': ''},
    'india': {'names': ('india',), 'languages': ('English', 'Hindi'), 'date': '%d/%m/%Y', 'phone': '91'},
    'mexico': {'names': ('mexico', 'méxico'), 'languages': ('Spanish',), 'date': '%d/%m/%Y', 'phone': '52',
               'trunk': ''},
    'brazil': {'names': ('brazil', 'brasil'), 'languages': ('Portuguese',), 'date': '%d/%m/%Y', 'phone': '55'},
    'argentina': {'names': ('argentina',), 'languages': ('Spanish',), 'date': '%d/%m/%Y', 'phone': '54'},
    'chile': {'names': ('chile',), 'languages': ('Spanish',), 'date': '%d-%m-%Y', 'phone': '56', 'trunk': ''},
    'colombia': {'names': ('colombia',), 'languages': ('Spanish',), 'date': '%d/%m/%Y', 'phone': '57',
                 'trunk': ''},
    'peru': {'names': ('peru', 'perú'), 'languages': ('Spanish',), 'date': '%d/%m/%Y', 'phone': '51', 'trunk': ''},
}

# Where nothing is known about the country.
DEFAULT_DATE = '%d/%m/%Y'
DEFAULT_TRUNK = '0'

# A letter's greeting and closing by language, standard usage; a country's
# own `closings` override these.
LETTER_FORMULAS = {
    'french': {'dear': 'Bonjour', 'team': 'Madame, Monsieur,', 'closing': 'Cordialement,'},
    'german': {'dear': 'Guten Tag', 'team': 'Sehr geehrte Damen und Herren,', 'closing': 'Mit freundlichen Grüßen'},
    'italian': {'dear': 'Gentile', 'team': 'Gentili Signore e Signori,', 'closing': 'Cordiali saluti,'},
    'spanish': {'dear': 'Estimado/a', 'team': 'Estimados señores:', 'closing': 'Atentamente,'},
    'dutch': {'dear': 'Beste', 'team': 'Geachte heer, mevrouw,', 'closing': 'Met vriendelijke groet,'},
    'portuguese': {'dear': 'Caro/a', 'team': 'Exmos. Senhores,', 'closing': 'Com os melhores cumprimentos,'},
}

# Language name -> the suffix of its HOME_CITY_<code> setting.
LANGUAGE_CODES = {'english': 'EN', 'french': 'FR', 'german': 'DE', 'italian': 'IT', 'spanish': 'ES',
                  'dutch': 'NL', 'portuguese': 'PT', 'polish': 'PL', 'swedish': 'SV', 'danish': 'DA',
                  'norwegian': 'NO', 'finnish': 'FI'}


def _key(name):
    """The table's key for a country name in any of its spellings, or None."""
    wanted = ' '.join((name or '').lower().split())
    for key, info in COUNTRIES.items():
        if wanted == key or wanted in info.get('names', ()):
            return key
    return None


def home_country():
    """The table key for HOME_COUNTRY, or None (unset or not in the table)."""
    return _key(os.environ.get('HOME_COUNTRY', ''))


def country_of(*places, default=None):
    """The table key of the first country named in `places` (free text such
    as "Zurich, Switzerland"), else `default`."""
    for place in places:
        text = (place or '').lower()
        for key, info in COUNTRIES.items():
            for name in (key,) + tuple(info.get('names', ())):
                if re.search(r'(?<![\w.])' + re.escape(name) + r'(?![\w])', text):
                    return key
    return default


def info(country):
    return COUNTRIES.get(country or '', {})


def country_words(country):
    """How `country` is written in a company's name ("Example Switzerland")."""
    entry = info(country)
    return tuple(entry.get('names', ())) + ((country,) if entry else ())


def languages(country):
    return tuple(info(country).get('languages', ()))


def letter_date_format(country):
    return info(country).get('date', DEFAULT_DATE)


def letter_formulas(language, country=None):
    """{'dear', 'team', 'closing'} for a letter in `language` to `country`,
    or None for a language without them (English among them)."""
    lang = (language or '').lower()
    for name, forms in LETTER_FORMULAS.items():
        if name in lang:
            closing = info(country).get('closings', {}).get(name)
            return dict(forms, closing=closing) if closing else dict(forms)
    return None


def spelling(language, country):
    """(from, to) replacements for documents in `language` for a job in `country`."""
    lang = (language or '').lower()
    rules = info(country).get('spelling', {})
    return next((pairs for name, pairs in rules.items() if name in lang), ())


def phone_code(country):
    return info(country).get('phone')


def trunk_prefix(code):
    """The digit national numbers start with, for a dialling code."""
    for entry in COUNTRIES.values():
        if entry.get('phone') == code:
            return entry.get('trunk', DEFAULT_TRUNK)
    return DEFAULT_TRUNK


def home_city(language):
    """HOME_CITY_<code> for a letter in `language` (HOME_CITY_FR for French),
    or None when it is not set."""
    lang = (language or '').lower()
    code = next((c for name, c in LANGUAGE_CODES.items() if name in lang), None)
    return ((os.environ.get(f'HOME_CITY_{code}') or '').strip() or None) if code else None
