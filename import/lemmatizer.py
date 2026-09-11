"""
lemmatizer.py — Full-coverage English Lemmatizer using NLTK WordNet.
Handles single words, irregular forms, and multi-word verb phrases.
"""

from functools import lru_cache
import nltk
from nltk.corpus import wordnet
from nltk.stem import WordNetLemmatizer

# Ensure required NLTK corpora are downloaded quietly on import
for resource in [
    'corpora/wordnet',
    'corpora/omw-1.4',
    'tokenizers/punkt',
    'tokenizers/punkt_tab',
    'taggers/averaged_perceptron_tagger_eng'
]:
    try:
        nltk.data.find(resource)
    except LookupError:
        nltk.download(resource.split('/')[-1], quiet=True)

_lemmatizer = WordNetLemmatizer()

# Auxiliary verbs stripped when identifying core action in verb phrases
AUXILIARY_VERBS = {
    "have", "has", "had", "having",
    "be", "am", "is", "are", "was", "were", "been", "being",
    "will", "would", "shall", "should", "can", "could", "may", "might", "must",
    "do", "does", "did"
}


def _nltk_to_wordnet_pos(nltk_tag: str) -> str:
    """Map NLTK POS tags to WordNet POS tags."""
    if nltk_tag.startswith('J'):
        return wordnet.ADJ
    elif nltk_tag.startswith('V'):
        return wordnet.VERB
    elif nltk_tag.startswith('N'):
        return wordnet.NOUN
    elif nltk_tag.startswith('R'):
        return wordnet.ADV
    return wordnet.NOUN


@lru_cache(maxsize=16384)
def lemmatize_word(word: str, pos: str = wordnet.NOUN) -> str:
    """Lemmatizes a single word using NLTK WordNet."""
    return _lemmatizer.lemmatize(word.lower().strip(), pos=pos)


@lru_cache(maxsize=32768)
def lemmatize_phrase(phrase: str) -> str:
    """
    Lemmatizes single words or multi-word verb phrases.
    
    Examples:
      - 'have eaten' -> 'eat'
      - 'will be running' -> 'run'
      - 'phenomena' -> 'phenomenon'
    """
    tokens = nltk.word_tokenize(phrase.strip())
    if not tokens:
        return ""

    # Single-word processing with POS tagging
    if len(tokens) == 1:
        tagged = nltk.pos_tag(tokens)
        word, tag = tagged[0]
        wn_pos = _nltk_to_wordnet_pos(tag)
        return lemmatize_word(word, pos=wn_pos)

    # Multi-word verb phrase processing: extract main action verb
    main_verb = None
    for token in reversed(tokens):
        if token.lower() not in AUXILIARY_VERBS:
            main_verb = token
            break

    # Fall back to last token if phrase consists entirely of auxiliaries
    if not main_verb:
        main_verb = tokens[-1]

    # Treat main verb in verb phrases as a verb
    return lemmatize_word(main_verb, pos=wordnet.VERB)