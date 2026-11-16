#!/usr/bin/env python3
"""
build_word_relations.py — Extracts lexeme-to-lexeme relations (derivation,
compounding, cognates) for Greek from three open sources and stores them in
River's SQLite DB (bible.sqlite), where a later step can turn them into
"word families" for the Occurrences sidebar.

Sources (all open):
  strongs        openscriptures/strongs  strongsgreek.xml   (<strongs_derivation>)
  abbott_smith   Abbott-Smith TEI XML                        ("(< X)", "cog. X")
  wiktionary     kaikki.org Ancient Greek JSONL (wiktextract) (etymology
                 templates + derived/related term lists + part of speech)

Creates (idempotent, per-source refresh — rows for a source are deleted and
re-inserted only when that source's file is passed):
  lexicon_word_relations(strong, related_strong, relation, role, source, detail)
  lexicon_word_pos(strong, pos, source)

Relation vocabulary (direction: `strong` is the derived/compound word,
`related_strong` is what it comes from):
  derived_from     direct derivation, same stem (ἀγάπη -> ἀγαπάω)
  prefix_compound  related_strong is the stem of a prefixed word (ἐκβάλλω -> βάλλω)
  compound         related_strong is one stem of a multi-stem compound
  variant_of       prolonged/strengthened/variant form
  cognate          "from the same root as" (weak, symmetric in meaning)
  derived_term     Wiktionary "derived terms" list (weak: kind unknown)
  related          Wiktionary "related terms" / Strong's "compare" (weak)
`role` is 'stem' or 'prefix' for compound-type rows (prefix rows point at the
preposition/particle, e.g. ἐκ, so a consumer can ignore them), else NULL.

Usage:
  python3 build_word_relations.py --db bible.sqlite \\
      --strongs strongsgreek.xml \\
      --abbott abbott-smith.xml \\
      --kaikki kaikki.org-dictionary-AncientGreek.jsonl \\
      [--dry-run] [--report-tsv unresolved.tsv] [--audit "λέγω,βάλλω"]

Requires only the Python standard library.
"""

import argparse
import collections
import json
import re
import sqlite3
import sys
import unicodedata
import xml.etree.ElementTree as ET

XML_LANG = '{http://www.w3.org/XML/1998/namespace}lang'

SRC_STRONGS = 'strongs'
SRC_ABBOTT = 'abbott_smith'
SRC_WIKT = 'wiktionary'

STRUCTURAL = {'derived_from', 'prefix_compound', 'compound', 'variant_of'}

# ---------------------------------------------------------------------------
# Prefixes / prepositions. Canonical normalized form -> Strong's number, and
# every assimilated/elided spelling -> canonical. Used to label the prefix
# half of a compound (role='prefix') so consumers can ignore it.
# ---------------------------------------------------------------------------
PREFIX_STRONG = {
    'α': 'G1',        # alpha privative
    'ανα': 'G303', 'αντι': 'G473', 'απο': 'G575', 'δια': 'G1223',
    'εις': 'G1519', 'εκ': 'G1537', 'εν': 'G1722', 'επι': 'G1909',
    'κατα': 'G2596', 'μετα': 'G3326', 'παρα': 'G3844', 'περι': 'G4012',
    'προ': 'G4253', 'προς': 'G4314', 'συν': 'G4862', 'υπερ': 'G5228',
    'υπο': 'G5259',
}
PREFIX_FORMS = {
    'α': 'α', 'αν': 'α',
    'ανα': 'ανα', 'αντι': 'αντι', 'αντ': 'αντι', 'ανθ': 'αντι',
    'απο': 'απο', 'απ': 'απο', 'αφ': 'απο',
    'δια': 'δια', 'δι': 'δια', 'εις': 'εις',
    'εκ': 'εκ', 'εξ': 'εκ',
    'εν': 'εν', 'εγ': 'εν', 'εμ': 'εν', 'ελ': 'εν', 'ερ': 'εν',
    'επι': 'επι', 'επ': 'επι', 'εφ': 'επι',
    'κατα': 'κατα', 'κατ': 'κατα', 'καθ': 'κατα',
    'μετα': 'μετα', 'μετ': 'μετα', 'μεθ': 'μετα',
    'παρα': 'παρα', 'παρ': 'παρα', 'περι': 'περι', 'προ': 'προ', 'προς': 'προς',
    'συν': 'συν', 'συγ': 'συν', 'συμ': 'συν', 'συλ': 'συν', 'συρ': 'συν', 'συσ': 'συν',
    'υπερ': 'υπερ', 'υπο': 'υπο', 'υπ': 'υπο', 'υφ': 'υπο',
}
PREFIX_STRONGS = set(PREFIX_STRONG.values())

