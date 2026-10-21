# The machine's pools

Every run queues for what it uses, on the far side, for the command's whole life: its remote
dir, each `ROPS_LOCK` name, the `ROPS_LOCK_ENV` values (one seat each), and last its seats in
the `cpu` pool. A run takes a ticket; it is admitted when its seats fit, decided under one short
mutex over the whole ledger, so two runs reading the same free count cannot both take it.
Tickets go in order: a later, smaller run goes first only while no earlier one fits, and never
past a head of the line that has waited 600s. A waiting run prints its ticket, its place, and
why (`needs 8, 3 of 14 seat(s) free (held by test(a1b2c3, n=11, pid=4242))`).

- `hs-rops lock status [--json]`: every pool, who holds, who waits and why, who skipped and why;
  each row names its run's process group (`pgid=`), and a holder the `hs-rops kill <pgid>` that
  frees its seats.
- A client told to stop (SIGTERM, SIGHUP, Ctrl-C — a supervisor stopping a background run)
  takes its far run down with it, from a reaper in a session of its own that the SIGKILL
  following the stop cannot reach; the reaper's lines land in the run's log (a fan-out
  `--hosts` reaps every host's run). A signal the client was started ignoring stays ignored:
  `nohup hs-rops run …` survives a closed terminal. A run that overruns `--timeout`, or whose
  connection drops, goes on detached (`fetch --wait`).
- `hs-rops lock cancel ID`: the waiting run ends rc 75, nothing run. A holding ticket is refused:
  its seats are its run's, freed when the run ends (`hs-rops kill`).
- `hs-rops lock resize ID N`: fewer seats keep the ticket's place; more go to the back.
- `--skip-queue "why"` (on `run` and every alias): start now, outside the `cpu` queue, with the
  reason on record in `status` — a narrow run (a few test files) that will not load the machine.
  It skips the `cpu` queue only: the dir, `--lock` and env locks still hold.

The queue is `rops/ropslib/pool_agent.py`, sent with every command and run by the far side's
`python3` (stdlib only). A host without it refuses every
run (rc 2) rather than running it unqueued.

A run's seats are `--cores N|all`; else, for an alias, its command's width; for a plain `run`,
`ROPS_CORES`, else its command's width; a `ci run` job, its width. The width is the
`-n`/`--numprocesses` of the simple command that runs pytest (`0` is one seat), `all` when it is
`auto` or none is given — another program's `-n` is no width — so a suite sent with no flag
never lands beside another unqueued. A command with no pytest made only of light one-process
programs (`find … | head`, `git log`, `python3 -c …`) takes one seat; any other (`make`, a runner
script) takes `all`. Either one naming no width that has to queue says, once while it waits, how
to size it: `--cores 1`, or `--skip-queue "why"`. A `bench` timed run holds the whole pool whatever
its command names; a lane rebuild holds none. `ROPS_CPU_CAP` (default 70% of
`nproc`) is the pool's size — set it where `nproc` is not what runs may use. With
`ROPS_CPU_BRAKE=on` (default) free seats are also capped by what the 1-minute load leaves: runs
outside the pool use cores too. The brake delays a run, never denies it: past the 600s
backfill window only the pool's seats decide, or load nobody queues for would starve it.

Keys: `ROPS_CORES`, `ROPS_CPU_CAP`, `ROPS_CPU_BRAKE`, `ROPS_LOCK`, `ROPS_LOCK_ENV`, `ROPS_LOCK_WAIT` (`references/config.md` § Execution).
