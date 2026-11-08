# geneanet-drift

Reports a Geneanet tree's recent edits against a Gramps Web tree. Useful when a
relative keeps editing their own copy of a tree you derived yours from, and you
want to fold the changes back in by hand.

## What it does and does not do

- It never writes to Gramps. It reads through the Gramps Web API with GET
  requests only. Give it a Gramps user with no write permissions that can still
  view private records (`ViewPrivate`); it refuses a user that cannot, because
  the snapshot would silently omit those people.
- The Python tool never contacts Geneanet. Geneanet's terms prohibit automatic
  access, so you copy the change-log page by hand; the wizard prints each step
  and echoes what it parsed before trusting it. The optional browser extension
  (below) only saves pages you open yourself; it never requests, navigates or
  clicks anything.
- It produces a review sheet: per cluster of related edits, with Gramps-ready
  Citation and Note blocks. You apply them in Gramps Desktop or Web.

Links between his people and yours are stored in Gramps itself: a Citation
under the Source `Geneanet - <tree> family tree` whose `page` is his display name
for the person. Only negative decisions, deferred items and the last-sync
timestamp live in the private data directory.

## Install

`uv tool install .` or `pip install .`; everything below is identical either way.

## Browser extension

`extension/` saves the Geneanet tree pages you open (person pages, the list of
all individuals and the data entry history) to
`Downloads/geneanet-drift/<tree>/pages/`, so you can reconcile a whole tree
without exporting it. It works in Chrome and Firefox from one manifest and has
no build step.

- Chrome: open `chrome://extensions`, enable Developer mode, choose Load
  unpacked and select `extension/`.
- Firefox: open `about:debugging#/runtime/this-firefox` and choose Load
  Temporary Add-on, then select `extension/manifest.json`. The add-on is
  removed when Firefox restarts; the saved pages are not.

Open the extension's Options, enter the tree name, and on Firefox choose Grant
access. Then browse: each person page you open is saved, and the popup lists
the people you have not opened yet. Pages are only saved while a tree name is
set. Edit forms are never saved.

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

`geneanet-drift gedcom --pages DIR --out FILE` exports the captured person pages
(`DIR` holds `person-<n>.html`) as GEDCOM 5.5.1. Media `FILE` paths are relative to
the capture folder of the tree (`media/person-<n>/...`).

`geneanet-drift snapshot --out FILE` saves the Gramps tree (read user) to a JSON file.
`geneanet-drift review --pages DIR --snapshot FILE [--port N]` serves the review page
on `127.0.0.1` (default: a free port) and prints its URL. It compares the captured
pages with the snapshot; accept, edit or reject each change, and choose the matching
Gramps person where names are ambiguous. Decisions are saved to `decisions.json` in
the data directory, so a review can span sittings. Applying to Gramps is not wired
up yet. Photos show only when their originals were saved next to `DIR`
(`../media/person-<n>/`).

Keep the data directory in its own private repository, and list `.env` in that
repository's `.gitignore`. The tool refuses to run if the data or cache
directory is inside its own git work tree.

## Keeping personal data out of this repository

gitleaks in CI catches credentials, not names. Personal data stays out by
structure: test fixtures use invented names only, the data and cache
directories are outside the repository, and `.gitignore` lists the files the
tool writes.

## Contributing

`uv run --extra dev pytest` and `uv run --extra dev ruff check .`. For the
extension, `node --test "tests/extension/*.test.js"` and
`npx --yes addons-linter@10.14.0 extension`.
