"""Reading LinkedIn's AI job search page. The markup is a made-up miniature of
the real page's structure: generated class names left out, the attributes
the scraper relies on kept."""
import asyncio

import pytest
from playwright.async_api import async_playwright

import scraper


def card(job_id, title, company, location, extra='', prefix=''):
    return f'''
    <div role="button" tabindex="0" componentkey="job-card-component-ref-{job_id}">
      <div componentkey="job-card-component-ref-{job_id}">
        <button type="button" aria-label="Dismiss {title} job"></button>
        <p><span>{prefix}{title}</span><span>{title}</span></p>
        <p>{company}</p>
        <p>{location}</p>
        {extra}
      </div>
    </div><hr role="presentation">'''


PAGE = f'''<html><body>
<div data-testid="lazy-column">
  {card('1000000001', 'Example Analyst', 'Example AG', 'Lausanne (Hybrid)', prefix='Selected, ')}
  {card('1000000002', 'Example Architect', 'Sample SA', 'Geneva', extra='<p>Promoted</p><p>Easy Apply</p>')}
</div>
<div data-sdui-screen="com.example.JobDetails">
  <div><div><h2>About the job</h2></div>
    <div data-testid="expandable-text-box">First part of the advert.</div>
    <p>Second part of the advert.</p>
    <button data-testid="expandable-text-button">… more</button>
  </div>
</div>
</body></html>'''


def run(js, *args):
    async def go():
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            await page.set_content(PAGE)
            try:
                return await page.evaluate(js, *args)
            finally:
                await browser.close()
    return asyncio.run(go())


def test_cards_give_id_title_company_location_and_promotion():
    cards = run(scraper.READ_CARDS_JS, scraper.CARD_SELECTOR)
    assert cards == [
        {'id': '1000000001', 'title': 'Example Analyst', 'company': 'Example AG',
         'location': 'Lausanne (Hybrid)', 'promoted': False},
        {'id': '1000000002', 'title': 'Example Architect', 'company': 'Sample SA',
         'location': 'Geneva', 'promoted': True},
    ]


def test_description_is_the_whole_section_without_heading_or_more():
    text = run(scraper.READ_DESCRIPTION_JS)
    assert 'First part of the advert.' in text and 'Second part of the advert.' in text
    assert 'About the job' not in text and not text.endswith('more')


@pytest.mark.parametrize('value, pages', [(None, 1), ('3', 3), ('0', 1), ('x', 1)])
def test_pages_per_search_is_a_setting(monkeypatch, value, pages):
    if value is None:
        monkeypatch.delenv('LINKEDIN_SEARCH_PAGES', raising=False)
    else:
        monkeypatch.setenv('LINKEDIN_SEARCH_PAGES', value)
    assert scraper.search_pages() == pages


def test_search_address_is_the_ai_search():
    url = scraper.search_results_url('Example Role', 'Switzerland', 25, 'FC')
    assert url.startswith('https://www.linkedin.com/jobs/search-results/?')
    assert 'keywords=Example+Role+in+Switzerland' in url and 'start=25' in url and 'f_JT=FC' in url
