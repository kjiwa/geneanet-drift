# geneanet-drift

Reports a Geneanet tree's recent edits against a Gramps Web tree. Useful when a
relative keeps editing their own copy of a tree you derived yours from, and you
want to fold the changes back in by hand.

## What it does and does not do

- It never writes to Gramps. It reads through the Gramps Web API with GET
  requests only, using a token it mints from `GRAMPS_URL`, `GRAMPS_USER` and
  `GRAMPS_PASSWORD`. Give it a Gramps user with no write permissions that can
  still view private records; it refuses a user that cannot, because the
  snapshot would silently omit those people.
- It never contacts Geneanet. Geneanet's terms prohibit automatic access, so you
  copy the change-log page by hand; the wizard prints each step and echoes what
  it parsed before trusting it.
- It produces a review sheet: per cluster of related edits, with Gramps-ready
  Citation and Note blocks. You apply them in Gramps Desktop or Web.

Links between his people and yours are stored in Gramps itself: a Citation
under the Source `Geneanet - <tree> family tree` whose `page` is his display name
for the person. Only negative decisions, deferred items and the last-sync
timestamp live in the private data directory.

## Quick start

```sh
pip install .
mkdir -p ~/geneanet-drift-data && cp config.example.toml ~/geneanet-drift-data/config.toml
export GENEANET_DRIFT_DATA=~/geneanet-drift-data
export GRAMPS_USER=... GRAMPS_PASSWORD=...
geneanet-drift          # wizard: prints every step
geneanet-drift --done   # after applying the review
```

Options: `--since YYYY-MM-DD` reviews from that date instead of after the last sync
(the first run asks how far back to look), and `--feed PATH` reads the change log
from an existing file.

Keep the data directory in its own private repository. The tool refuses to run
if the data or cache directory is inside its own git work tree.

## Keeping personal data out of this repository

gitleaks in CI catches credentials, not names. Personal data stays out by
structure: test fixtures use invented names only, the data and cache
directories are outside the repository, and `.gitignore` lists the files the
tool writes.
