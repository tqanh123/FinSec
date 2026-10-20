"""The one name this CLI answers to, imported everywhere a message needs it instead
of spelling it a second time.

Before the move into `harness/scripts/rops/`, the router shipped as a bare
`rops` shim on `PATH`; that shim is gone; the launcher `harness/bin/` generates is
`hs-rops`. A hint quoting the old name sends a reader to `command not found`.
"""
PROG = "hs-rops"
