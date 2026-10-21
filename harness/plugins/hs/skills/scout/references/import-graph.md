# Import questions of several hops — ask the graph

For a question that follows imports more than one hop. One hop stays a search
(`internal-scouting.md` § Import questions).

Check the repo has a code map first: `hs-code --repo . init status` (`data.adoption.state`).
Anything but `adopted` means there is no `codemap.yaml`, and creating one is the user's call,
not scout's: answer from search (`internal-scouting.md`) and tell the user `/hs:codemap` sets
the map up. An adopted repo whose `graph` row is not ready only lacks a snapshot, which the
first command below takes (it writes to the data home, not the repo).

Then query it (`<module>` is a dotted module name as the graph
spells it, for example `pkg.c` for `pkg/c.py` -- `file` prints it -- `<N>` the number of
hops, `<path>` a repo-relative file):

```bash
"${HARNESS_BIN_ROOT:-.}"/harness/bin/hs-code --repo . init status
"${HARNESS_BIN_ROOT:-.}"/harness/bin/hs-code --repo . --apply graph snapshot
"${HARNESS_BIN_ROOT:-.}"/harness/bin/hs-code --repo . graph importers <module> --depth <N>
"${HARNESS_BIN_ROOT:-.}"/harness/bin/hs-code --repo . graph file <path>
```

Each call answers with one JSON envelope on stdout; the verb's own payload is its `data`
(add `--text` for the human table instead).

- `importers` walks the reverse edges. Each of `data.rows` carries `module`, `file`, `depth`
  (1 = imports `<module>` itself) and `via` (the modules that row goes through to reach
  `<module>`, ending at `<module>`).
  "Directly or through one module that imports it directly" is `depth <= 2` where the
  `depth` 1 rows are the middle step. Do not read every row as a direct importer.
- `file` lists one file's imports as `{module, symbol}` pairs, with `module` and `symbol`
  in separate fields, split into `imports_module_level` and `imports_function_local`.
  Count both lists: an import inside a function is still an import. Follow a chain forward
  one module at a time: find the module's own file (Glob `**/<name>.py`, or `<pkg>/<name>.py`
  for a dotted name), then call `file` with that path INCLUDING its directory. `file`
  matches whole path segments (`store.py` names `x/store.py`, never `findings_store.py`);
  a tail that names several files exits 2 and lists every candidate -- pick one and ask
  again with more of its path. Check that the reply's `file` field is the file you asked for.
- The snapshot must be of the current HEAD. A query against an older snapshot exits 2 and
  names the stale commit; re-run the `snapshot` line, then ask again.
- The project needs a `codemap.yaml`. Without one the command exits 2; fall back to
  searching and say so in the report.
