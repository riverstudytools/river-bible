#!/usr/bin/env python3
"""
build_word_families.py — Turns the raw lexeme relations produced by
build_word_relations.py into per-word "family" rows that River's Occurrences
sidebar can offer as an opt-in filter, and derives one part of speech per
lemma from the interlinear tokens' own morphology codes.

Reads:   lexicon_words, lexicon_word_relations, words (parsing column),
         lexicon_word_occurrences (optional, for coverage stats only)
Rebuilds (drop + create, like build_word_index.py):
         lexicon_word_family(strong, member_strong, tier, support, kind, sources)
Refreshes (rows with source='tokens' only; Wiktionary rows are left alone):
         lexicon_word_pos(strong, pos, source)

Tiers (each is opt-in in the UI; lower tiers are cleaner):
  1  Same word family   Words linked by direct derivation (derived_from /
                        variant_of). Each word keeps only its best-supported
                        parent, so two trees are never merged just because
                        one source disagrees with another.
  2  Compounds          ONE hop over compound / prefix-compound links, from a
                        family to the families of its stems or of words built
                        on it. Deliberately not transitive: ἐκβάλλω reaches
                        βάλλω, but not every other βάλλω compound.
  3  Possible relatives Weak, single-hop links (cognate, "compare",
                        Wiktionary derived/related term lists). Listed as
                        individual words, never expanded into their families.
  (4 is reserved for a future by-meaning source; the UI renders it if rows
   with tier=4 ever appear.)

`support` = number of distinct sources behind the link that placed the member
(a proxy for confidence); `sources` lists them.

Usage:
    python3 build_word_families.py --db bible.sqlite [--dry-run]
        [--max-tier1 40] [--audit "ἀγαπάω,βάλλω,λέγω"] [--top 15]
"""

import argparse
import collections
import sqlite3
import sys
import unicodedata

TIER1_KINDS = {'derived_from', 'variant_of'}
TIER2_KINDS = {'prefix_compound', 'compound'}
TIER3_KINDS = {'cognate', 'related', 'derived_term'}
SOURCE_RANK = {'strongs': 0, 'abbott_smith': 1, 'wiktionary': 2}

# Robinson morphology code (first segment) -> POS vocabulary shared with
# build_word_relations.py's Wiktionary POS map.
ROBINSON_POS = {
    'N': 'noun', 'A': 'adjective', 'V': 'verb', 'T': 'article',
    'P': 'pronoun', 'R': 'pronoun', 'D': 'pronoun', 'K': 'pronoun',
    'I': 'pronoun', 'X': 'pronoun', 'C': 'pronoun', 'F': 'pronoun',
    'S': 'pronoun', 'CONJ': 'conjunction', 'PREP': 'preposition',
    'ADV': 'adverb', 'PRT': 'particle', 'COND': 'particle', 'INJ': 'interjection',
}


def fold(s):
    s = unicodedata.normalize('NFD', s or '')
    s = ''.join(c for c in s if unicodedata.category(c) != 'Mn')
    return s.lower().replace('ς', 'σ')


class DSU:
    def __init__(self):
        self.p = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


# ---------------------------------------------------------------------------
# POS from tokens
# ---------------------------------------------------------------------------
def build_token_pos(con, known):
    """One POS per lemma: the most common category among its tokens'
    Robinson codes. Returns {strong: pos}."""
    try:
        rows = con.execute("""
            SELECT (CASE WHEN w.strong LIKE 'G%' OR w.strong LIKE 'H%' THEN w.strong
                         ELSE (CASE WHEN w.lang = 'Hebrew' THEN 'H' ELSE 'G' END) || w.strong END) AS s,
                   CASE WHEN instr(w.parsing, '-') > 0
                        THEN substr(w.parsing, 1, instr(w.parsing, '-') - 1)
                        ELSE w.parsing END AS code,
                   count(*) AS n
            FROM words w
            WHERE w.strong IS NOT NULL AND trim(w.strong) != ''
              AND w.parsing IS NOT NULL AND trim(w.parsing) != ''
            GROUP BY s, code
        """).fetchall()
    except sqlite3.Error as e:
        print('  (skipping token POS: %s)' % e)
        return {}
    votes = collections.defaultdict(collections.Counter)
    for s, code, n in rows:
        if s in known and code in ROBINSON_POS:
            votes[s][ROBINSON_POS[code]] += n
    return {s: c.most_common(1)[0][0] for s, c in votes.items()}


