"""Full mode or safe mode, and the one door to the logged-in LinkedIn session.

JOBSCRAPER_MODE=full  (default) scrapes and applies through a browser signed in
                      to the candidate's LinkedIn account.
JOBSCRAPER_MODE=safe  never automates that account. New jobs come from
                      LinkedIn's job-alert emails and descriptions from public
                      job pages. See DISCLAIMER.md and issue #20.

Every use of the persistent profile goes through open_linkedin_session(), and
in safe mode that raises. A code path that forgets to check the mode therefore
fails loudly instead of quietly using the account.
"""

import os

MODES = ('full', 'safe')
PROFILE_DIR = './chrome_profile'


class SafeModeViolation(RuntimeError):
    """Something tried to use the logged-in LinkedIn session in safe mode."""


def current_mode():
    """'full' or 'safe', read live so a change to .env needs no restart.

    An unrecognised value means safe, not full: a typo in a setting whose job
    is protecting the account must not quietly switch the protection off.
    """
    raw = os.environ.get('JOBSCRAPER_MODE', 'full').strip().lower()
    if raw in MODES:
        return raw
    print(f"JOBSCRAPER_MODE={raw!r} is not one of {', '.join(MODES)}; treating it as 'safe'.")
    return 'safe'


def safe_mode():
    return current_mode() == 'safe'


async def open_linkedin_session(playwright, **options):
    """Launch Chromium on the candidate's logged-in LinkedIn profile.

    The only place in the codebase that may do so. Raises SafeModeViolation
    in safe mode.
    """
    if safe_mode():
        raise SafeModeViolation(
            'Safe mode is on (JOBSCRAPER_MODE=safe): the logged-in LinkedIn session is '
            'never automated. Set JOBSCRAPER_MODE=full to use this feature, accepting '
            'the risk to the account described in DISCLAIMER.md.')
    os.makedirs(PROFILE_DIR, exist_ok=True)
    return await playwright.chromium.launch_persistent_context(
        user_data_dir=PROFILE_DIR, **options)
