# MSB

python3 import/import_berean_tsv.py import/sources/msb.tsv --code MSB --name "Majority Standard Bible" --db bible.sqlite --language "English"
python3 import/import_copyright.py --code MSB --info-file import/copyright/MSB.txt
python3 import/import_berean_tsv.py import/sources/bsb_ot.tsv --code BSBOT --name "x" --db bible.sqlite --language "English"


# BSB

python3 import/import_berean_tsv.py import/sources/bsb_tables.tsv --code BSB --name "Berean Standard Bible" --db bible.sqlite --language "English"
python3 import/import_copyright.py --code BSB --info-file import/copyright/BSB.txt

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

python3 import/import_swete_parsed.py import/sources/swete_parsed.tsv import/sources/LXX-Swete-1930-master/01-Swete_word_with_punctuations.csv import/sources/LXX-Swete-1930-master/03-Swete_SBL_transliterations.csv --code SLXX --name "Swete Greek Septuagint" --db bible.sqlite --language "Greek"
python3 import/import_copyright.py --code SLXX --info-file import/copyright/Swete.txt

# HFB
python3 import/import_usfm.py import/sources/brenton_modernizer/output_usfm --code BLXXM --name "Brenton Septuagint Modernized" --db bible.sqlite --language "English"
python3 import/merge_translations.py --db bible.sqlite --ot BLXXM --nt MSB --code HFB --name "Historic Faith Bible"
python3 import/import_copyright.py --code HFB --info-file import/copyright/HFB.txt
python3 import/remove_translation.py --code BLXXM --db bible.sqlite
python3 import/edit_bible.py --db bible.sqlite --json import/sources/edits.json --load

# Vulgate
python3 import/import_vulgate_dra.py import/sources/lat-clementine.usfx.xml --code VUL --name "Clementine Vulgate" --language Latin --db bible.sqlite
python3 import/import_copyright.py --code VUL --info-file import/copyright/Vulgate.txt
# This will be the new parsed Vulgate, but we need to build a pipeline for it.
#python3 import/import_vulgate_proiel.py import/sources/Vulgate/la_proiel-ud-train.conllu import/sources/Vulgate/la_proiel-ud-dev.conllu import/sources/Vulgate/la_proiel-ud-test.conllu \
#        --code VUL --name "Vulgate NT" --db bible.sqlite --replace
#python3 import/import_copyright.py --code VUL --info-file import/copyright/PROIEL.txt
python3 import/import_vulgate_dra.py import/sources/eng-dra.zefania.xml --code DRA --name "Douay-Rheims" --language English --db bible.sqlite
python3 import/import_copyright.py --code DRA --info-file import/copyright/Douay.txt

# Peshitta
# Not including original language text because of a current lack of a good lexicon.
#python3 import/import_peshitta_tf.py --corpus syrnt --code PESH --name "Peshitta (Syriac NT)" --replace --db bible.sqlite
#python3 import/import_copyright.py --code PESH --info-file import/copyright/Peshitta.txt
    # Dependency: pip install pysword --break-system-packages
python3 import/import_spe_english.py import/sources/Murdock --code Murdock --name "Murdock English Peshitta" --db bible.sqlite
python3 import/import_copyright.py --code Murdock --info-file import/copyright/Murdock.txt

# Hebrew
    # Masoretic
python3 import/import_oshb.py import/sources/wlc --code WLC --name "Westminster Leningrad Codex"
python3 import/import_copyright.py --code WLC --info-file import/copyright/WLC.txt
    # DSS
python3 import/import_dss_biblical.py --code DSS --name "Dead Sea Scrolls" --db bible.sqlite
python3 import/import_copyright.py --code DSS --info-file import/copyright/DSS.txt

# Samaritan
python3 import/import_samaritan_pentateuch.py --code Samaritan --name "Samaritan Pentateuch" --db bible.sqlite
python3 import/import_copyright.py --code Samaritan --info-file import/copyright/Samaritan.txt
python3 import/import_spe_english.py import/sources/SPE --code SPE --name "Samaritan Pentateuch English" --db bible.sqlite
python3 import/import_copyright.py --code SPE --info-file import/copyright/SPE.txt

# Search and word index

python3 import/build_search_index.py
python3 import/build_word_index.py --db bible.sqlite

# Lexicons

python3 import/import_abbott_smith.py import/sources/abbott-smith.tei.xml bible.sqlite
python3 import/import_lexicon_copyright.py import/copyright/Abbott-Smith.txt --code AS --db bible.sqlite

python3 import/import_strongs_bdb.py import/sources/HebrewLexicon-master bible.sqlite
python3 import/import_lexicon_copyright.py import/copyright/Strongs.txt --code HSTR --db bible.sqlite
python3 import/import_lexicon_copyright.py import/copyright/BDB.txt --code BDB --db bible.sqlite

python3 import/import_latin_lexicon.py import/sources/lat.ls.perseus-eng2.xml --db bible.sqlite
python3 import/import_lexicon_copyright.py import/copyright/Lewis-Short.txt --code LS --db bible.sqlite

# Versifier
python3 import/versifier/build_versification_index.py bible.sqlite import/versifier/mt_lxx_diffs.tsv