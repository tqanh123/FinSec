"""hs_run_facts — the `_apply_facts` engine seam's own logic.

A flat package under `harness/scripts/`, one call site
(`hs_run._apply_facts`, immediately before `_apply_escalation` in
`main()`). Kept out of `hs_run.py` itself, which is APPEND-only shared
ground: this package is where the seam's real work lives, the same split
`hs_run_cook.py`/`hs_run_review.py`/... already use for a domain's own
logic.

ALLOWED IMPORTS: the stdlib, plus harness-internal flat modules already on
hs-run's own import path (`harness_paths`, `yaml_io`,
`codegraph_snapshot_reader`) — never the `codegraph` package itself. Two
gates already walk the import graph from every `hs_run*.py` file through
whatever it imports (`test_no_hs_run_or_hs_family_module_imports_codegraph`,
`test_the_hs_run_family_import_closure_never_touches_codegraph` in
`test_codegraph_snapshot_reader.py`) — this package sits on that closure the
moment `hs_run.py` imports it, so the same bar applies here even though its
own directory name does not match either gate's `hs_run*.py` glob.
"""