POS_MAP = {
    'noun': 'noun', 'verb': 'verb', 'adj': 'adjective', 'adv': 'adverb',
    'name': 'proper_noun', 'prep': 'preposition', 'conj': 'conjunction',
    'pron': 'pronoun', 'num': 'numeral', 'intj': 'interjection',
    'particle': 'particle', 'article': 'article', 'det': 'article',
    'postp': 'postposition',
}
SKIP_POS = {'prefix', 'suffix', 'infix', 'affix', 'circumfix', 'interfix',
            'character', 'symbol', 'phrase', 'proverb', 'contraction'}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def local(tag):
    return tag.rsplit('}', 1)[-1] if isinstance(tag, str) else tag


def norm(s):
    """Fold a Greek string for matching: NFD, drop all combining marks
    (accents, breathings, iota subscript, macrons), lowercase, final sigma ->
    sigma, drop hyphens/whitespace/apostrophes."""
    s = unicodedata.normalize('NFD', s)
    s = ''.join(c for c in s if unicodedata.category(c) != 'Mn')
    s = s.lower().replace('ς', 'σ')
    return re.sub(r"[\s\-\u2010\u2011'’ʼ*]+", '', s)


def strong_id(num, prefix='G'):
    try:
        return prefix + str(int(num))
    except (TypeError, ValueError):
        return None


BAD_ENTITY = re.compile(r'&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)')


def parse_entries(path):
    """Yield parsed <entry> elements from a file whether or not it has a valid
    single root (and regardless of default namespace)."""
    with open(path, encoding='utf-8') as f:
        text = f.read()
    for m in re.finditer(r'<entry\b.*?</entry>', text, re.DOTALL):
        chunk = BAD_ENTITY.sub('&amp;', m.group(0))
        try:
            yield ET.fromstring(chunk)
        except ET.ParseError:
            continue


class Resolver:
    """Maps a Greek lemma spelling to a unique Strong's-family ID in
    lexicon_words. Exact (accented) match wins; otherwise a diacritic-folded
    match must be unique — homographs are reported, never guessed."""

    def __init__(self, con):
        self.exact = collections.defaultdict(set)
        self.folded = collections.defaultdict(set)
        self.known = set()
        self.lexeme_of = {}
        for strong, lexeme in con.execute(
                "SELECT strong, lexeme FROM lexicon_words WHERE strong LIKE 'G%'"):
            if not lexeme:
                continue
            self.known.add(strong)
            self.lexeme_of[strong] = lexeme
            self.exact[unicodedata.normalize('NFC', lexeme)].add(strong)
            self.folded[norm(lexeme)].add(strong)
        self.unresolved = collections.Counter()
        self.ambiguous = collections.Counter()

    def resolve(self, form, source):
        form = unicodedata.normalize('NFC', form.strip())
        if not form:
            return None
        hit = self.exact.get(form)
        if hit and len(hit) == 1:
            return next(iter(hit))
        hit = self.folded.get(norm(form))
        if not hit:
            self.unresolved[(source, form)] += 1
            return None
        if len(hit) > 1:
            self.ambiguous[(source, form)] += 1
            return None
        return next(iter(hit))


