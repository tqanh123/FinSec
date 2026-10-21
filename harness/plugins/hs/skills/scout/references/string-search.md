# Where a string is written -- ask `graph grep`, not a bare search

For a question about a STRING rather than an import: an env name, a config key, a
message, a name in docs or docstrings. A bare text search counts every occurrence the
same; `graph grep` types each match by where it sits, so a docstring mention never reads
as a use.

It needs the code map the import graph needs (`import-graph.md` § the `init status`
check); without one, search and say so in the report.

```bash
"${HARNESS_BIN_ROOT:-.}"/harness/bin/hs-code --repo . graph grep '<regex>' [--kind K,K] [--top N] [-i]
```

- Every git-tracked file, one row per match: `code` / `docstring` / `comment` / `string`
  in `.py` (with `enclosing`, the def or class around it), `data` in YAML/TOML/JSON (with
  `context`, the key path), `doc` in Markdown (heading path, `fenced`), else `text`.
- `data.by_kind` counts every match; `rows` stop at `--top`; `--kind code,string` keeps
  only those kinds.
- Files `codemap.yaml` declares under `copies:` (`generated:` views, `backup:` folders;
  gitignore-style patterns) are skipped and counted in `data.skipped_copies`, with a
  warning; `--include-copies` reads them, each row's `copy` naming the kind. A hit in a
  copy is fixed by regenerating or deleting it, never by editing.
- A whole-word search for a renamed name is `graph grep '\bname\b'` -- there is no
  separate prose-references verb.
- Count `code` rows for uses. A `docstring`/`comment`/`doc` row is a mention: never report
  it as the place something is computed or called.