# ---------------------------------------------------------------------------
# Relations -> tiers
# ---------------------------------------------------------------------------
def load_edges(con, known):
    """Returns (tier1, tier2, tier3) where each maps a (child, related) pair to
    the set of sources that assert it, restricted to lexemes in lexicon_words.
    Prefix rows (role='prefix') are ignored: they point at prepositions."""
    tier1 = collections.defaultdict(set)
    tier2 = collections.defaultdict(set)
    tier3 = collections.defaultdict(set)
    for child, related, rel, role, src in con.execute(
            'SELECT strong, related_strong, relation, role, source FROM lexicon_word_relations'):
        if child not in known or related not in known or child == related:
            continue
        if role == 'prefix':
            continue
        if rel in TIER1_KINDS:
            tier1[(child, related)].add(src)
        elif rel in TIER2_KINDS:
            tier2[(child, related)].add(src)
        elif rel in TIER3_KINDS:
            tier3[frozenset((child, related))].add(src)
    return tier1, tier2, tier3


def edge_key(sources):
    return (-len(sources), min(SOURCE_RANK.get(s, 9) for s in sources))


def pick_parents(tier1):
    """Each child keeps only its best-supported parent(s): most sources first,
    then source priority (Strong's > Abbott-Smith > Wiktionary). Ties keep all."""
    by_child = collections.defaultdict(list)
    for (child, parent), srcs in tier1.items():
        by_child[child].append((edge_key(srcs), parent, srcs))
    kept = {}
    dropped = 0
    for child, cands in by_child.items():
        best = min(k for k, _, _ in cands)
        for k, parent, srcs in cands:
            if k == best:
                kept[(child, parent)] = srcs
            else:
                dropped += 1
    return kept, dropped


def components(edges):
    dsu = DSU()
    for (a, b) in edges:
        dsu.union(a, b)
    groups = collections.defaultdict(set)
    for (a, b) in edges:
        groups[dsu.find(a)].update((a, b))
    return list(groups.values())


def split_oversize(edges, max_size, warnings):
    """Connected components of the tier-1 edges. A component over max_size is
    re-cut using only stronger edges (Strong's-backed or multi-source, then
    multi-source only); whatever is still too big is kept and reported."""
    comps = components(edges)
    out = []

    def strong_enough(srcs, level):
        if level == 1:
            return 'strongs' in srcs or len(srcs) >= 2
        return len(srcs) >= 2

    def recut(nodes, level):
        if len(nodes) <= max_size or level > 2:
            if len(nodes) > max_size:
                warnings.append(nodes)
            return [nodes]
        sub = {k: v for k, v in edges.items()
               if k[0] in nodes and k[1] in nodes and strong_enough(v, level)}
        pieces = components(sub)
        covered = set().union(*pieces) if pieces else set()
        result = []
        for piece in pieces:
            result += recut(piece, level + 1) if len(piece) > max_size else [piece]
        # nodes that lost all their edges become singletons (dropped from families)
        _ = covered
        return result

    for c in comps:
        out += recut(c, 1) if len(c) > max_size else [c]
    return out