# ---------------------------------------------------------------------------
# Shared classifier for "child derives from these components"
# ---------------------------------------------------------------------------
def classify_components(child, comps, variant=False):
    """comps: ordered list of resolved Strong's IDs for the structural parts.
    Returns [(related, relation, role)]."""
    comps = [c for c in comps if c and c != child]
    seen, uniq = set(), []
    for c in comps:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    comps = uniq
    if not comps:
        return []
    if len(comps) == 1:
        return [(comps[0], 'variant_of' if variant else 'derived_from', None)]
    stems = [c for c in comps if c not in PREFIX_STRONGS]
    prefixes = [c for c in comps if c in PREFIX_STRONGS]
    out = []
    if len(stems) == 1 and prefixes:
        out.append((stems[0], 'prefix_compound', 'stem'))
        out += [(p, 'prefix_compound', 'prefix') for p in prefixes]
    else:
        out += [(s, 'compound', 'stem') for s in stems]
        out += [(p, 'compound', 'prefix') for p in prefixes]
    return out


# ---------------------------------------------------------------------------
# Source 1: Strong's
# ---------------------------------------------------------------------------
VARIANT_CUE = re.compile(
    r'prolonged|strengthened|intensive|intensified|another form|variant|'
    r'variation|contracted|shorter form|lengthened|form of|by variation', re.I)
COGNATE_CUE = re.compile(r'same as|akin to|allied to|cognate', re.I)
COMPARE_CUE = re.compile(r'compare|\bcf\b', re.I)


def strongs_flat_derivation(el):
    """Flatten <strongs_derivation>, replacing each <strongsref> with «G:12»."""
    out = []
    if el.text:
        out.append(el.text)
    for c in el:
        if local(c.tag) == 'strongsref':
            lang = (c.get('language') or '').upper()
            num = (c.get('strongs') or '').strip()
            out.append('«%s:%s»' % ('G' if lang == 'GREEK' else 'H', num))
        else:
            out.append(''.join(c.itertext()))
        if c.tail:
            out.append(c.tail)
    return ''.join(out)


REF_RE = re.compile(r'«([GH]):(\d+)»')


def strongs_edges(path, known, stats):
    edges = []
    for entry in parse_entries(path):
        num = entry.get('strongs')
        child = strong_id(num)
        if not child:
            continue
        stats['entries'] += 1
        deriv = None
        for c in entry:
            if local(c.tag) == 'strongs_derivation':
                deriv = c
                break
        if deriv is None:
            continue
        flat = strongs_flat_derivation(deriv)
        prev_end = 0
        structural, weak = [], []
        first_struct_start = None
        for m in REF_RE.finditer(flat):
            cue = flat[prev_end:m.start()]
            prev_end = m.end()
            if m.group(1) != 'G':
                stats['hebrew_refs_skipped'] += 1
                continue
            ref = strong_id(m.group(2))
            if COMPARE_CUE.search(cue):
                weak.append((ref, 'related'))
            elif COGNATE_CUE.search(cue):
                weak.append((ref, 'cognate'))
            else:
                structural.append(ref)
                if first_struct_start is None:
                    first_struct_start = m.start()
        detail = re.sub(r'«[GH]:(\d+)»', lambda mm: mm.group(0)[1:-1], flat).strip()
        variant = bool(structural) and bool(
            VARIANT_CUE.search(flat[:first_struct_start])) and len(set(structural)) == 1
        for related, rel, role in classify_components(child, structural, variant):
            edges.append((child, related, rel, role, SRC_STRONGS, detail))
        for ref, rel in weak:
            if ref != child:
                edges.append((child, ref, rel, None, SRC_STRONGS, detail))
    return edges


# ---------------------------------------------------------------------------
# Source 2: Abbott-Smith
# ---------------------------------------------------------------------------
DERIV_PAREN = re.compile(r'\(\s*<\s*([^()]*)\)')
COG_RE = re.compile(r'\bcog\.\s*((?:⟦[^⟧]+⟧[,;\s]*(?:and\s+)?)+)')
GREEK_REF = re.compile(r'⟦([^⟧]+)⟧')


