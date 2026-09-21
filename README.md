# geneanet-drift

Reports a Geneanet tree's recent edits against a Gramps Web tree. Useful when a
relative keeps editing their own copy of a tree you derived yours from, and you
want to fold the changes back in by hand.

## What it does and does not do

- It never writes to Gramps. It reads through the Gramps Web API with GET
  requests only. Give it a Gramps user with no write permissions that can still
  view private records (`ViewPrivate`); it refuses a user that cannot, because
  the snapshot would silently omit those people.
- It never contacts Geneanet. Geneanet's terms prohibit automatic access, so you
  copy the change-log page by hand; the wizard prints each step and echoes what
  it parsed before trusting it.
- It produces a review sheet: per cluster of related edits, with Gramps-ready
  Citation and Note blocks. You apply them in Gramps Desktop or Web.

Links between his people and yours are stored in Gramps itself: a Citation
under the Source `Geneanet - <tree> family tree` whose `page` is his display name
for the person. Only negative decisions, deferred items and the last-sync
timestamp live in the private data directory.

## Install

`uv tool install .` or `pip install .`; everything below is identical either way.

## Configure

The tool remembers everything it can and prompts only for the one thing it
deliberately refuses to remember: the Gramps password.

The data directory holds `config.toml`, `.env` and the tool's state. Point
`--data-dir` or `GENEANET_DRIFT_DATA` at it (default
`$XDG_DATA_HOME/geneanet-drift`), then copy `config.example.toml` there as
`config.toml`.

`tree` is the slug in your relative's Geneanet URL with the `_w` suffix removed:
`https://gw.geneanet.org/example_w` is `tree = "example"`.

| setting | environment | `.env` | `config.toml` | prompt |
|---|---|---|---|---|
| data directory | `GENEANET_DRIFT_DATA` | no | no | no |
| `tree` | no | no | `tree` | no |
| Gramps URL | `GRAMPS_URL` | yes | `gramps_url` | no |
| Gramps user | `GRAMPS_USER` | yes | `gramps_user` | no |
| Gramps password | `GRAMPS_PASSWORD` | yes | never | yes |

Within a row, the process environment beats `<data dir>/.env`, which beats
`config.toml`. With no terminal and no password set, the tool exits with an
error instead of prompting.

```sh
echo 'GRAMPS_PASSWORD=...' > <data dir>/.env && chmod 600 <data dir>/.env
```

`.env` cannot set `GENEANET_DRIFT_DATA`, because it is found inside the data
directory. Its format is `KEY=VALUE` lines, `#` comments, an optional `export `
prefix and optional surrounding quotes; there is no interpolation, escape
sequences or multi-line values.

## The change log

The one step the tool cannot do for you. The page is the tree's "Data entry
history" (the `/history/editing` path under the tree URL from above). The
interface must be in English, because the parser reads English month names and
rejects the paste otherwise. Select the whole page and paste it into a
plain-text file. The wizard walks through these steps on every run.

## Run

```sh
geneanet-drift          # wizard: prints every step
geneanet-drift --done   # after applying the review
```

Options: `--since YYYY-MM-DD` reviews from that date instead of after the last sync
(the first run asks how far back to look), and `--feed PATH` reads the change log
from an existing file.

Keep the data directory in its own private repository, and list `.env` in that
repository's `.gitignore`. The tool refuses to run if the data or cache
directory is inside its own git work tree.

## Keeping personal data out of this repository

gitleaks in CI catches credentials, not names. Personal data stays out by
structure: test fixtures use invented names only, the data and cache
directories are outside the repository, and `.gitignore` lists the files the
tool writes.

## Contributing

`uv run --extra dev pytest` and `uv run --extra dev ruff check .`.
