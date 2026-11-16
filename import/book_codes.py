#!/usr/bin/env python3
"""
book_codes.py — Centralized Book Name Resolver & Code Mapping for River importers.

Provides a unified dictionary mapping common 3-letter, USFM, OSIS, Swete LXX, Roman numerals,
and custom book codes/abbreviations to canonical full book names, along with a robust fallback resolver.
"""

# Canonical 66-book Protestant order
CANONICAL_BOOKS = [
    'Genesis', 'Exodus', 'Leviticus', 'Numbers', 'Deuteronomy', 'Joshua', 'Judges', 'Ruth',
    '1 Samuel', '2 Samuel', '1 Kings', '2 Kings', '1 Chronicles', '2 Chronicles', 'Ezra', 'Nehemiah',
    'Esther', 'Job', 'Psalms', 'Proverbs', 'Ecclesiastes', 'Song of Solomon', 'Isaiah', 'Jeremiah',
    'Lamentations', 'Ezekiel', 'Daniel', 'Hosea', 'Joel', 'Amos', 'Obadiah', 'Jonah', 'Micah', 'Nahum',
    'Habakkuk', 'Zephaniah', 'Haggai', 'Zechariah', 'Malachi',
    'Matthew', 'Mark', 'Luke', 'John', 'Acts', 'Romans', '1 Corinthians', '2 Corinthians', 'Galatians',
    'Ephesians', 'Philippians', 'Colossians', '1 Thessalonians', '2 Thessalonians', '1 Timothy',
    '2 Timothy', 'Titus', 'Philemon', 'Hebrews', 'James', '1 Peter', '2 Peter', '1 John', '2 John',
    '3 John', 'Jude', 'Revelation'
]

# Lookup dictionary for full names and common spelling variants
FULL_NAME_LOOKUP = {b.lower(): b for b in CANONICAL_BOOKS}
FULL_NAME_LOOKUP.update({
    'psalm': 'Psalms',
    'song of songs': 'Song of Solomon',
    'canticles': 'Song of Solomon',
    'revelations': 'Revelation',
    'revelation of john': 'Revelation',
    'apocalypse': 'Revelation',

    # Roman Numeral Variants
    'i samuel': '1 Samuel',
    'ii samuel': '2 Samuel',
    'i kings': '1 Kings',
    'ii kings': '2 Kings',
    'i chronicles': '1 Chronicles',
    'ii chronicles': '2 Chronicles',
    'i corinthians': '1 Corinthians',
    'ii corinthians': '2 Corinthians',
    'i thessalonians': '1 Thessalonians',
    'ii thessalonians': '2 Thessalonians',
    'i timothy': '1 Timothy',
    'ii timothy': '2 Timothy',
    'i peter': '1 Peter',
    'ii peter': '2 Peter',
    'i john': '1 John',
    'ii john': '2 John',
    'iii john': '3 John',
    'i esdras': '1 Esdras',
    'ii esdras': '2 Esdras',
    'i maccabees': '1 Maccabees',
    'ii maccabees': '2 Maccabees',
    'iii maccabees': '3 Maccabees',
    'iv maccabees': '4 Maccabees',

    # Douay-Rheims / Vulgate book-name variants (English Douay-Rheims
    # uses Latin-derived names throughout — "Josue", "Machabees",
    # "Paralipomenon", "Osee", "Sophonias", "Aggeus", "Canticle of
    # Canticles", "Apocalypse" — plus a handful of French-derived
    # spellings ("Isaie", "Jeremie", "Malachie") observed in the same
    # cross-reference dataset. 3/4 Kings follow the Vulgate's four-book
    # Samuel+Kings scheme (1-2 Kings = Protestant 1-2 Samuel, 3-4 Kings =
    # Protestant 1-2 Kings) — this dataset uses '1-samuel'/'2-samuel' for
    # the first pair directly, so only 3/4 need the alias here.
    '3 kings': '1 Kings',
    '4 kings': '2 Kings',
    '1 paralipomenon': '1 Chronicles',
    '2 paralipomenon': '2 Chronicles',
    'josue': 'Joshua',
    'abdias': 'Obadiah',
    'osee': 'Hosea',
    'micheas': 'Micah',
    'habacuc': 'Habakkuk',
    'sophonias': 'Zephaniah',
    'aggeus': 'Haggai',
    'zacharias': 'Zechariah',
    'malachie': 'Malachi',
    'isaie': 'Isaiah',
    'jeremie': 'Jeremiah',
    'ezechiel': 'Ezekiel',
    'jonas': 'Jonah',
    'canticle of canticles': 'Song of Solomon',
    '1 machabees': '1 Maccabees',
    '2 machabees': '2 Maccabees',
    'ecclesiasticus': 'Sirach',
    'tobias': 'Tobit',
    'wisdom': 'Wisdom of Solomon',

    # Greek Esther / Greek Daniel are whole-book Greek texts (Esther with its
    # additions inline; Daniel with 3:24-90 etc.). "Additions to Esther" is a
    # different thing: just the additions, as a book of their own.
    'greek esther': 'Greek Esther',
    'esther (greek)': 'Greek Esther',
    'esther greek': 'Greek Esther',
    'greek daniel': 'Greek Daniel',
    'daniel (greek)': 'Greek Daniel',
    'daniel greek': 'Greek Daniel',
    'additions to esther': 'Additions to Esther',
    'rest of esther': 'Additions to Esther',
    'the rest of esther': 'Additions to Esther',
})

