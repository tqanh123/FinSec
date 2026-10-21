"""Seats in a machine's pools, run on the far side (`python3 -c <this file> ...`).

Every lock a run takes is a pool: `cpu` (seats = 70% of the cores unless `--cap`, a run
asks for N), and one per lane dir, lock name, or exclusive env (1 seat). A run takes a
ticket and is admitted when its N fit -- decided under ONE short mutex over the whole
ledger, so two runs reading the same free count cannot both take it. In ticket order: a
later run goes first only when no earlier one fits and the head of the line has waited
less than `--backfill-s` (then nobody overtakes it). `cpu` also brakes on the load: free
is the smaller of the seats left and the cores the 1-minute load leaves -- runs outside
the pool use cores too. The brake delays, never denies: a ticket that has waited
`--backfill-s` is judged by the pool's seats alone, or load nobody queues for would
starve it. A seat is held while the owner pid (with its start time) lives.

    acquire --pool P --n N --owner PID --label L --wait S   rc 0 admitted, 75 gave up/cancelled
    skip    --pool P --owner PID --label L --reason R       run outside the queue, on record
    status [--json] · cancel ID · resize ID N
Stdlib only, Linux `/proc` for start times.
"""
import argparse
import fcntl
import json
import os
import secrets
import sys
import time
from datetime import datetime

RC_BUSY = 75
CPU = "cpu"
RATIO = 0.7
SAY_EVERY_S = 30.0


def _stat(pid):
    """(state, start time) from /proc, ("", "") where there is none."""
    try:
        with open("/proc/%d/stat" % pid) as fh:
            f = fh.read().rsplit(")", 1)[1].split()
        return f[0], f[19]
    except (OSError, IndexError):
        return "", ""


def _start(pid):
    return _stat(pid)[1]


def _pgid(pid):
    """The owner's process group -- what `hs-rops kill` takes down -- or None."""
    try:
        with open("/proc/%d/stat" % int(pid)) as fh:
            return int(fh.read().rsplit(")", 1)[1].split()[2])
    except (OSError, IndexError, ValueError):
        return None


def _alive(e):
    """The owner still runs: the pid exists, is not a zombie (exited, not yet reaped),
    and is the same process (start time) -- not a later one given its pid."""
    pid = int(e["owner"])
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    state, start = _stat(pid)
    return state != "Z" and (not e.get("start") or start in ("", e["start"]))