def build_families(tier1_edges, tier2, tier3, max_size):
    warnings = []
    kept, dropped_parents = pick_parents(tier1_edges)
    comps = split_oversize(kept, max_size, warnings)

    comp_of = {}
    for i, c in enumerate(comps):
        for n in c:
            comp_of[n] = i

    # sources incident to each node's kept tier-1 edges (for `support`)
    node_sources = collections.defaultdict(set)
    for (a, b), srcs in kept.items():
        if a in comp_of and b in comp_of and comp_of[a] == comp_of[b]:
            node_sources[a] |= srcs
            node_sources[b] |= srcs

    rows = {}  # (strong, member) -> (tier, sources set, kind)

    def put(strong, member, tier, srcs, kind):
        if strong == member:
            return
        cur = rows.get((strong, member))
        if cur is None or tier < cur[0]:
            rows[(strong, member)] = (tier, set(srcs), kind)

    # Tier 1
    for c in comps:
        for x in c:
            for m in c:
                put(x, m, 1, node_sources.get(m, set()) or node_sources.get(x, set()), 'same_family')

    # Tier 2: one hop between components over compound links
    link = collections.defaultdict(lambda: collections.defaultdict(set))  # comp -> comp -> sources
    single_link = collections.defaultdict(lambda: collections.defaultdict(set))  # node-level for words with no tier-1 family
    for (child, stem), srcs in tier2.items():
        cc = comp_of.get(child)
        sc = comp_of.get(stem)
        if cc is not None and sc is not None and cc == sc:
            continue
        if cc is not None and sc is not None:
            link[cc][sc] |= srcs
            link[sc][cc] |= srcs
        elif cc is not None:
            link[cc][('n', stem)] |= srcs
            single_link[stem][('c', cc)] |= srcs
        elif sc is not None:
            link[sc][('n', child)] |= srcs
            single_link[child][('c', sc)] |= srcs
        else:
            single_link[child][('n', stem)] |= srcs
            single_link[stem][('n', child)] |= srcs

    def members_of(ref):
        if isinstance(ref, tuple):
            return {ref[1]} if ref[0] == 'n' else comps[ref[1]]
        return comps[ref]

    for cid, targets in link.items():
        for ref, srcs in targets.items():
            for x in comps[cid]:
                for m in members_of(ref):
                    put(x, m, 2, srcs, 'compound')
    for node, targets in single_link.items():
        for ref, srcs in targets.items():
            for m in members_of(ref):
                put(node, m, 2, srcs, 'compound')

    # Tier 3: weak links, single words only
    for pair, srcs in tier3.items():
        a, b = tuple(pair)
        for x in (comps[comp_of[a]] if a in comp_of else {a}):
            put(x, b, 3, srcs, 'weak')
        for x in (comps[comp_of[b]] if b in comp_of else {b}):
            put(x, a, 3, srcs, 'weak')

    return rows, comps, warnings, dropped_parents


# ---------------------------------------------------------------------------
# DB + report
# ---------------------------------------------------------------------------
def write_tables(con, rows, token_pos):
    con.executescript("""
        DROP TABLE IF EXISTS lexicon_word_family;
        CREATE TABLE lexicon_word_family (
            strong        TEXT NOT NULL,
            member_strong TEXT NOT NULL,
            tier          INTEGER NOT NULL,
            support       INTEGER NOT NULL DEFAULT 1,
            kind          TEXT,
            sources       TEXT,
            PRIMARY KEY (strong, member_strong)
        );
        CREATE INDEX idx_word_family_strong ON lexicon_word_family(strong, tier);

        CREATE TABLE IF NOT EXISTS lexicon_word_pos (
            strong TEXT NOT NULL,
            pos    TEXT NOT NULL,
            source TEXT NOT NULL,
            PRIMARY KEY (strong, pos, source)
        );
        CREATE INDEX IF NOT EXISTS idx_word_pos_strong ON lexicon_word_pos(strong);
    """)
    con.executemany(
        'INSERT INTO lexicon_word_family (strong, member_strong, tier, support, kind, sources) '
        'VALUES (?,?,?,?,?,?)',
        [(s, m, tier, max(len(srcs), 1), kind, ','.join(sorted(srcs)))
         for (s, m), (tier, srcs, kind) in rows.items()])
    con.execute("DELETE FROM lexicon_word_pos WHERE source = 'tokens'")
    con.executemany('INSERT OR IGNORE INTO lexicon_word_pos (strong, pos, source) VALUES (?,?,?)',
                    [(s, p, 'tokens') for s, p in token_pos.items()])


