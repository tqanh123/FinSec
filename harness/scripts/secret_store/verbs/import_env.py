"""import-env — pull a dotenv file's values into the store.

Every parsed pair is stored under its own name (backend value, one index
line per ref, source tagged) and the file is left byte-identical unless
--rewrite is passed: then the ORIGINAL is backed up beside it first, and
every imported pair's line becomes `KEY=${secret:KEY}` — the shape the
inject hook understands. Values are never printed: the report names the
refs it took in.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from secret_store import api, backends, dotenv, ledgers


def add_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("path", help="the .env file to import")
    p.add_argument("--rewrite", action="store_true",
                   help="after backing the file up, replace each imported value "
                        "with ${secret:KEY}")


def run(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if not path.is_file():
        print("!! no such file: %s" % path)
        return 2
    try:
        pairs = dotenv.parse(path.read_text(encoding="utf-8"))
        if not pairs:
            print("nothing to import — no KEY=VALUE lines in %s" % path)
            return 0
        home, backend, live = api.open_store(args.backend_choice)
        for ref, value in sorted(pairs.items()):
            backend.put(ref, value)
            ledgers.append(home / "index.jsonl",
                           {"op": "set", "ref": ref, "source": "import-env"})
        if args.rewrite:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            backup = path.with_name("%s.bak-%s" % (path.name, stamp))
            backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
            out_lines = []
            for raw in path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                key = line.partition("=")[0].strip() if "=" in line and not line.startswith("#") else None
                if key in pairs:
                    out_lines.append("%s=${secret:%s}" % (key, key))
                else:
                    out_lines.append(raw)
            path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
            print("imported %d ref(s); backup at %s; file rewritten to placeholders"
                  % (len(pairs), backup.name))
        else:
            print("imported %d ref(s): %s" % (len(pairs), ", ".join(sorted(pairs))))
    except (api.Refusal, backends.BackendError) as e:
        print("!! %s" % e)
        return getattr(e, "code", 2)
    return 0
