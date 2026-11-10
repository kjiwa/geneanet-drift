# geneanet-drift

Folds the edits a relative makes in their Geneanet tree back into your Gramps
Web tree. A browser extension saves the Geneanet pages you open; this tool
compares the saved pages with Gramps, lets you accept, edit or reject each
difference in a local web page, and writes the accepted ones to Gramps.

## What it does and does not do

- The Python tool never contacts Geneanet. Geneanet's terms prohibit automatic
  access. The browser extension (below) acts only on a page you opened
  yourself: it never requests, navigates, clicks, scrolls or paginates. It
  saves the DOM your browser already rendered, the same as select-all and copy,
  and saves an image only when you click for it, like "Save image as". That is
  the author's reading, not legal advice; Geneanet may still treat any
  page-reading extension as a tool.
- Reading Gramps is GET-only. Give the read user no write permissions but
  `ViewPrivate`; it refuses a user that cannot view private records, because
  the snapshot would silently omit those people. Only `geneanet-drift apply` and
  `undo` write, as a separate Editor user that must hold `AddObject` and
  `EditObject` and nothing beyond the Editor role.
- Nothing is written without a dry-run preview, a typed confirmation and a
  Gramps XML backup taken first.

Links between his people and yours are stored in Gramps itself: a Citation
under the Source `Geneanet - <tree> family tree` whose `page` is his display name
for the person. The private data directory holds `decisions.json`,
`applied.json`, `backups/` and an optional hand-written `negatives.json` of
rejected pairings.

`decisions.json` holds the field decisions and three kinds of entry: `link:`
(a chosen Gramps match), `none:<his person number>` ("None of these is the same
person") and `place:` (a place text mapped to a Gramps place).

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
set. Edit forms are never saved. The data entry history page is saved as
`history-editing.html` beside the person pages.

## Configure

The tool remembers everything it can and prompts only for what it deliberately
refuses to remember: the Gramps passwords.

The data directory holds `config.toml`, `.env` and the tool's state. Point
`--data-dir` or `GENEANET_DRIFT_DATA` at it (default
`$XDG_DATA_HOME/geneanet-drift`, else `~/.local/share/geneanet-drift`), then copy `config.example.toml` there as
`config.toml`.

`tree` is the slug in your relative's Geneanet URL with the `_w` suffix removed:
`https://gw.geneanet.org/example_w` is `tree = "example"`. The extension's Tree
name takes the same value.

| setting | environment | `.env` | `config.toml` | prompt |
|---|---|---|---|---|
| data directory | `GENEANET_DRIFT_DATA` | no | no | no |
| `tree` | no | no | `tree` | no |
| Gramps URL | `GRAMPS_URL` | yes | `gramps_url` | no |
| Gramps user | `GRAMPS_USER` | yes | `gramps_user` | no |
| Gramps password | `GRAMPS_PASSWORD` | yes | never | yes |
| Gramps write user (Editor role, `apply` and `undo` only) | `GRAMPS_WRITE_USER` | yes | `gramps_write_user` | no |
| Gramps write password | `GRAMPS_WRITE_PASSWORD` | yes | never | yes |

Within a row, the process environment beats `<data dir>/.env`, which beats
`config.toml`. With no terminal and no password set, the tool exits with an
error instead of prompting. The write rows are read only by `apply` and `undo`.

```sh
echo 'GRAMPS_PASSWORD=...' > <data dir>/.env && chmod 600 <data dir>/.env
```

`.env` cannot set `GENEANET_DRIFT_DATA`, because it is found inside the data
directory. Its format is `KEY=VALUE` lines, `#` comments, an optional `export `
prefix and optional surrounding quotes; there is no interpolation, escape
sequences or multi-line values.

## Workflow

Run these in order. `DIR` is the capture folder, by default
`~/Downloads/geneanet-drift/<tree>/pages`.

1. Capture. `geneanet-drift` with no command is `geneanet-drift capture-status`;
   its options need the command spelled out, so `geneanet-drift --since
   2026-01-01` is an error (exit status 2). It reads
   the saved change log `DIR/history-editing.html` and lists every person in it
   whose captured page is missing or older than their newest change (file time
   against the change time), each with the address to open. No sync state is
   kept: capturing a page is what clears it from the list. Geneanet's times are
   compared as written against your local file times, so a change within a few
   hours of a capture may need a manual check. Open those pages with the
   extension on, rerun to check, and repeat until nothing is left. Options:
   `capture-status --pages DIR`, `--since YYYY-MM-DD` to list only changes from
   that date on, and `--feed FILE` to read a pasted copy of the change log (the page in
   English, select-all, paste into a plain-text file) instead of the saved
   page.
2. Snapshot. `geneanet-drift snapshot --out FILE` saves the Gramps tree (read
   user) to a JSON file outside this repository.
3. Review. `geneanet-drift review --pages DIR --snapshot FILE [--port N]` serves
   the review page on `127.0.0.1` (default: a free port) and prints its URL.
   Decisions are saved to `decisions.json`, so a review can span sittings. See
   Review page below.
4. Apply. `geneanet-drift apply --pages DIR` builds a plan from the accepted
   decisions, prints it (the dry run is always shown), and writes it only after
   you type the confirmation. Before any write it saves a Gramps XML backup to
   `<data dir>/backups/` through the read user and stops if that fails. Each
   write is logged to `<data dir>/applied.json` with its created and updated
   objects and transaction ids; a rerun skips logged steps and stops, without
   writing, if a logged object has since changed.
   `geneanet-drift undo --last N` reverts the newest N logged steps, newest
   first.
5. GEDCOM, optionally. `geneanet-drift gedcom --pages DIR --out FILE` exports
   the captured person pages (`DIR` holds `person-<n>.html`) as GEDCOM 5.5.1.
   Media `FILE` paths are relative to the capture folder of the tree
   (`media/person-<n>/...`).

Keep the data directory in its own private repository, and list `.env` in that
repository's `.gitignore`. The tool refuses to run if the data
directory is inside its own git work tree.

### Review page

People are grouped as Needs a match, New in his tree, Differs and Unchanged.
"Hide people with no changes left to decide" is on by default; a group opens
when something in it is left to decide.

- Needs a match: each person lists the Gramps candidates inline. "Use this
  person" chooses one ("Chosen. Undo" reverts it); "None of these is the same
  person" marks the person as new. A notice after each choice says how many people still need a
  match, and counts relatives whose match the choice settled. Open in Gramps and
  Open in Geneanet open the person in a new tab.
- Differs and New: accept, edit or reject each change. Edit opens a form for
  the value Gramps would hold. A change that depends on an unmatched relative
  waits until that relative has a match.
- Places: each place text in his tree maps to a Gramps place. Use a suggestion,
  type another Gramps place title, or Clear. A text with no choice becomes one new
  place, named exactly as in his tree, when applied.
- Photos show only when their originals were saved with the extension's Save
  originals button, next to `DIR` (`../media/person-<n>/`).
- "Accepted changes (N)" opens a read-only dialog listing what `apply` will
  write, what it will not and why, and the command to run. Nothing is written
  from the page.

## Keeping personal data out of this repository

gitleaks in CI catches credentials, not names. Personal data stays out by
structure: test fixtures use invented names only, the data directory is
outside the repository, and `.gitignore` lists the files the
tool writes.

## Contributing

`uv run --extra dev pytest` and `uv run --extra dev ruff check .`. For the
extension, `node --test "tests/extension/*.test.js"` and
`npx --yes addons-linter@10.14.0 extension`.
