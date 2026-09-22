"""Safe mode must make the logged-in LinkedIn session unusable (issue #12)."""
import asyncio
import pathlib
import re

import pytest

import mode

ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.mark.parametrize('value, expected', [
    (None, 'full'),          # default
    ('full', 'full'),
    ('safe', 'safe'),
    (' SAFE ', 'safe'),
    ('saf', 'safe'),         # a typo must not switch protection off
    ('', 'safe'),
])
def test_current_mode(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv('JOBSCRAPER_MODE', raising=False)
    else:
        monkeypatch.setenv('JOBSCRAPER_MODE', value)
    assert mode.current_mode() == expected


class _Recorder:
    """A stand-in for Playwright that records whether a browser was launched."""
    def __init__(self):
        self.launched = False
        self.chromium = self

    async def launch_persistent_context(self, **kwargs):
        self.launched = True
        return 'browser'


def test_safe_mode_refuses_the_logged_in_session(monkeypatch):
    monkeypatch.setenv('JOBSCRAPER_MODE', 'safe')
    playwright = _Recorder()
    with pytest.raises(mode.SafeModeViolation):
        asyncio.run(mode.open_linkedin_session(playwright, headless=True))
    assert not playwright.launched


def test_full_mode_opens_it(monkeypatch, tmp_path):
    monkeypatch.setenv('JOBSCRAPER_MODE', 'full')
    monkeypatch.setattr(mode, 'PROFILE_DIR', str(tmp_path / 'profile'))
    playwright = _Recorder()
    assert asyncio.run(mode.open_linkedin_session(playwright, headless=True)) == 'browser'
    assert playwright.launched


def test_nothing_bypasses_the_guard():
    """Only mode.py may launch the persistent profile directly. A new call site
    elsewhere would ignore safe mode, so it fails this test instead."""
    offenders = []
    for path in ROOT.glob('*.py'):
        if path.name == 'mode.py':
            continue
        if re.search(r'launch_persistent_context\s*\(', path.read_text(encoding='utf-8')):
            offenders.append(path.name)
    assert offenders == []
