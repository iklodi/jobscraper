"""A letter in another language gets its greeting and closing in that language too."""
from generator import localise_letter_formulas, sanitize_generated_texts

BODY = "Dear Oliver,\n\nJe souhaite postuler au poste.\n\nSincerely,\nJane Doe"


def test_french_letter_translates_greeting_and_closing():
    out = localise_letter_formulas(BODY, 'French', 'switzerland')
    lines = out.split('\n')
    assert lines[0] == 'Bonjour Oliver,'
    assert 'Meilleures salutations,' in lines
    assert 'Sincerely,' not in out and lines[-1] == 'Jane Doe'


def test_german_generic_addressee():
    out = localise_letter_formulas("Dear Hiring Team,\n\nText.\n\nBest regards,\nJane Doe", 'German', 'switzerland')
    assert out.startswith('Sehr geehrte Damen und Herren,')
    assert 'Freundliche Grüsse,' in out


def test_each_country_its_own_usage():
    letter = "Dear Hiring Team,\n\nText.\n\nBest regards,\nJane Doe"
    assert 'Mit freundlichen Grüßen' in localise_letter_formulas(letter, 'German', 'germany')
    assert 'Cordialement,' in localise_letter_formulas(letter, 'French', 'france')
    assert 'Cordialement,' in localise_letter_formulas(letter, 'French')         # country unknown
    assert 'Met vriendelijke groet,' in localise_letter_formulas(letter, 'Dutch', 'netherlands')


def test_the_letter_follows_the_office_it_is_addressed_to(monkeypatch):
    monkeypatch.setenv('HOME_COUNTRY', 'Switzerland')
    letter = "Dear Hiring Team,\n\nText.\n\nBest regards,\nJane Doe"
    texts = {'cover_letter_body': letter, 'display_company': 'X', 'company_hq': 'Berlin, Germany'}
    sanitize_generated_texts(texts, 'X', 'Berlin', 'German', 'German')
    assert 'Mit freundlichen Grüßen' in texts['cover_letter_body']
    texts = {'cover_letter_body': letter, 'display_company': 'X', 'company_hq': 'Bern'}
    sanitize_generated_texts(texts, 'X', 'Bern', 'German', 'German')      # unknown: home usage
    assert 'Freundliche Grüsse,' in texts['cover_letter_body']


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


ENGLISH_ON_FRENCH_AD = ("Dear Oliver,\n\nI am writing to apply for the role of product manager with your team. "
                        "My experience in enterprise architecture and the work I have done with clients "
                        "is a good match for the position, and I would welcome the chance to discuss it.\n\n"
                        "Sincerely,\nJane Doe")


def test_english_letter_asked_for_on_a_french_advert_stays_english():
    texts = {'cover_letter_body': ENGLISH_ON_FRENCH_AD, 'display_company': 'Example AG', 'company_hq': 'Bern'}
    sanitize_generated_texts(texts, 'Example AG', 'Bern', 'French')
    assert texts['cover_letter_body'] == ENGLISH_ON_FRENCH_AD


def test_french_letter_detected(monkeypatch):
    monkeypatch.setenv('HOME_COUNTRY', 'Switzerland')
    from generator import letter_language
    body = ("Dear Oliver,\n\nJe souhaite postuler au poste de chef de produit. Mon parcours combine une expertise "
            "technique et une vision stratégique, et je suis convaincu que mes compétences sont un atout pour votre "
            "équipe dans le cadre de vos projets.\n\nSincerely,\nJane Doe")
    assert letter_language(body) == 'French'
    texts = {'cover_letter_body': body, 'display_company': 'Example AG', 'company_hq': 'Bern'}
    sanitize_generated_texts(texts, 'Example AG', 'Bern', 'English')
    assert texts['cover_letter_body'].startswith('Bonjour Oliver,')
    assert 'Meilleures salutations,' in texts['cover_letter_body']
