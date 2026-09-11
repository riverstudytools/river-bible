# River Word Index Builder

This setup processes English word glosses in `bible.sqlite` to construct normalized word occurrence and gloss frequency tables for River's lexicon sidebar.

It uses **NLTK WordNet** to combine inflectional variants (e.g. *ate*, *eating*, *eats*) and complex multi-word verb phrases (e.g. *have eaten*, *will be running*) into single unified gloss statistics.

---

## Prerequisites

- **Python 3.8+**
- **pip**

---

## Setup Instructions

### 1. Place Files
Ensure `lemmatizer.py` and `build_word_index.py` are saved in the same directory.

### 2. Install NLTK
Install the Natural Language Toolkit dependency via pip:

```bash
pip install nltk

(Note: Required WordNet data corpora will automatically download on the first script execution.)
Usage
Run the build script against your SQLite database file:
Bash
python3 build_word_index.py --db /path/to/bible.sqlite
How Gloss Deduplication Works
    1. Brackets Removal: Bracketed supplied words like "[the]" or "{to rise}" are stripped out.
    2. Lemmatization: Glosses are passed to lemmatizer.py:
        ◦ Single words map to their base dictionary lemma ("phenomena" -> "phenomenon").
        ◦ Multi-word verb phrases strip auxiliary verbs to find the main action ("have eaten" -> "eat").
    3. Theological Case Preservation: Terms matching God or Lord/LORD retain their exact casing to keep theological distinctions intact.
    4. Display Selection: Multi-word variants accumulate into one count, and the script automatically selects the most frequently occurring literal surface form as the display label for that bucket in lexicon_gloss_stats.