class Ledger:
    def __init__(self, root):
        self.root = root
        self.dir = os.path.join(root, "entries")
        os.makedirs(self.dir, exist_ok=True)
        self._fd = None

    def __enter__(self):
        self._fd = os.open(os.path.join(self.root, "ledger.mutex"), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        os.close(self._fd)

    def entries(self):
        out = []
        for name in sorted(os.listdir(self.dir)):
            if not name.endswith(".json"):
                continue
            p = os.path.join(self.dir, name)
            try:
                with open(p) as fh:
                    e = json.load(fh)
            except (OSError, ValueError):
                continue
            if _alive(e):
                out.append(e)
            else:
                self.drop(e["id"])
        return out

    def put(self, e):
        p = os.path.join(self.dir, e["id"] + ".json")
        with open(p + ".tmp", "w") as fh:
            json.dump(e, fh, sort_keys=True)
        os.replace(p + ".tmp", p)

    def drop(self, eid):
        try:
            os.unlink(os.path.join(self.dir, eid + ".json"))
            return True
        except FileNotFoundError:
            return False


def _load(path):
    try:
        with open(path) as fh:
            return float(fh.read().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0


def _cap(a):
    if a.cap:
        return max(1, a.cap)
    return max(1, int(a.nproc * RATIO)) if a.pool == CPU else 1


def _who(es):
    return ", ".join("%s(%s, n=%d, pid=%s)" % (e["label"], e["id"], e["n"], e["owner"])
                     for e in es) or "nobody"


def _decide(a, me, es, now):
    """(admit, why) for `me` against the live entries of its pool."""
    same = [e for e in es if e["pool"] == me["pool"]]
    held = sum(e["n"] for e in same if e["state"] == "holding")
    free = me["cap"] - held
    why = "needs %d, %d of %d seat(s) free (held by %s)" % (
        me["n"], max(free, 0), me["cap"], _who([e for e in same if e["state"] == "holding"]))
    if a.brake and now - me["queued"] < a.backfill_s:
        load = _load(a.loadavg)
        idle = a.nproc - load
        if idle < free:
            free = int(idle)
            why = "needs %d, the load %.1f leaves %d of %d core(s) (runs outside the pool count)" % (
                me["n"], load, max(free, 0), a.nproc)
    line = sorted((e for e in same if e["state"] == "waiting"), key=lambda e: e["order"])
    ahead = line[:[e["id"] for e in line].index(me["id"])]
    if me["n"] > free:
        return False, why
    if any(e["n"] <= free for e in ahead):
        return False, "an earlier ticket fits first (%s)" % _who(ahead)
    if ahead and now - ahead[0]["queued"] >= a.backfill_s:
        return False, "the head of the line (%s) has waited past %gs; nobody overtakes it" % (
            _who(ahead[:1]), a.backfill_s)
    return True, ""


def _entry(a, state):
    pid = a.owner
    return {"id": secrets.token_hex(3), "pool": a.pool, "owner": pid, "start": _start(pid),
            "label": a.label, "state": state, "since": datetime.now().isoformat(timespec="seconds"),
            "queued": time.time(), "order": time.time()}


def acquire(a):
    led = Ledger(a.dir)
    me = _entry(a, "waiting")
    me["cap"] = _cap(a)
    me["n"] = min(max(a.n, 1), me["cap"])
    if a.n > me["cap"]:
        print("[rops] %s: asks %d, the pool holds %d -- waits to run alone" % (a.pool, a.n, me["cap"]))
    deadline = time.monotonic() + a.wait
    said, said_at = None, 0.0
    with led:
        led.put(me)
    while True:
        with led:
            es = led.entries()
            cur = next((e for e in es if e["id"] == me["id"]), None)
            if cur is None:
                print("[rops] %s: ticket %s cancelled -- nothing run" % (a.pool, me["id"]))
                return RC_BUSY
            ok, why = _decide(a, cur, es, time.time())
            if ok:
                cur.update(state="holding", admitted=datetime.now().isoformat(timespec="seconds"))
                cur.pop("why", None)
                led.put(cur)
                if said is not None:
                    print("[rops] %s: ticket %s admitted (%d seat(s))" % (a.pool, cur["id"], cur["n"]))
                return 0
            if cur.get("why") != why:
                cur["why"] = why
                led.put(cur)
            line = sorted((e for e in es if e["pool"] == a.pool and e["state"] == "waiting"),
                          key=lambda e: e["order"])
            pos = [e["id"] for e in line].index(cur["id"]) + 1
        view = (pos, why)
        if said is None and getattr(a, "hint", ""):
            print("[rops] %s" % a.hint)
        if view != said or time.monotonic() - said_at >= SAY_EVERY_S:
            print("[rops] queued: %s ticket %s #%d of %d -- %s; waits up to %ds. cancel: "
                  "hs-rops lock cancel %s · resize: hs-rops lock resize %s N"
                  % (a.pool, cur["id"], pos, len(line), why, a.wait, cur["id"], cur["id"]))
            sys.stdout.flush()
            said, said_at = view, time.monotonic()
        if time.monotonic() >= deadline:
            with led:
                led.drop(me["id"])
            print("[rops] %s: gave up after %ds waiting (ticket %s) -- nothing run"
                  % (a.pool, a.wait, me["id"]))
            return RC_BUSY
        time.sleep(a.poll)


def skip(a):
    if not (a.reason or "").strip():
        print("[rops] skipping the %s queue needs a reason (--skip-queue \"why\")" % a.pool,
              file=sys.stderr)
        return 2
    led = Ledger(a.dir)
    e = _entry(a, "skipped")
    e.update(reason=a.reason.strip(), n=0, cap=_cap(a))
    with led:
        led.put(e)
    print("[rops] %s: runs outside the queue -- %s" % (a.pool, e["reason"]))
    return 0


def status(a):
    led = Ledger(a.dir)
    with led:
        es = led.entries()
    now = time.time()
    pools = {}
    for e in es:
        p = pools.setdefault(e["pool"], {"cap": e.get("cap", 1), "held": 0})
        p["held"] += e["n"] if e["state"] == "holding" else 0
    rank = {"holding": 0, "skipped": 1, "waiting": 2}
    es.sort(key=lambda e: (e["pool"], rank.get(e["state"], 3), e["order"]))
    for e in es:
        e["waited_s"] = int(now - e["queued"])
        e["pgid"] = _pgid(e["owner"])
    if a.json:
        print(json.dumps({"pools": pools, "entries": es}, sort_keys=True))
        return 0
    if not es:
        print("no run holds or waits for a pool")
    for pool, p in sorted(pools.items()):
        print("%s: %d of %d seat(s) held" % (pool, p["held"], p["cap"]))
        for e in (e for e in es if e["pool"] == pool):
            tail = {"holding": "since %s" % e.get("admitted", e["since"]),
                    "skipped": "outside the queue since %s: %s" % (e["since"], e.get("reason", "")),
                    "waiting": "waiting %ds: %s" % (e["waited_s"], e.get("why", ""))}[e["state"]]
            print("  %-8s %s %-24s n=%d pid=%s pgid=%s %s" % (e["state"], e["id"], e["label"],
                                                              e["n"], e["owner"], e["pgid"], tail))
            if e["state"] != "waiting" and e["pgid"]:
                # a holder's seats are its run's: they free when the run's group goes
                print("           stop it: hs-rops kill %d" % e["pgid"])
    return 0


def cancel(a):
    """A waiting ticket only: a holder's seats are its run's, freed when the run ends."""
    with Ledger(a.dir) as led:
        e = next((e for e in led.entries() if e["id"] == a.id), None)
        if e is not None and e["state"] == "waiting" and led.drop(a.id):
            print("cancelled %s" % a.id)
            return 0
    if e is None:
        print("no ticket %s" % a.id, file=sys.stderr)
    else:
        print("ticket %s is %s, not waiting: its run (pid %s) owns it -- stop the run "
              "(hs-rops kill), which frees its seats" % (a.id, e["state"], e["owner"]),
              file=sys.stderr)
    return 1


def resize(a):
    with Ledger(a.dir) as led:
        e = next((e for e in led.entries() if e["id"] == a.id), None)
        if e is None or e["state"] != "waiting":
            print("no waiting ticket %s" % a.id, file=sys.stderr)
            return 1
        n = min(max(a.n, 1), e["cap"])
        if n > e["n"]:
            e["order"] = time.time()
        e["n"] = n
        led.put(e)
    print("%s now asks %d" % (a.id, n))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pool")
    sub = ap.add_subparsers(dest="verb", required=True)

    def common(p, owner=True):
        p.add_argument("--dir", required=True)
        if owner:
            p.add_argument("--pool", required=True)
            p.add_argument("--owner", type=int, required=True)
            p.add_argument("--label", default="run")
            p.add_argument("--nproc", type=int, default=os.cpu_count() or 1)
            p.add_argument("--cap", type=int, default=0)
        return p

    p = common(sub.add_parser("acquire"))
    p.add_argument("--n", type=int, default=1)
    p.add_argument("--hint", default="", help="said once if the ticket has to queue")
    p.add_argument("--wait", type=int, default=1800)
    p.add_argument("--poll", type=float, default=2.0)
    p.add_argument("--backfill-s", type=float, default=600.0)
    p.add_argument("--loadavg", default="/proc/loadavg")
    p.add_argument("--no-brake", dest="brake", action="store_false")
    p = common(sub.add_parser("skip"))
    p.add_argument("--reason", default="")
    p = common(sub.add_parser("status"), owner=False)
    p.add_argument("--json", action="store_true")
    p = common(sub.add_parser("cancel"), owner=False)
    p.add_argument("id")
    p = common(sub.add_parser("resize"), owner=False)
    p.add_argument("id")
    p.add_argument("n", type=int)
    a = ap.parse_args(argv)
    if a.verb == "acquire" and a.pool != CPU:
        a.brake = False
    return {"acquire": acquire, "skip": skip, "status": status, "cancel": cancel,
            "resize": resize}[a.verb](a)


if __name__ == "__main__":
    sys.exit(main())