# Centralized Book Code Map covering standard OT/NT, Apocrypha, Swete LXX, and variant source codes
BOOK_CODE_MAP = {
    # Old Testament
    'GEN': 'Genesis', 'EXO': 'Exodus', 'LEV': 'Leviticus', 'NUM': 'Numbers', 'DEU': 'Deuteronomy',
    'JOSH': 'Joshua', 'JOS': 'Joshua', 'JDG': 'Judges', 'RUT': 'Ruth', 
    '1SAM': '1 Samuel', '1SA': '1 Samuel', '2SAM': '2 Samuel', '2SA': '2 Samuel', 
    '1KGS': '1 Kings', '1KI': '1 Kings', '2KGS': '2 Kings', '2KI': '2 Kings',
    '1CHR': '1 Chronicles', '1CH': '1 Chronicles', '2CHR': '2 Chronicles', '2CH': '2 Chronicles',
    'EZR': 'Ezra', 'NEH': 'Nehemiah', 'EST': 'Esther', 'JOB': 'Job', 
    'PSA': 'Psalms', 'PSM': 'Psalms', 'PS': 'Psalms', 'PSS': 'Psalms',
    'PRO': 'Proverbs', 'PRV': 'Proverbs', 'ECC': 'Ecclesiastes', 
    'SNG': 'Song of Solomon', 'SOS': 'Song of Solomon', 'CANT': 'Song of Solomon', 'SOLOMON\'S SONG': 'Song of Solomon',
    'ISA': 'Isaiah', 'JER': 'Jeremiah', 'LAM': 'Lamentations', 'EZK': 'Ezekiel', 'EZE': 'Ezekiel',
    'DAN': 'Daniel', 'HOS': 'Hosea', 'JOL': 'Joel', 'JOE': 'Joel', 'AMO': 'Amos', 
    'OBA': 'Obadiah', 'JON': 'Jonah', 'MIC': 'Micah', 'NAM': 'Nahum', 'NAH': 'Nahum', 
    'HAB': 'Habakkuk', 'ZEP': 'Zephaniah', 'HAG': 'Haggai', 'ZEC': 'Zechariah', 'MAL': 'Malachi',

    # New Testament
    'MAT': 'Matthew', 'MATT': 'Matthew', 'MT': 'Matthew',
    'MRK': 'Mark', 'MARK': 'Mark', 'MAR': 'Mark', 'MR': 'Mark',
    'LUK': 'Luke', 'LUKE': 'Luke', 'LU': 'Luke',
    'JHN': 'John', 'JOH': 'John', 'JOHN': 'John',
    'ACT': 'Acts', 'ACTS': 'Acts', 'AC': 'Acts',
    'ROM': 'Romans', 'RO': 'Romans',
    '1CO': '1 Corinthians', '1COR': '1 Corinthians', '1-COR': '1 Corinthians', '1_CO': '1 Corinthians',
    '2CO': '2 Corinthians', '2COR': '2 Corinthians', '2-COR': '2 Corinthians', '2_CO': '2 Corinthians',
    'GAL': 'Galatians', 'GA': 'Galatians',
    'EPH': 'Ephesians',
    'PHP': 'Philippians', 'PHIL': 'Philippians', 'PHI': 'Philippians',
    'COL': 'Colossians',
    '1TH': '1 Thessalonians', '1THS': '1 Thessalonians', '1THES': '1 Thessalonians', '1THESS': '1 Thessalonians',
    '2TH': '2 Thessalonians', '2THS': '2 Thessalonians', '2THES': '2 Thessalonians', '2THESS': '2 Thessalonians',
    '1TI': '1 Timothy', '1TIM': '1 Timothy',
    '2TI': '2 Timothy', '2TIM': '2 Timothy',
    'TIT': 'Titus', 'TITUS': 'Titus',
    'PHM': 'Philemon', 'PHLE': 'Philemon', 'PHILEM': 'Philemon', 'PHLM': 'Philemon',
    'HEB': 'Hebrews', 'HEBREWS': 'Hebrews',
    'JAM': 'James', 'JAS': 'James', 'JAMES': 'James',
    '1PE': '1 Peter', '1PTR': '1 Peter', '1PET': '1 Peter',
    '2PE': '2 Peter', '2PTR': '2 Peter', '2PET': '2 Peter',
    '1JO': '1 John', '1JN': '1 John', '1JHN': '1 John', '1JOHN': '1 John',
    '2JO': '2 John', '2JN': '2 John', '2JHN': '2 John', '2JOHN': '2 John',
    '3JO': '3 John', '3JN': '3 John', '3JHN': '3 John', '3JOHN': '3 John',
    'JUD': 'Jude', 'JUDE': 'Jude',
    'REV': 'Revelation', 'REVELATION': 'Revelation', 'APO': 'Revelation', 'APOC': 'Revelation', 'RE': 'Revelation',

    # Extended Canon / Apocrypha / Swete LXX Codes
    'ESG': 'Greek Esther', 'ESTG': 'Greek Esther', 'ESTGR': 'Greek Esther',
    'ADE': 'Additions to Esther', 'ADDESTH': 'Additions to Esther',
    'DAG': 'Greek Daniel', 'DAT': 'Greek Daniel',
    'BET': 'Bel and the Dragon', 'BEL': 'Bel and the Dragon',
    'SUT': 'Susanna', 'SUS': 'Susanna',
    'EPJ': 'Letter of Jeremiah', 'LJE': 'Letter of Jeremiah',
    'TBS': 'Tobit', 'TOB': 'Tobit',
    'SIP': 'Sirach', 'SIR': 'Sirach',
    'SOL': 'Psalms of Solomon', 'PSSOL': 'Psalms of Solomon',
    'ODE': 'Odes',
    'JDT': 'Judith', 'WIS': 'Wisdom of Solomon',
    'BAR': 'Baruch', 'S3Y': 'Song of the Three Children',
    '1MA': '1 Maccabees', '2MA': '2 Maccabees', '3MA': '3 Maccabees', '4MA': '4 Maccabees',
    '1ES': '1 Esdras', '2ES': '2 Esdras',
    'MAN': 'Prayer of Manasseh', 'PS2': 'Psalm 151', 'ENO': 'Enoch', '1EN': 'Enoch', 'JUB': 'Jubilees',
    '1MQ': '1 Meqabyan', '2MQ': '2 Meqabyan', '3MQ': '3 Meqabyan'
}


def resolve_canonical_name(raw_code):
    """
    Resolves any input code or filename stem to full canonical book name.
    """
    if not raw_code:
        return None

    raw_str = str(raw_code).strip()
    code_upper = raw_str.upper()

    # 1. Direct code lookup
    if code_upper in BOOK_CODE_MAP:
        return BOOK_CODE_MAP[code_upper]

    # 2. Full name or common variant lookup
    raw_lower = raw_str.lower()
    if raw_lower in FULL_NAME_LOOKUP:
        return FULL_NAME_LOOKUP[raw_lower]

    return None