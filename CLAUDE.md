# feed-filter

Working rules (flow, delegation, verification) come from the `workflow` plugin; this file holds only what is specific to this project.

## What this is

An RSS feed filter: it pulls a curated set of sources and uses Laya — a local,
self-hosted decision model — to score each item for relevance and sensationalism, and
to tag whether it is reporting, analysis, opinion or promotion.

Laya runs natively on the host, not in Docker. The app reaches it over HTTP at
`FF_LAYA_BASE_URL`.

## Backlog

`tasks/` — local directory, git-ignored. One file per task, one task per PR.
Chosen over an MCP backlog tool for this project.

## Docs

`README.md`

## Language

Replies to the user in Spanish. Everything written to the repo — code, comments, docs,
commit messages — in English. User-facing UI copy in Spanish.

## Verify

`make check`
