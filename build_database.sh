# MSB

python3 import/import_berean_tsv.py import/sources/msb.tsv --code MSB --name "Majority Standard Bible" --db bible.sqlite --language "English"
python3 import/import_copyright.py --code MSB --info-file import/copyright/MSB.txt

# BSB

python3 import/import_berean_tsv.py import/sources/bsb_nt.tsv --code BSB --name "Berean Standard Bible" --db bible.sqlite --language "English"
python3 import/import_copyright.py --code BSB --info-file import/copyright/BSB.txt
python3 import/import_berean_tsv.py import/sources/bsb_ot.tsv --code BSBOT --name X --db bible.sqlite --language "English"
python3 import/merge_into.py --target BSB --ot BSBOT

# KJV

python3 import/import_osis_json.py import/sources/KJV-osis.json --code KJV --name "King James Version"
python3 import/import_copyright.py --code KJV --info-file import/copyright/KJV.txt

# WEB Updated

python3 import/import_usfm.py import/sources/engwebu_usfm --code WEB --name "World English Bible Updated" --db bible.sqlite --language "English"
python3 import/import_copyright.py --code WEB --info-file import/copyright/WEBU.txt

# Brenton

python3 import/import_usfm.py import/sources/eng-Brenton_usfm --code BLXX --name "Brenton Septuagint" --db bible.sqlite --language "English"
python3 import/import_copyright.py --code BLXX --info-file import/copyright/Brenton.txt

# Greek

python3 import/import_robinson_csv.py import/sources/with-parsing import/sources/with-variants --code BYZ --name "Robinson-Pierpont Byzantine Text" --db bible.sqlite --language "Greek"
python3 import/import_copyright.py --code BYZ --info-file import/copyright/Robinson.txt

python3 import/import_nestle_csv.py import/sources/Nestle1904.csv --code Nestle --name "Nestle 1904" --db bible.sqlite --language "Greek"
python3 import/import_copyright.py --code Nestle --info-file import/copyright/Nestle1904.txt

python3 import/import_textus_receptus.py import/sources/parsed --code TR --name "Textus Receptus (Scrivener/Robinson)"
python3 import/import_copyright.py --code TR --info-file import/copyright/TR.txt

python3 import/import_swete_parsed.py import/sources/swete_parsed.tsv import/sources/LXX-Swete-1930-master/01-Swete_word_with_punctuations.csv import/sources/LXX-Swete-1930-master/03-Swete_SBL_transliterations.csv --code SLXX --name "Swete Greek Septuagint" --db bible.sqlite --language "Greek"
python3 import/import_copyright.py --code SLXX --info-file import/copyright/Swete.txt

# HFB
python3 import/import_usfm.py import/sources/eng-BrentonM_usfm --code BLXXM --name "Brenton Septuagint Modernized" --db bible.sqlite --language "English"
python3 import/merge_translations.py --db bible.sqlite --ot BLXXM --nt MSB --code HFB --name "Historic Faith Bible"
python3 import/import_copyright.py --code HFB --info-file import/copyright/HFB.txt
python3 import/edit_bible.py --db bible.sqlite --json import/sources/edits.json --load

python3 import/merge_into.py --target MSB --ot BSBOT

# Vulgate
python3 import/import_vulgate_dra.py import/sources/lat-clementine.usfx.xml --code VUL --name "Clementine Vulgate" --language Latin --db bible.sqlite
python3 import/import_copyright.py --code VUL --info-file import/copyright/Vulgate.txt

python3 import/import_vulgate_dra.py import/sources/eng-dra.zefania.xml --code DRA --name "Douay-Rheims" --language English --db bible.sqlite
python3 import/import_copyright.py --code DRA --info-file import/copyright/Douay.txt

# Peshitta
    # Dependency: pip install pysword --break-system-packages
python3 import/import_spe_english.py import/sources/Murdock --code Murdock --name "Murdock English Peshitta" --db bible.sqlite
python3 import/import_copyright.py --code Murdock --info-file import/copyright/Murdock.txt

# Hebrew
    # Masoretic
python3 import/import_oshb.py import/sources/wlc --code WLC --name "Westminster Leningrad Codex"
python3 import/import_copyright.py --code WLC --info-file import/copyright/WLC.txt

# Samaritan
python3 import/import_samaritan_pentateuch.py --code Samaritan --name "Samaritan Pentateuch" --db bible.sqlite
python3 import/import_copyright.py --code Samaritan --info-file import/copyright/Samaritan.txt
python3 import/import_spe_english.py import/sources/SPE --code SPE --name "Samaritan Pentateuch English" --db bible.sqlite
python3 import/import_copyright.py --code SPE --info-file import/copyright/SPE.txt

# Lexicons

    # This needs to be done later as well, but this is putting it up above the other results
python3 import/import_shebanq_gloss.py import/sources/bhsa_tf bible.sqlite

python3 import/import_strongs_bdb.py import/sources/HebrewLexicon-master bible.sqlite
python3 import/import_lexicon_copyright.py import/copyright/Strongs.txt --code HSTR --db bible.sqlite
python3 import/import_lexicon_copyright.py import/copyright/BDB.txt --code BDB --db bible.sqlite

python3 import/import_shebanq_gloss.py import/sources/bhsa_tf bible.sqlite
python3 import/import_lexicon_copyright.py import/copyright/SHEBANQ.txt --code SHEBANQ --db bible.sqlite

python3 import/import_dodson.py import/sources/dodson.xml bible.sqlite
python3 import/import_lexicon_copyright.py import/copyright/Dodson.txt --code DODSON --db bible.sqlite

python3 import/import_abbott_smith.py import/sources/abbott-smith.tei.xml bible.sqlite
python3 import/import_lexicon_copyright.py import/copyright/Abbott-Smith.txt --code AS --db bible.sqlite

python3 import/import_latin_lexicon.py import/sources/lat.ls.perseus-eng2.xml --db bible.sqlite
python3 import/import_lexicon_copyright.py import/copyright/Lewis-Short.txt --code LS --db bible.sqlite

python3 import/import_strongs_greek.py import/sources/strongsgreek.xml bible.sqlite
python3 import/import_lexicon_copyright.py import/copyright/Strongs-Greek.txt --code GSTR --db bible.sqlite

# DSS (needs to come after lexicons)
python3 import/import_dss_biblical.py --code DSS --name "Dead Sea Scrolls" --db bible.sqlite
python3 import/import_copyright.py --code DSS --info-file import/copyright/DSS.txt


python3 import/remove_translation.py --code BSBOT --db bible.sqlite
python3 import/remove_translation.py --code BLXXM --db bible.sqlite
python3 import/remove_book.py --target HFB --book "Greek Daniel"
python3 import/remove_book.py --target HFB --book "3 Maccabees"
python3 import/remove_book.py --target HFB --book "4 Maccabees"

# Search and word index

python3 import/build_search_index.py
python3 import/build_word_index_nltk.py --db bible.sqlite

python3 import/build_word_relations.py --db bible.sqlite \
  --strongs import/sources/strongsgreek.xml --abbott import/sources/abbott-smith.tei.xml \
  --kaikki import/sources/kaikki.org-dictionary-AncientGreek.jsonl --report-tsv unresolved.tsv

python3 import/build_word_families.py --db bible.sqlite

# Versifier
    # Needs the files from versifier project
python3 import/versification/build_versification_index.py bible.sqlite import/versification/versification_links.tsv --manual import/versification/manual_vulg.tsv import/versification/manual_links.tsv
python3 import/build_xrefs.py import/sources/cross_references.csv