"""secret_store — the engine under the `hs-secrets` CLI: where values live, and
where the ledgers that say what happened live.

Two stores with two different laws, and the difference is the point:

* VALUES go only into the active backend — the OS keychain, the encrypted file
  (`backends.EncryptedFileBackend`), or in-process memory for tests. A value
  never reaches an argv, a ledger line, or a log.
* METADATA (what was set, rotated, deleted; who may read it; who did read it)
  goes into append-only JSONL ledgers (`ledgers.py`) — the harness store law:
  no read-modify-write; the present is always a fold of the lines.

`router.HsSecretsCli` is the family CLI (hs-envelope/1); verbs live in
`verbs/`, one module per command, declared in
`harness/data/hs-secrets-registry.d/`.
"""
