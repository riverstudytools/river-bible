# River Bible App
The River Bible app is an open source app intended to make the highest-quality free Bible resources available online and offline. 

A second goal is to provide access to translations that are more aligned with Anabaptist and Kingdom Christian standards of exegesis, without forcing the use of such translations on users.

## Features

- Enjoy a readable, simple Bible-reading experience.
- Tap or click on a Bible word to find the original language word and its meanings.
- Easily copy Bible text and paste it outside of River.
- Have as many passages as you want open at one time.
- Have up to two passages open side-by-side.
- Compare up to four texts for the same passage. For texts with different chapter/verse numbering systems, they can be aligned by their content for easier comparison.
- Find all the places where an original language word appears in the Bible.

## Bibles

The River Bible app contains original texts and translations of the main text families:

Old Testament
- Masoretic Text
- Septuagint (LXX)
(coming soon)
- Dead Sea Scrolls
- Samaritan Pentateuch

New Testament
- Byzantine (Majority) Text
- Critical Text
- Textus Receptus
(coming soon)
- Latin Vulgate
- Syriac Peshitta

Also included are the Old Testament Apocrypha/Deuterocanon.

## Lexicons

Hebrew
- Strongs
- Brown, Driver, and Briggs

Greek
- Abbott-Smith

# Build

## Build Dependencies

- **Python 3.8+**
- **pip**
pip install nltk

pip install text-fabric[all] --break-system-packages

pip install pysword --break-system-packages

# Attribution

Copyright information for each of the included texts or lexicons can be found in `import/copyright`. Other attribution can be found below.

The code of the River Bible app, as well as tools for importing and linking Bible data, was built using Claude and Gemini.

Aligning verse numbers is done using STEPBible's TVTMS (CC BY 4.0)

Finding which glosses should be lumped together is done using NLTK's WordNetLemmatizer.

The Swete LXX was parsed using Eliran Wong, J Tauber, Maurice Robinson, etc.