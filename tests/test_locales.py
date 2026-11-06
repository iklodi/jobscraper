"""Country conventions come from one table, keyed by the job's or the candidate's country."""
import datetime

import pytest
from docx import Document

import applier
import dedupe
import locales


@pytest.mark.parametrize('place, country', [
    ('Zurich, Switzerland', 'switzerland'), ('Lausanne, Suisse', 'switzerland'),
    ('London, UK', 'united kingdom'), ('Remote (USA)', 'united states'), ('Berlin', None),
    ('Paris, France', 'france'), ('Dubai, UAE', 'united arab emirates'),
])
def test_country_of(place, country):
    assert locales.country_of(place) == country


def test_unknown_places_fall_back():
    assert locales.country_of('Bern', '', default='switzerland') == 'switzerland'


@pytest.mark.parametrize('home, number, expected', [
    ('Switzerland', '+41 79 123 45 67', ['+41 79 123 45 67', '+41791234567', '0041791234567', '0791234567']),
    ('Switzerland', '079 123 45 67', ['079 123 45 67', '0791234567', '+41791234567']),
    ('Germany', '0151 1234567', ['0151 1234567', '01511234567', '+491511234567']),
    ('Italy', '+39 06 1234 5678', ['+39 06 1234 5678', '+390612345678', '00390612345678', '0612345678']),
    ('United States', '+1 212 555 0100', ['+1 212 555 0100', '+12125550100', '0012125550100', '2125550100']),
    ('', '079 123 45 67', ['079 123 45 67', '0791234567']),       # no country known: no guess
])
def test_phone_variants_follow_the_number_or_the_home_country(monkeypatch, home, number, expected):
    monkeypatch.setenv('HOME_COUNTRY', home)
    monkeypatch.delenv('PHONE_COUNTRY_CODE', raising=False)
    assert applier.phone_variants(number) == expected


def test_phone_country_code_setting_wins_over_the_home_country(monkeypatch):
    monkeypatch.setenv('HOME_COUNTRY', 'Switzerland')
    monkeypatch.setenv('PHONE_COUNTRY_CODE', '+33')
    assert applier.phone_variants('06 12 34 56 78')[-1] == '+33612345678'


@pytest.mark.parametrize('home, a, b, same', [
    ('Switzerland', 'Example Switzerland AG', 'Example', True),
    ('Switzerland', 'Example Schweiz', 'Example Suisse SA', True),
    ('Germany', 'Example Deutschland GmbH', 'Example', True),
    ('Germany', 'Example Switzerland AG', 'Example', False),
    ('', 'Example GmbH', 'Example AG', True),                         # legal forms everywhere
])
def test_the_home_countrys_name_is_dropped_from_company_names(monkeypatch, home, a, b, same):
    monkeypatch.setenv('HOME_COUNTRY', home)
    assert dedupe.same_company(a, b) is same


def test_letter_dates_and_city_names(monkeypatch):
    day = datetime.date(2026, 3, 4)
    assert day.strftime(locales.letter_date_format('switzerland')) == '04.03.2026'
    assert day.strftime(locales.letter_date_format('united states')) == '03/04/2026'
    assert day.strftime(locales.letter_date_format(None)) == '04/03/2026'
    monkeypatch.setenv('HOME_CITY_DE', 'Genf, Schweiz')
    monkeypatch.delenv('HOME_CITY_IT', raising=False)
    assert locales.home_city('German') == 'Genf, Schweiz'
    assert locales.home_city('Italian') is None and locales.home_city(None) is None


def test_the_language_menu_starts_with_the_home_countrys(monkeypatch):
    import app
    monkeypatch.setenv('HOME_COUNTRY', 'Belgium')
    assert app.app.test_client().get('/api/locale').get_json() == {
        'main_languages': ['English', 'Dutch', 'French', 'German']}
    monkeypatch.delenv('HOME_COUNTRY')
    assert app.app.test_client().get('/api/locale').get_json() == {'main_languages': ['English']}
