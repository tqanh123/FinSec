"""hs-code's `test` domain.

Port of ob-tools' `testkit` (pinned commit `60075cb`). Which verbs exist, and
the module + entry each one runs, is `harness/data/hs-code-registry.d/test.yaml`
alone; this docstring does not repeat it. A module here that the registry
does not name is a shared piece the verbs import, never copied between
them. Every verb raises
`hs_family.UsageError` directly at its own CLI door (its `run`) for a
request that cannot run honestly — matching `codegraph/check/`'s and
`codegraph/change/`'s domains, which carry no separate `errors.py` either;
a domain error class is only worth the extra module when more than one
engine function needs to raise it before the door converts it.

OUT OF THIS PACKAGE: running a suite on another machine. `durations` reads
whatever junit XML it is handed (here or copied back from elsewhere) — it
carries no `--rops`/`--run` flag of its own, and no verb in this package
invokes `hs-rops`. A verb here that needs a pytest run executes it locally,
synchronously, against `root`; measuring a run on a different machine is
`hs-rops`'s own job, reached separately.
"""
from __future__ import annotations