def as_flat(entry):
    """Flatten an entry to text, marking Greek <foreign> spans as ⟦…⟧ and
    dropping synonym blocks, notes, citations and LXX-correspondence segs."""
    parts = []

    def walk(e):
        tag = local(e.tag)
        if tag in ('re', 'note', 'ref'):
            return
        if tag == 'seg' and e.get('type') == 'septuagint':
            return
        if tag == 'foreign':
            if e.get(XML_LANG) == 'grc':
                parts.append('⟦' + ''.join(e.itertext()).strip() + '⟧')
            return
        if e.text:
            parts.append(e.text)
        for c in e:
            walk(c)
            if c.tail:
                parts.append(c.tail)

    walk(entry)
    return ''.join(parts)


def lemma_tokens(ref):
    """A ⟦…⟧ span may hold 'κράτος' or a list; return usable lemma words,
    skipping endings like '-ές' and bare articles."""
    toks = [t for t in re.split(r'[,;\s]+', ref) if t]
    return [t for t in toks if not t.startswith('-') and not t.endswith('-')
            and len(norm(t)) >= 2]


def abbott_edges(path, res, stats):
    edges = []
    for entry in parse_entries(path):
        n = entry.get('n') or ''
        stats['entries'] += 1
        m = re.search(r'\|\s*(G\d+[a-z]?)\b', n)
        child = m.group(1) if m else res.resolve(n.split('|')[0], SRC_ABBOTT)
        if not child or child not in res.known:
            continue
        flat = as_flat(entry)
        for dm in DERIV_PAREN.finditer(flat):
            comps = []
            for ref in GREEK_REF.findall(dm.group(1)):
                for tok in lemma_tokens(ref):
                    comps.append(res.resolve(tok, SRC_ABBOTT))
            detail = '(< %s)' % dm.group(1).strip()
            for related, rel, role in classify_components(child, comps):
                edges.append((child, related, rel, role, SRC_ABBOTT, detail))
        for cm in COG_RE.finditer(flat):
            for ref in GREEK_REF.findall(cm.group(1)):
                for tok in lemma_tokens(ref):
                    rel_id = res.resolve(tok, SRC_ABBOTT)
                    if rel_id and rel_id != child:
                        edges.append((child, rel_id, 'cognate', None, SRC_ABBOTT,
                                      'cog. ' + tok))
    return edges


# ---------------------------------------------------------------------------
# Source 3: Wiktionary via kaikki.org
# ---------------------------------------------------------------------------
AFFIX_TEMPLATES = {'suffix', 'suf', 'prefix', 'pre', 'affix', 'af', 'confix',
                   'con', 'compound', 'com'}
INLINE_MOD = re.compile(r'<[^>]*>')


def clean_part(p):
    p = INLINE_MOD.sub('', str(p))
    p = p.split('#', 1)[0]
    return p.strip()


def template_parts(args):
    """Positional args 2..N of an etymology template, cleaned."""
    keys = sorted(int(k) for k in args if str(k).isdigit() and int(k) >= 2)
    return [clean_part(args[str(k)]) for k in keys if clean_part(args[str(k)])]


def entry_is_lemma(obj):
    senses = obj.get('senses') or []
    if not senses:
        return True
    return not all(('form_of' in s or 'alt_of' in s) for s in senses)


