"""Only the posting's own apply control decides Easy Apply (applier.is_easy_apply)."""
import asyncio

from playwright.async_api import async_playwright

import applier

OFFSITE = '''
<main>
  <h1>Chief Architect</h1>
  <a href="#" aria-label="Apply to Chief Architect on company website">Apply</a>
  <section style="margin-top: 1200px">
    <h2>Similar jobs</h2>
    <a href="#">Enterprise Architect · Example AG · Easy Apply</a>
    <button>Easy Apply</button>
  </section>
</main>'''

EASY = '''
<main>
  <h1>Chief Architect</h1>
  <button aria-label="Easy Apply to Chief Architect at Example AG">Easy Apply</button>
  <a href="#" style="display:block; margin-top: 900px">Apply to Other role on company website</a>
</main>'''


def check(html):
    async def run():
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            await page.set_content(html)
            try:
                return await applier.is_easy_apply(page)
            finally:
                await browser.close()
    return asyncio.run(run())


def test_offsite_posting_with_easy_apply_further_down():
    assert check(OFFSITE) is False


def test_easy_apply_posting():
    assert check(EASY) is True


def test_no_apply_control():
    assert check('<main><h1>Closed</h1></main>') is False


CLOSED = '''
<div style="display:flex">
  <main style="width:800px">
    <p>Example AG</p>
    <h1>Lead Solutions Architect</h1>
    <p>Geneva, Switzerland</p>
    <p>No longer accepting applications</p>
    <section style="margin-top: 600px"><h2>Similar jobs</h2>
      <button aria-label="Easy Apply to Architect at Other AG">Easy Apply</button></section>
  </main>
  <aside><h2>Jobs for you</h2><button>Easy Apply</button></aside>
</div>'''


def test_easy_apply_buttons_beside_or_below_the_posting_do_not_count():
    # The side panel's button sits higher than the title; the similar job's far below.
    assert check(CLOSED) is False


def test_a_closed_posting_is_reported_closed():
    async def run():
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            await page.set_content(CLOSED)
            page.goto = lambda *a, **k: asyncio.sleep(0)
            try:
                return await applier.find_apply_url(page, 'https://example.invalid/jobs/view/1/')
            finally:
                await browser.close()
    assert asyncio.run(run()) == (None, 'closed')
