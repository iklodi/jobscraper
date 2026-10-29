"""A letter in another language gets its greeting and closing in that language too."""
from generator import localise_letter_formulas, sanitize_generated_texts

BODY = "Dear Oliver,\n\nJe souhaite postuler au poste.\n\nSincerely,\nJane Doe"


def test_french_letter_translates_greeting_and_closing():
    out = localise_letter_formulas(BODY, 'French')
    lines = out.split('\n')
    assert lines[0] == 'Bonjour Oliver,'
    assert 'Meilleures salutations,' in lines
    assert 'Sincerely,' not in out and lines[-1] == 'Jane Doe'


def test_german_generic_addressee():
    out = localise_letter_formulas("Dear Hiring Team,\n\nText.\n\nBest regards,\nJane Doe", 'German')
    assert out.startswith('Sehr geehrte Damen und Herren,')
    assert 'Freundliche Grüsse,' in out


def test_english_letter_untouched():
    assert localise_letter_formulas(BODY, 'English') == BODY
    assert localise_letter_formulas(BODY, None) == BODY


def test_target_language_formulas_kept():
    body = "Bonjour Oliver,\n\nTexte.\n\nCordialement,\nJane Doe"
    assert localise_letter_formulas(body, 'French') == body


def test_sanitize_applies_it():
    texts = {'cover_letter_body': BODY, 'display_company': 'Example AG', 'company_hq': 'Bern, Switzerland'}
    sanitize_generated_texts(texts, 'Example AG', 'Bern', 'French')
    assert texts['cover_letter_body'].startswith('Bonjour Oliver,')
