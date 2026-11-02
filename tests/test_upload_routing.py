"""Which stored document goes into which upload field (applier.upload_documents)."""
import asyncio

from playwright.async_api import async_playwright

import applier

PAGE = '''<form>
  <input type="file" data-jsapply="0">
  <input type="file" data-jsapply="1">
</form>'''


def route(tmp_path, labels, chosen):
    docs = {}
    for key in ('cv', 'cover_letter', 'diplomas', 'reference_letter'):
        docs[key] = str(tmp_path / f'{key}.pdf')
        (tmp_path / f'{key}.pdf').write_bytes(b'%PDF-1.4')
    fields = [{'idx': i, 'type': 'file', 'label': label} for i, label in enumerate(labels)]
    uploads = [{'idx': i, 'document': doc} for i, doc in enumerate(chosen)]

    class Trace:
        def __init__(self):
            self.sent = []

        def record_upload(self, field, path):
            self.sent.append((field, path.rsplit('/', 1)[-1]))

    async def run():
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            await page.set_content(PAGE)
            trace = Trace()
            await applier.upload_documents(page, fields, docs['cv'], docs['cover_letter'],
                                           attachments=docs, trace=trace, uploads=uploads)
            await browser.close()
            return trace.sent
    return asyncio.run(run())


def test_a_generic_slot_gets_the_bundle_even_when_the_model_picks_the_letter(tmp_path):
    sent = route(tmp_path, ['Resume', 'Additional files'], ['cv', 'reference_letter'])
    assert sent == [('Resume', 'cv.pdf'), ('Additional files', 'diplomas.pdf')]


def test_a_field_asking_for_a_reference_gets_the_letter(tmp_path):
    sent = route(tmp_path, ['CV', 'Reference letter'], ['cv', 'reference_letter'])
    assert sent[1] == ('Reference letter', 'reference_letter.pdf')