def report(con, rows, comps, warnings, dropped, lexeme_of, token_pos, args):
    total = len(lexeme_of)
    by_tier = collections.Counter(t for (t, _, _) in rows.values())
    has = collections.defaultdict(set)
    for (s, _), (t, _, _) in rows.items():
        has[t].add(s)
    print('\n=== Family report ===')
    print('Greek lexemes: %d; token POS assigned to %d' % (total, len(token_pos)))
    print('Rows by tier: ' + ', '.join('tier %d=%d' % (t, by_tier[t]) for t in sorted(by_tier)))
    for t in sorted(has):
        print('  tier %d: %d lexemes (%.1f%%) have at least one member'
              % (t, len(has[t]), 100.0 * len(has[t]) / max(total, 1)))
    print('Tier-1 families: %d (parents dropped in favor of better-supported ones: %d)'
          % (len(comps), dropped))
    sizes = collections.Counter(min(len(c), 10) for c in comps)
    print('  size distribution: ' + ', '.join(
        '%s=%d' % (('10+' if k == 10 else k), v) for k, v in sorted(sizes.items())))
    print('  largest families:')
    for c in sorted(comps, key=len, reverse=True)[:args.top]:
        names = sorted(c, key=lambda s: int(''.join(ch for ch in s if ch.isdigit()) or 0))
        print('    %3d  %s' % (len(c), ' '.join(lexeme_of.get(n, n) for n in names[:8])
                              + (' …' if len(names) > 8 else '')))
    if warnings:
        print('WARNING: %d tier-1 families still exceed --max-tier1 after re-cutting '
              '(inspect for lumped words):' % len(warnings))
        for c in warnings[:args.top]:
            print('    %3d  %s …' % (len(c), ' '.join(lexeme_of.get(n, n) for n in sorted(c)[:8])))
    try:
        cov = con.execute("""
            SELECT SUM(t.n), SUM(CASE WHEN f.strong IS NOT NULL THEN t.n ELSE 0 END)
            FROM (SELECT strong, SUM(occurrence_count) AS n FROM lexicon_word_occurrences
                  WHERE strong LIKE 'G%' GROUP BY strong) t
            LEFT JOIN (SELECT DISTINCT strong FROM lexicon_word_family WHERE tier = 1) f
                   ON f.strong = t.strong
        """).fetchone()
        if cov and cov[0]:
            print('Occurrence-weighted: %.1f%% of Greek word occurrences belong to a word with a '
                  'tier-1 family' % (100.0 * (cov[1] or 0) / cov[0]))
    except sqlite3.Error:
        pass


def audit(rows, lexeme_of, names, token_pos):
    print('\n=== Audit ===')
    by_lex = collections.defaultdict(list)
    for s, lx in lexeme_of.items():
        by_lex[fold(lx)].append(s)
    for name in names:
        name = name.strip()
        for sid in sorted(by_lex.get(fold(name), [])):
            print('\n%s  %s  (%s)' % (sid, lexeme_of[sid], token_pos.get(sid, '?')))
            mem = sorted(((t, m) for (s, m), (t, _, _) in rows.items() if s == sid))
            if not mem:
                print('   (no family)')
            for t, m in mem[:60]:
                _, srcs, _ = rows[(sid, m)]
                print('   tier %d  %-8s %-18s %-8s %s'
                      % (t, m, lexeme_of.get(m, '?'), token_pos.get(m, '?'), ','.join(sorted(srcs))))
            if len(mem) > 60:
                print('   … %d more' % (len(mem) - 60))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--db', default='bible.sqlite')
    ap.add_argument('--max-tier1', type=int, default=40,
                    help='re-cut tier-1 families larger than this using only stronger edges')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--audit', default='ἀγαπάω,βάλλω,λέγω,ἵστημι,ἀποκατάστασις')
    ap.add_argument('--top', type=int, default=15)
    args = ap.parse_args()

    con = sqlite3.connect(args.db, timeout=30.0)
    try:
        lexeme_of = dict(con.execute(
            "SELECT strong, lexeme FROM lexicon_words WHERE strong LIKE 'G%'"))
        con.execute('SELECT 1 FROM lexicon_word_relations LIMIT 1')
    except sqlite3.Error as e:
        sys.exit('Missing table (%s). Run build_word_relations.py first.' % e)
    known = set(lexeme_of)

    print('Deriving part of speech from token morphology…')
    token_pos = build_token_pos(con, known)

    print('Building families…')
    t1, t2, t3 = load_edges(con, known)
    rows, comps, warnings, dropped = build_families(t1, t2, t3, args.max_tier1)

    if not args.dry_run:
        with con:
            write_tables(con, rows, token_pos)
        print('Wrote %d family rows.' % len(rows))
    else:
        print('Dry run — nothing written.')

    report(con, rows, comps, warnings, dropped, lexeme_of, token_pos, args)
    audit(rows, lexeme_of, args.audit.split(','), token_pos)
    con.close()


if __name__ == '__main__':
    main()