def kaikki_edges(path, res, stats):
    edges, pos_rows = [], set()
    deferred = []  # (child_word, kind, target_word) resolved after first pass
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (obj.get('lang_code') or '') != 'grc':
                continue
            pos = obj.get('pos') or ''
            word = obj.get('word') or ''
            if not word or pos in SKIP_POS or not entry_is_lemma(obj):
                continue
            stats['entries'] += 1
            child = res.resolve(word, SRC_WIKT)
            if not child:
                continue
            if pos in POS_MAP:
                pos_rows.add((child, POS_MAP[pos], SRC_WIKT))

            for t in obj.get('etymology_templates') or []:
                if t.get('name') not in AFFIX_TEMPLATES:
                    continue
                args = t.get('args') or {}
                if args.get('1') != 'grc':
                    continue
                prefixes, suffixes, bases = [], [], []
                for p in template_parts(args):
                    if p.startswith('-') and not p.endswith('-'):
                        suffixes.append(p)
                    elif p.endswith('-') and not p.startswith('-'):
                        prefixes.append(p)
                    elif p.startswith('-') and p.endswith('-'):
                        continue  # infix / interfix
                    else:
                        bases.append(p)
                if not bases:
                    continue
                detail = '{{%s|grc|%s}}' % (t.get('name'), '|'.join(template_parts(args)))
                base_ids = [res.resolve(b, SRC_WIKT) for b in bases]
                # Only treat a hyphenated part as a *prefix* when it is a real
                # preposition/particle; other "prefixes" are bound stems.
                real_prefix = []
                for p in prefixes:
                    canon = PREFIX_FORMS.get(norm(p))
                    if canon:
                        real_prefix.append(PREFIX_STRONG[canon])
                if len(base_ids) == 1 and base_ids[0]:
                    b = base_ids[0]
                    if b == child:
                        continue
                    if real_prefix:
                        edges.append((child, b, 'prefix_compound', 'stem', SRC_WIKT, detail))
                        for p in real_prefix:
                            edges.append((child, p, 'prefix_compound', 'prefix', SRC_WIKT, detail))
                    elif suffixes and not prefixes:
                        edges.append((child, b, 'derived_from', None, SRC_WIKT, detail))
                    elif not prefixes and not suffixes:
                        continue
                    else:
                        # bound-stem prefix or mixed affixation: keep, but weak
                        edges.append((child, b, 'derived_term', None, SRC_WIKT, detail))
                elif len(bases) >= 2:
                    for b in base_ids:
                        if b and b != child:
                            edges.append((child, b, 'compound', 'stem', SRC_WIKT, detail))

            for item in obj.get('derived') or []:
                w = item.get('word') if isinstance(item, dict) else None
                if w:
                    deferred.append((word, 'derived', w))
            for item in obj.get('related') or []:
                w = item.get('word') if isinstance(item, dict) else None
                if w:
                    deferred.append((word, 'related', w))

    for src_word, kind, other_word in deferred:
        a = res.resolve(src_word, SRC_WIKT)
        b = res.resolve(other_word, SRC_WIKT)
        if not a or not b or a == b:
            continue
        if kind == 'derived':
            # src lists other as a derived term: other derives from src
            edges.append((b, a, 'derived_term', None, SRC_WIKT,
                          'derived term of %s' % src_word))
        else:
            edges.append((a, b, 'related', None, SRC_WIKT, 'related term'))
    return edges, pos_rows


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------
def ensure_schema(con):
    con.executescript("""
        CREATE TABLE IF NOT EXISTS lexicon_word_relations (
            strong         TEXT NOT NULL,
            related_strong TEXT NOT NULL,
            relation       TEXT NOT NULL,
            role           TEXT,
            source         TEXT NOT NULL,
            detail         TEXT,
            PRIMARY KEY (strong, related_strong, relation, source)
        );
        CREATE INDEX IF NOT EXISTS idx_word_relations_strong
            ON lexicon_word_relations(strong);
        CREATE INDEX IF NOT EXISTS idx_word_relations_related
            ON lexicon_word_relations(related_strong);

        CREATE TABLE IF NOT EXISTS lexicon_word_pos (
            strong TEXT NOT NULL,
            pos    TEXT NOT NULL,
            source TEXT NOT NULL,
            PRIMARY KEY (strong, pos, source)
        );
        CREATE INDEX IF NOT EXISTS idx_word_pos_strong ON lexicon_word_pos(strong);
    """)


def dedupe(edges, known, stats_by_src):
    out = {}
    for child, related, rel, role, src, detail in edges:
        if child not in known or related not in known:
            stats_by_src[src]['dropped_not_in_db'] += 1
            continue
        key = (child, related, rel, src)
        if key not in out:
            out[key] = (child, related, rel, role, src, detail)
    return list(out.values())


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def print_report(con, res, stats_by_src, all_edges, args):
    total_g = len(res.known)
    print('\n=== Coverage report ===')
    print('Greek lexemes in lexicon_words: %d' % total_g)

    by_src = collections.defaultdict(list)
    for e in all_edges:
        by_src[e[4]].append(e)

    def touched(edges):
        s = set()
        for e in edges:
            s.add(e[0])
            if e[3] != 'prefix':
                s.add(e[1])
        return s

    for src, edges in sorted(by_src.items()):
        rel_counts = collections.Counter(e[2] for e in edges)
        struct = [e for e in edges if e[2] in STRUCTURAL and e[3] != 'prefix']
        print('\n[%s] %d edges; %d lexemes touched (%d by structural edges)'
              % (src, len(edges), len(touched(edges)), len(touched(struct))))
        print('   relations: ' + ', '.join('%s=%d' % kv for kv in rel_counts.most_common()))
        st = stats_by_src.get(src, {})
        if st:
            print('   stats: ' + ', '.join('%s=%d' % kv for kv in sorted(st.items())))

    struct_all = [e for e in all_edges if e[2] in STRUCTURAL and e[3] != 'prefix']
    any_all = [e for e in all_edges if e[3] != 'prefix']
    print('\nAll sources: %d lexemes (%.1f%%) have >=1 structural relative; '
          '%d (%.1f%%) have >=1 relative of any kind'
          % (len(touched(struct_all)), 100.0 * len(touched(struct_all)) / max(total_g, 1),
             len(touched(any_all)), 100.0 * len(touched(any_all)) / max(total_g, 1)))

    pair_sources = collections.defaultdict(set)
    for child, related, rel, role, src, _ in all_edges:
        if role == 'prefix':
            continue
        pair_sources[frozenset((child, related))].add(src)
    counts = collections.Counter(len(v) for v in pair_sources.values())
    print('Pair agreement (undirected, excl. prefix rows): ' +
          ', '.join('%d source(s)=%d' % (k, counts[k]) for k in sorted(counts)))

    print('\nTop unresolved lemmas (no match in lexicon_words):')
    for (src, form), c in res.unresolved.most_common(args.top):
        print('   %-14s %-20s x%d' % (src, form, c))
    print('Top ambiguous lemmas (homographs; skipped, never guessed):')
    for (src, form), c in res.ambiguous.most_common(args.top):
        print('   %-14s %-20s x%d' % (src, form, c))

    if args.report_tsv:
        with open(args.report_tsv, 'w', encoding='utf-8') as f:
            f.write('kind\tsource\tform\tcount\n')
            for (src, form), c in res.unresolved.most_common():
                f.write('unresolved\t%s\t%s\t%d\n' % (src, form, c))
            for (src, form), c in res.ambiguous.most_common():
                f.write('ambiguous\t%s\t%s\t%d\n' % (src, form, c))
        print('\nWrote %s' % args.report_tsv)


def print_audit(res, all_edges, names):
    print('\n=== Audit ===')
    for name in names:
        name = name.strip()
        if not name:
            continue
        hits = res.exact.get(unicodedata.normalize('NFC', name)) or res.folded.get(norm(name))
        if not hits:
            print('\n%s: not found in lexicon_words' % name)
            continue
        for sid in sorted(hits):
            print('\n%s  %s' % (sid, res.lexeme_of.get(sid, '?')))
            lines = []
            for child, related, rel, role, src, detail in all_edges:
                if child == sid:
                    lines.append((src, '-> %s %s' % (related, res.lexeme_of.get(related, '?')),
                                  rel + (':' + role if role else '')))
                elif related == sid and role != 'prefix':
                    lines.append((src, '<- %s %s' % (child, res.lexeme_of.get(child, '?')),
                                  rel + (':' + role if role else '')))
            for src, text, rel in sorted(lines):
                print('   [%-12s] %-16s %s' % (src, rel, text))
            if not lines:
                print('   (no relations)')


DEFAULT_AUDIT = 'λέγω,ῥέω,ἀγαπάω,βάλλω,ἐκβάλλω,ἀποκατάστασις,ἵστημι'


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--db', default='bible.sqlite')
    ap.add_argument('--strongs', help='strongsgreek.xml (openscriptures/strongs)')
    ap.add_argument('--abbott', help='Abbott-Smith TEI XML')
    ap.add_argument('--kaikki', help='kaikki.org Ancient Greek JSONL')
    ap.add_argument('--dry-run', action='store_true', help='report only; do not write')
    ap.add_argument('--report-tsv', help='write unresolved/ambiguous lemmas to this TSV')
    ap.add_argument('--audit', default=DEFAULT_AUDIT,
                    help='comma-separated lexemes whose relatives are printed')
    ap.add_argument('--top', type=int, default=15)
    args = ap.parse_args()

    if not (args.strongs or args.abbott or args.kaikki):
        sys.exit('Pass at least one of --strongs / --abbott / --kaikki')

    con = sqlite3.connect(args.db, timeout=30.0)
    try:
        con.execute('SELECT 1 FROM lexicon_words LIMIT 1')
    except sqlite3.Error:
        sys.exit("lexicon_words not found in %s — run the lexicon importers first." % args.db)

    res = Resolver(con)
    stats = collections.defaultdict(lambda: collections.defaultdict(int))
    fresh = {}       # source -> deduped edges
    pos_rows = set()

    if args.strongs:
        print('Parsing Strong\'s derivations…')
        fresh[SRC_STRONGS] = strongs_edges(args.strongs, res.known, stats[SRC_STRONGS])
    if args.abbott:
        print('Parsing Abbott-Smith derivations…')
        fresh[SRC_ABBOTT] = abbott_edges(args.abbott, res, stats[SRC_ABBOTT])
    if args.kaikki:
        print('Streaming kaikki.org Ancient Greek dump…')
        fresh[SRC_WIKT], pos_rows = kaikki_edges(args.kaikki, res, stats[SRC_WIKT])

    all_new = []
    for src, edges in fresh.items():
        fresh[src] = dedupe(edges, res.known, stats)
        all_new += fresh[src]

    if not args.dry_run:
        ensure_schema(con)
        with con:
            for src, edges in fresh.items():
                con.execute('DELETE FROM lexicon_word_relations WHERE source = ?', (src,))
                con.executemany(
                    'INSERT OR IGNORE INTO lexicon_word_relations '
                    '(strong, related_strong, relation, role, source, detail) '
                    'VALUES (?,?,?,?,?,?)', edges)
            if args.kaikki:
                con.execute('DELETE FROM lexicon_word_pos WHERE source = ?', (SRC_WIKT,))
                con.executemany(
                    'INSERT OR IGNORE INTO lexicon_word_pos (strong, pos, source) VALUES (?,?,?)',
                    [r for r in pos_rows if r[0] in res.known])
        report_edges = [
            (r[0], r[1], r[2], r[3], r[4], r[5]) for r in con.execute(
                'SELECT strong, related_strong, relation, role, source, detail '
                'FROM lexicon_word_relations')]
        print('Wrote %d relation rows this run%s.' % (
            len(all_new), (' and %d POS rows' % len(pos_rows)) if args.kaikki else ''))
    else:
        report_edges = all_new
        print('Dry run — nothing written.')

    print_report(con, res, stats, report_edges, args)
    print_audit(res, report_edges, args.audit.split(','))
    con.close()


if __name__ == '__main__':
    main()
