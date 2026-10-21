#!/usr/bin/env python3
"""hs-cli — a thin operator front-end over the harness scripts.

Every verb wraps a script that already owns the logic; the CLI adds no new
behaviour, just one discoverable entry point and install-time group selection.

    hs doctor                      verify_install --strict + preflight_deps
    hs migrate [--check|--dry-run] run the decomposition migrate engine
    hs list                        plugins, their skills, and on/off state
    hs emit --target T             what survives projecting the hooks onto runtime T
    hs components --enable G ...    flip a group on/off (--disable G; bare or hs-G)
    hs version                     harness_version + kit_digest from release.json
    hs install [install.py args]   install + interactive group selection

No watch/content/dashboard verbs — those belong to a different tool (YAGNI).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import shutil
import sys
from pathlib import Path
from typing import Any, Iterable, Optional

_SCRIPTS = Path(__file__).resolve().parent
_ROOT = _SCRIPTS.parents[1]  # harness/scripts -> harness -> repo root
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import attrs as _attrs  # noqa: E402  (REQUIRED — see harness/requirements.txt)
import harness_root  # noqa: E402
import hs_family  # noqa: E402
import hs_family.registry as hs_registry  # noqa: E402
from hs_family.cli import CliBase, Command, Outcome, RegistrySource, state_of  # noqa: E402
import off_record  # noqa: E402
import process_group  # noqa: E402
import yaml_io  # noqa: E402


def _catalog_root(args) -> Path:
    """Where the shared skill/component CATALOG lives — `harness/plugins/hs/
    skills/`, `harness/data/components.yaml` — for a verb that has no
    `--root` override. NEVER `CLAUDE_PROJECT_DIR`: under a global install that
    names the PROJECT currently being worked on, a completely different
    directory from the catalog (docs/harness/global-install-guide.md's
    two-zone model — the catalog lives in the shared, read-only bin tree).
    `harness_root.bin_root_env()` (this CLI's shared sibling with hs-run) is
    the explicit, validated `HARNESS_BIN_ROOT` when a global install set one;
    `_ROOT` — this script's OWN physical location — is the correct fallback
    in every other shape (self-host, or a per-project install where the
    harness/ tree, catalog included, is copied inside the project itself),
    because that location IS wherever the catalog physically lives, matching
    the same `_ROOT` the already-correct `list`/`doctor`/`gates`/`guards`/
    `capabilities` verbs use.

    READ-ONLY verbs only. Anything that MOVES a skill directory or writes an
    omit record must go through `_mutable_catalog_root` instead — see there.

    `cmd_cleanup` deliberately does NOT call this: it clears a local `harness/`
    COPY, which only per-project install has, so its `base` still needs
    `CLAUDE_PROJECT_DIR`/`_ROOT` — under a global install there is correctly
    nothing to clean there, since no per-project copy exists at all."""
    root = getattr(args, "root", None)
    if root:
        return Path(root)
    # `_ROOT` first, `bin_root_env()` only as a fallback. The tree this running
    # script physically belongs to is the most specific answer available, and an
    # ambient env var must not override it: with the order reversed, a per-project
    # install whose shell still carried a stale `HARNESS_BIN_ROOT` from some other
    # global install read (and, before `_mutable_catalog_root` below, WROTE) a
    # completely different tree — failing to do what was asked while damaging
    # something it was never pointed at. Under a global install `_ROOT` already IS
    # the bin root, because that mode runs the scripts out of the shared checkout
    # rather than copying them, so the original bug stays fixed either way.
    if (_ROOT / "harness" / "plugins" / "hs" / "skills").is_dir():
        return _ROOT
    return harness_root.bin_root_env() or _ROOT


def _mutable_catalog_root(args) -> Path:
    """Same lookup as `_catalog_root`, but refuses the SHARED bin tree.

    `--disable`/`--enable`/`--off`/`--on` do not read a catalog, they MOVE skill
    directories on disk and write an omit record. Under a global install the
    catalog they would move is the one every project on the machine shares, and
    `docs/harness/global-install-guide.md` is explicit that the shared binary is
    never written to by a running project — "that separation is the whole point".

    Measured before this existed: two projects on one shared bin, project A runs
    `hs-cli skills --disable bakeoff`, and the skill disappears for project B,
    which asked for nothing. Silently — the omit record written alongside it is
    exactly what makes `verify_install --strict` still report clean in the victim
    project. `hs-cli components --disable <group>` widens the same blast to a whole
    group in one command. None of the write guards catch it: they read the shell
    command line, and this write happens inside the Python process.

    An explicit `--root` still works, because then the operator has named the tree
    they mean rather than inheriting it from an env var.
    """
    root = getattr(args, "root", None)
    if root:
        return Path(root)
    bin_root = harness_root.bin_root_env()
    resolved = _catalog_root(args)
    if bin_root is not None and bin_root.resolve() == resolved.resolve():
        raise hs_family.RefusedError(
            "refusing to toggle skills inside the shared bin tree %s — that catalog "
            "serves every project on this machine, and disabling a skill here removes "
            "it from all of them (docs/harness/global-install-guide.md: the shared "
            "binary is read-only at runtime).\n"
            "If you really mean the shared tree, name it: hs-cli skills --root %s ..."
            % (bin_root, bin_root))
    return resolved


def _run(argv: list[str]) -> int:
    """Run a child process, inheriting stdio, and return its exit code."""
    return subprocess.run(argv).returncode


# --------------------------------------------------------------------- verbs

def cmd_version(args) -> int:
    return hs_family.common.print_version(_ROOT)


def cmd_trust(args) -> int:
    """Trust a repo root (TOFU) so its rule shell-detectors may auto-fire, or
    list the trusted roots. Per-machine, recorded outside git."""
    import trust_store
    if args.list:
        for root in sorted(trust_store.load_trust()):
            print(root)
        return 0
    try:
        rp = trust_store.add_trust(args.repo)
    except trust_store.TrustError as exc:
        label = "trust refused" if exc.exit_code == hs_family.EXIT_REFUSED else "trust error"
        print("%s: %s" % (label, exc), file=sys.stderr)
        return int(exc.exit_code)
    print("trusted: %s" % rp)
    fp = trust_store.command_fingerprint(rp)
    if fp is not None:
        print("commands (fingerprint %s):" % fp[:16])
        cmds = trust_store.declared_commands(rp)
        for argv in cmds:
            print("  " + " ".join(argv))
        files = trust_store.command_files(rp, cmds)
        print("  fingerprinted: %s (modules those scripts import are not)" % (", ".join(files) or "no script in the repo"))
    return 0


def _registry_findings() -> list[str]:
    """One line per broken domain row across the `hs-*` registries: a declared
    `module:` that does not import, or a domain module on disk no row declares.
    Dispatch imports lazily, so without this a typo'd row only breaks for the one
    user who calls that exact verb."""
    import hs_code
    import hs_run
    findings = []
    families = (
        ("hs-run", hs_run.load_registry, hs_run._REGISTRY_ENV, hs_run._ROOT),
        ("hs-code", lambda: hs_registry.load_registry(
            hs_code._REGISTRY_ENV, hs_code._ROOT,
            extra_frag_dirs=hs_code._pack_frag_dirs(hs_code.ShellOptions())).domains,
         hs_code._REGISTRY_ENV, hs_code._ROOT),
    )
    for cli, load, env, root in families:
        try:
            domains = load()
        except Exception as e:  # noqa: BLE001 — an unreadable registry is a finding here
            findings.append("%s: registry unreadable: %s" % (cli, e))
            continue
        for module, exc in sorted(hs_registry.import_declared_modules(domains).items()):
            if exc is not None:
                findings.append("%s: module %s does not import: %s" % (cli, module, exc))
        for module in hs_registry.find_undeclared_domain_modules(env, root, domains):
            findings.append("%s: module %s is on disk but no registry row declares it"
                            % (cli, module))
    # `doctor` is the one place hs-cli's own registry health is checked as a FINDING,
    # regardless of what is typed: every other command refuses on it (`HsCli.admit`).
    try:
        loaded = hs_registry.load_registry(hs_registry.family_env("hs-cli"), _ROOT)
    except hs_registry.RegistryError as e:
        findings.append("hs-cli: registry unreadable: %s" % e)
    else:
        findings.extend(_hs_cli_verb_level_moved_under_live_domain(loaded))
    return findings


def _hs_cli_verb_level_moved_under_live_domain(loaded) -> list[str]:
    """A verb-level `moved_to` row (`(domain, verb)`, e.g. `{trust: {list:
    {moved_to: ...}}}`) whose DOMAIN is a live hs-cli command: hs-cli's commands are one
    word, so `trust list` dispatches into `trust`'s own argv and this row is never
    consulted by name."""
    live = set(loaded.domains)
    return sorted(
        "hs-cli: moved_to row '%s' is unreachable — '%s' is a live command, "
        "so hs-cli dispatches '%s' as its own argv before this row is ever "
        "consulted by name" % (" ".join(old), old[0], " ".join(old))
        for old in loaded.moved if len(old) > 1 and old[0] in live)


def cmd_doctor(args) -> int:
    # verify_install reds on inconsistency; preflight never blocks (advisory).
    rc = _run([sys.executable, str(_SCRIPTS / "verify_install.py"), "--strict"])
    _run([sys.executable, str(_SCRIPTS / "preflight_deps.py")])
    findings = _registry_findings()
    print("registry: %s" % ("clean" if not findings else "%d finding(s)" % len(findings)))
    for line in findings:
        print("  %s" % line)
    # verify_install's own exit codes: 0 clean, 1 drift found (a FINDING), and
    # (via its own hs_family-wrapped entrypoint) 2 on an unclassified crash.
    # Collapsing every nonzero to 1 used to make "verify found something" and
    # "verify itself broke" indistinguishable — kept apart here on purpose.
    if rc not in (0, 1):
        return 2
    return 1 if rc == 1 or findings else 0


def cmd_gates(args) -> int:
    """Report the resolved stage-gate posture (per-stage hard/soft + required
    artifacts, posture overrides, the opt-in security-scan gate), then the gate
    registry: rows per page and every gate that is off, with its layer and reason.
    With --off/--on it switches gates in THIS project's layer instead."""
    if getattr(args, "off", None) or getattr(args, "on", None):
        return _gates_switch(args)
    if getattr(args, "whole_tree", False):
        return _gates_whole_tree(args)
    sys.path.insert(0, str(_SCRIPTS))
    import artifact_check
    try:
        stages = artifact_check.load_policy().get("stages", {}) or {}
    except Exception as e:  # noqa: BLE001 — a malformed policy should say so, not crash
        print("could not load stage-policy: %s" % e, file=sys.stderr)
        return 2  # broken config source, not a finding about a healthy policy
    overrides = [k for k in ("HARNESS_STAGE_POLICY", "HARNESS_GUARD_POLICY",
                             "HARNESS_PROTECTED_BRANCHES")
                 if (os.environ.get(k) or "").strip()]
    print("stage-gate posture (source: %s)"
          % ("ENV OVERRIDE" if overrides else "tracked stage-policy.yaml"))
    if overrides:
        print("  ! posture override(s) active: %s — in-session policy is redirected; "
              "the pre-push transport re-judges with tracked config" % ", ".join(overrides))
    for name, spec in stages.items():
        if not isinstance(spec, dict):
            continue
        reqs = spec.get("requires") or []
        rp = "" if spec.get("require_plan", True) else "  [require_plan: false]"
        print("  %-8s %-4s  requires: %s%s"
              % (name, "HARD" if spec.get("hard") else "soft",
                 ", ".join(str(r) for r in reqs) if reqs else "(none)", rp))
    sec_on = any("security-scan" in (s.get("requires") or [])
                 for s in stages.values() if isinstance(s, dict))
    print("  security-scan gate: %s"
          % ("ON" if sec_on else "OFF (opt-in — add 'security-scan' to a stage's requires)"))
    from hs_family import gate_registry
    try:
        book = gate_registry.load(_catalog_root(args))
    except hs_family.registry.RegistryError as e:
        print("could not load the gate registry: %s" % e, file=sys.stderr)
        return 2
    pages = book.by_page()
    print("gate registry (%s)" % ", ".join(str(f) for f in book.files))
    for page in sorted(pages):
        rows = pages[page]
        attached = sum(1 for r in book.rows if page in r.attached and r.page != page)
        found = sum(1 for r in rows if r.source)
        split = (" (%d found + %d literal)" % (found, len(rows) - found)
                 if found and found < len(rows) else "")
        print("  %s: %d%s%s" % (page, len(rows), split,
                                " (+%d on other pages' rows)" % attached if attached else ""))
    print("  total: %d" % len(book.rows))
    print("  whole_tree: %d (hs-cli gates --whole-tree)" % len(book.whole_tree()))
    import harness_paths
    home = harness_paths.data_root()
    try:
        offs = gate_registry.gates_off(book, _catalog_root(args),
                                       None if harness_paths.data_root_unresolved(home) else home)
    except hs_family.registry.RegistryError as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    for g in offs:
        print(gate_registry.off_line(g))
    return 0


def _record(root: Path, reason: str) -> dict:
    """Who, when and why — what every off in a project layer carries."""
    import datetime
    return off_record.make(
        reason=reason, actor=_hook_runtime(root).resolve_actor(),
        ts=datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"))


def _gates_switch(args) -> int:
    """`gates --off/--on`: a hook goes to its own layer through the hooks door, a gate
    on a `gates.yaml` page to that file, anything else is refused. An off needs --reason."""
    import argparse
    import artifact_io
    import harness_paths
    import yaml_io
    from hs_family import gate_registry
    root = _catalog_root(args)
    off_csv = _split_csv(getattr(args, "off", None))
    on_csv = _split_csv(getattr(args, "on", None))
    reason = (getattr(args, "reason", None) or "").strip()
    if off_csv and not reason:
        print("error: --off needs --reason '<why>' — every off says who, when and why",
              file=sys.stderr)
        return 2
    try:
        rows = {r.name: r for r in gate_registry.load(root).rows}
    except hs_family.registry.RegistryError as e:
        print("could not load the gate registry: %s" % e, file=sys.stderr)
        return 2
    hooks: dict = {"off": [], "on": []}
    gates: dict = {"off": [], "on": []}
    for verb, names in (("off", off_csv), ("on", on_csv)):
        for name in names:
            row = rows.get(name)
            if row is None:
                print("error: no gate named %s — run `hs-cli gates` for the registry"
                      % name, file=sys.stderr)
                return 2
            if verb == "off" and row.fields.get("floor") is True:
                print("error: %s is a floor rule — no project layer turns it off, --force "
                      "included" % name, file=sys.stderr)
                return int(hs_family.EXIT_REFUSED)
            if row.layer == gate_registry.HOOKS_LAYER:
                hooks[verb].append(name)
            elif row.layer == gate_registry.GATES_LAYER:
                gates[verb].append(name)
            else:
                print("error: %s is on page %s, whose reader does not honour a project "
                      "layer — it cannot be switched from a project" % (name, row.page),
                      file=sys.stderr)
                return 2
    record = _record(root, reason) if reason else None
    if hooks["off"] or hooks["on"]:
        rc = cmd_hooks(argparse.Namespace(
            off=[",".join(hooks["off"])] if hooks["off"] else [],
            on=[",".join(hooks["on"])] if hooks["on"] else [],
            force=getattr(args, "force", False), reason=reason or None,
            root=getattr(args, "root", None), record=record))
        if rc:
            return rc
    if gates["off"] or gates["on"]:
        home, why = harness_paths.data_root_and_reason()
        if harness_paths.data_root_unresolved(home):
            print("error: no project data home resolved — %s" % why, file=sys.stderr)
            return 2
        path = home / gate_registry.GATES_LAYER
        try:
            layer = gate_registry.read_project_layer(path)
        except hs_family.registry.RegistryError as e:
            print("error: %s — fix or delete it first" % e, file=sys.stderr)
            return 2
        for name in gates["off"]:
            layer[name] = dict({"enabled": False}, **(record or {}))
        for name in gates["on"]:
            layer.pop(name, None)
        path.parent.mkdir(parents=True, exist_ok=True)
        artifact_io.atomic_write_text(
            path,
            "# gates.yaml — THIS PROJECT's gate layer (hooks live in harness-hooks.yaml).\n"
            "# One entry turns one gate off and says who, when and why.\n"
            "# Written by `hs-cli gates --off/--on`.\n"
            + yaml_io.safe_dump({"gates": layer}, sort_keys=True))
        for name in gates["off"]:
            print("off (this project): %s" % name)
        for name in gates["on"]:
            print("on (this project): %s" % name)
        print("→ %s" % path)
    return 0


def _gates_whole_tree(args) -> int:
    """`<gate> <file>` per gate a change anywhere can redden — what a narrow run adds."""
    from hs_family import gate_registry
    try:
        book = gate_registry.load(_catalog_root(args))
    except hs_family.registry.RegistryError as e:
        print("could not load the gate registry: %s" % e, file=sys.stderr)
        return 2
    for r in book.whole_tree():
        print("%s %s" % (r.name, r.fields.get("file") or r.module or "-"))
    return 0


def cmd_guards(args) -> int:
    """Report the resolved GUARD posture: the off/warn/block preset + any per-guard
    override, and the protected branches. Pairs with `gates` (stage gates) for the
    full picture. Read-only."""
    sys.path.insert(0, str(_SCRIPTS))
    import branch_policy
    import guard_policy
    overrides = [k for k in ("HARNESS_GUARD_POLICY", "HARNESS_PROTECTED_BRANCHES")
                 if (os.environ.get(k) or "").strip()]
    print("guard posture (source: %s)"
          % ("ENV OVERRIDE" if overrides else "tracked config"))
    if overrides:
        print("  ! override(s) active: %s — in-session only; pre-push uses tracked config"
              % ", ".join(overrides))
    try:
        pol = guard_policy.load_guard_policy()
        print("  preset: %s" % pol.get("preset", "?"))
        ov = pol.get("overrides")
        print("  per-guard overrides: %s"
              % (ov if ov else "(none — every guard runs at the preset)"))
    except Exception as e:  # noqa: BLE001 — a bad policy should report, not crash
        print("  guard-policy: could not load (%s)" % e)
    try:
        prot = branch_policy.load_protected()
        print("  protected branches: %s" % (", ".join(prot) if prot else "(none)"))
    except Exception as e:  # noqa: BLE001
        print("  protected-branches: could not load (%s)" % e)
    return 0


def _hook_registry(root: Path) -> list:
    """Parse hooks-registration.yaml into [{name, event, class}], name = the hook
    script's basename. The installer's registration is the COMPLETE hook map;
    component_config only knows the 3 hook-bearing components, so it would
    under-report. A hook_dispatch.py command is EXPANDED into its per-core hooks
    (from hook-dispatch.yaml) — the migrated leaf gates still fire in-process, and
    tầng-2 discovery must see each of them, not the multiplexer. Read-only, off the
    harness root (never CWD)."""
    import yaml_io
    reg = root / "harness" / "install" / "hooks-registration.yaml"
    data = yaml_io.safe_load(reg.read_text(encoding="utf-8")) or {}
    disp: dict = {}
    disp_path = root / "harness" / "data" / "hook-dispatch.yaml"
    if disp_path.is_file():
        dd = yaml_io.safe_load(disp_path.read_text(encoding="utf-8")) or {}
        for gk, cores in (dd.get("groups") or {}).items():
            ev, _, mt = str(gk).partition(":")
            disp[(ev, mt or None)] = cores or []
    out = []
    for entry in (data.get("hooks") or []):
        if not isinstance(entry, dict):
            continue
        name = None
        for tok in str(entry.get("command", "")).split():
            if tok.endswith(".py"):
                name = os.path.basename(tok)[:-3]
        if not name:
            continue
        if name == "hook_dispatch":
            for c in disp.get((entry.get("event"), entry.get("matcher")), []):
                if isinstance(c, dict) and c.get("module"):
                    out.append(RegisteredHook(
                        name=c["module"], event=entry.get("event", ""),
                        hook_class=c.get("class", "nudge")).as_dict())
            continue
        out.append(RegisteredHook(name=name, event=entry.get("event", ""),
                                  hook_class=entry.get("class", "nudge")).as_dict())
    return out


def known_hook_names(root: Path) -> dict:
    """Every hook name this harness knows, mapped to its class.

    Three sources, because no single one is complete: the shipped on/off registry
    lists what ships toggled, the dispatch registry lists the in-process cores, and
    the installer registration lists the separately-wired scripts. A name-check
    built on one of them refuses names that are perfectly real, which teaches the
    operator to reach past the door and hand-edit the file.

    The dispatch registry keys config by `name` when it carries one and by `module`
    otherwise, and `hook_runtime` looks up the former — so both spellings are known
    here, or a legal name reads as a typo.
    """
    import yaml_io
    out: dict = {}
    for h in _hook_registry(root):
        out[h["name"]] = h.get("class") or "nudge"
    disp = root / "harness" / "data" / "hook-dispatch.yaml"
    if disp.is_file():
        try:
            dd = yaml_io.safe_load(disp.read_text(encoding="utf-8")) or {}
            for cores in (dd.get("groups") or {}).values():
                for c in cores or []:
                    if isinstance(c, dict) and c.get("module"):
                        cls = c.get("class") or "nudge"
                        out.setdefault(c["module"], cls)
                        if c.get("name"):
                            out[c["name"]] = cls
        except Exception:  # noqa: BLE001 — a malformed registry must not break the door
            pass
    cfg = root / "harness" / "data" / _HOOK_CONFIG_NAME
    if cfg.is_file():
        try:
            raw = yaml_io.safe_load(cfg.read_text(encoding="utf-8")) or {}
            for name in (raw.get("hooks") or {}):
                out.setdefault(name, "nudge")
        except Exception:  # noqa: BLE001
            pass
    return out



def hook_gates(root: Path) -> dict:
    """Gate-registry source for the `hook` page: `{name: {class, module?, core?}}`, one
    row per known hook. `module` is set where a registry names the file that runs it;
    `core` marks a fail-closed gate the project layer loosens only with a reason."""
    root = Path(root)
    modules = {h["name"] for h in _hook_registry(root)}
    core = _hook_runtime(root).force_required_names()
    return {name: dict({"class": cls}, **({"module": name} if name in modules else {}),
                       **({"core": True} if name in core else {}))
            for name, cls in known_hook_names(root).items()}

_HOOK_CONFIG_NAME = "harness-hooks.yaml"


def _hook_runtime(root: Path):
    """The hook-side runtime module. It lives in harness/hooks, not harness/scripts,
    so the CLI has to put that directory on the path before importing it — and it
    must be the SAME module the hooks import, or the door would report a posture no
    hook actually runs under."""
    hooks_dir = str(Path(root) / "harness" / "hooks")
    if hooks_dir not in sys.path:
        sys.path.insert(0, hooks_dir)
    import hook_runtime
    return hook_runtime


def _hook_overlay_path(root: Path) -> Path:
    """The project layer's file, resolved the same way the hook runtime resolves it.

    Deliberately NOT `_catalog_root()`: the catalog is the shared read-only binary,
    and this file is the one piece of hook config that belongs to the project being
    worked on. Asking the same resolver the runtime asks is what keeps the door and
    the reader pointed at one file.
    """
    return Path(_hook_runtime(root)._overlay_path())


def _guarded_names() -> set:
    """Names whose posture is owned by the guard-policy plane, not by this file."""
    try:
        import guard_policy
        return set(guard_policy.GUARD_REGISTRY)
    except Exception:  # noqa: BLE001 — no policy engine means nothing is reserved
        return set()


_LAYER_HEADER = (
    "# harness-hooks.yaml — THIS PROJECT's hook layer.\n"
    "#\n"
    "# Merged over the shipped registry: a name absent here keeps whatever the\n"
    "# shipped file says, and a key absent from an entry keeps its shipped value.\n"
    "# Written by `hs-cli hooks --off/--on`; hand edits are fine.\n")


def _flow_scalar(value) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return str(value)
    return "'%s'" % str(value).replace("'", "''")


def _flow_entry(entry: dict) -> str:
    keys = (["enabled"] if "enabled" in entry else []) + sorted(k for k in entry if k != "enabled")
    return "{%s}" % ", ".join("%s: %s" % (k, _flow_scalar(entry[k])) for k in keys)


def _splice_hook_entries(text: str, entries: dict) -> str:
    """Rewrite only the named entries under `hooks:`, one flow line each.

    The layer is hand-edited YAML, so every other line — header, comments, top-level
    keys, entries nobody asked about — is kept byte for byte. A named entry that
    exists (flow or block form) is replaced in place; a new one goes at the end of
    the section. The caller re-parses the result and falls back if it disagrees."""
    import re
    lines = text.splitlines()
    if not lines:
        lines = _LAYER_HEADER.rstrip("\n").split("\n")
    head = next((i for i, ln in enumerate(lines) if re.match(r"^hooks:\s*(\{\s*\})?\s*(#.*)?$", ln)), None)
    if head is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("hooks:")
        head = len(lines) - 1
    else:
        lines[head] = "hooks:"
    end = next((j for j in range(head + 1, len(lines))
                if lines[j].strip() and lines[j][0] not in " \t"), len(lines))
    indent = next((m.group(1) for m in (re.match(r"^(\s+)[A-Za-z_][\w-]*:", lines[j])
                                        for j in range(head + 1, end)) if m), "  ")
    for name, entry in entries.items():
        rendered = "%s%s: %s" % (indent, name, _flow_entry(entry))
        start = next((k for k in range(head + 1, end)
                      if re.match("^%s%s:(\\s|$)" % (re.escape(indent), re.escape(name)), lines[k])), None)
        if start is None:
            last = max([head] + [j for j in range(head + 1, end) if lines[j].strip()])
            lines.insert(last + 1, rendered)
            end += 1
            continue
        stop = start + 1
        while stop < end and lines[stop].strip() and \
                len(lines[stop]) - len(lines[stop].lstrip()) > len(indent):
            stop += 1
        tail = re.search(r"\}\s*(#.*)$", lines[start]) if stop == start + 1 else None
        if tail:
            rendered += "  " + tail.group(1)
        lines[start:stop] = [rendered]
        end -= (stop - start) - 1
    return "\n".join(lines) + "\n"


def cmd_hooks(args) -> int:
    """Report the resolved hook posture and, with --off/--on, write the PROJECT layer.

    Bare is read-only and names the layer that decided each row. That reporting half
    is the point: the defect this plane exists for was hooks that stopped firing with
    nothing on screen saying why.

    A write goes to the project's own data home and never to the shared binary, so
    one project's choice cannot reach another's. Unknown names are refused — a row
    nothing reads is the quietest way to believe a hook is off while it keeps firing
    — and turning off a name owned by the guard-policy plane needs --force, because a
    typo must not silently disarm a safety guard. Every off is written with who, when
    and why; `--on` needs no --force and drops that record with the off (and an
    advisory mode), so a later bare off cannot inherit an old reason.
    """
    import yaml_io
    import harness_paths
    problem = harness_paths.data_root_env_problem()
    if problem:
        print("error: %s" % problem, file=sys.stderr)
        return 2
    root = _catalog_root(args)
    hook_runtime = _hook_runtime(root)
    known = known_hook_names(root)
    overlay = _hook_overlay_path(root)
    off_csv = _split_csv(getattr(args, "off", None))
    on_csv = _split_csv(getattr(args, "on", None))

    if off_csv or on_csv:
        unknown = [n for n in off_csv + on_csv if n not in known]
        if unknown:
            print("error: unknown hook name(s): %s\n"
                  "       run `hs-cli hooks` for the names this harness knows"
                  % ", ".join(sorted(unknown)), file=sys.stderr)
            return 2
        force = bool(getattr(args, "force", False))
        try:
            fail_closed = hook_runtime.force_required_names()
        except Exception as e:  # noqa: BLE001 — unreadable registry: nothing is proven safe
            if not force:
                print("error: hook-dispatch.yaml is unreadable (%s), so no name can be "
                      "shown not to be a fail-closed gate — fix it, or pass --force"
                      % e, file=sys.stderr)
                return 2
            fail_closed = frozenset(off_csv)
        reserved = sorted(set(off_csv) & (_guarded_names() | fail_closed))
        if reserved and not force:
            print("error: %s is a fail-closed safety gate (or owned by the guard-policy "
                  "plane).\n       pass --force --reason '<why>' to override it from "
                  "this project's layer" % ", ".join(reserved), file=sys.stderr)
            return 3  # a known-valid name declined on purpose, not a usage error
        reason = ((getattr(args, "reason", None) or "").strip()
                  or ("hs-cli hooks --force" if force else "hs-cli hooks --off"))
        doc: dict = {}
        if overlay.is_file():
            try:
                doc = yaml_io.safe_load(overlay.read_text(encoding="utf-8")) or {}
            except Exception as e:  # noqa: BLE001
                print("error: %s is unreadable (%s) — fix or delete it first"
                      % (overlay, e), file=sys.stderr)
                return 2
        if not isinstance(doc, dict):
            doc = {}
        hooks = doc.get("hooks")
        if not isinstance(hooks, dict):
            hooks = {}
        record = getattr(args, "record", None) or (_record(root, reason) if off_csv else {})
        for name in off_csv:
            hooks.setdefault(name, {})["enabled"] = False
            if name in fail_closed:
                hooks[name]["force_reason"] = reason
            hooks[name].update(record)
        for name in on_csv:
            entry = hooks.setdefault(name, {})
            entry["enabled"] = True
            for key in ("force_reason", *off_record.RECORD_KEYS):
                entry.pop(key, None)
            if entry.get("mode") == "advisory":
                entry.pop("mode")
        doc["hooks"] = hooks
        overlay.parent.mkdir(parents=True, exist_ok=True)
        import artifact_io
        old_text = overlay.read_text(encoding="utf-8") if overlay.is_file() else ""
        changed = {n: hooks[n] for n in off_csv + on_csv}
        new_text = _splice_hook_entries(old_text, changed)
        try:
            spliced_ok = (yaml_io.safe_load(new_text) or {}).get("hooks") == hooks
        except Exception:  # noqa: BLE001 — an unparseable splice falls back below
            spliced_ok = False
        if not spliced_ok:
            print("warning: %s could not be edited in place; rewriting it (comments "
                  "are lost)" % overlay, file=sys.stderr)
            new_text = _LAYER_HEADER + yaml_io.safe_dump(doc, sort_keys=True)
        artifact_io.atomic_write_text(overlay, new_text)
        if off_csv:
            print("off (this project): %s" % ", ".join(off_csv))
        if on_csv:
            print("on (this project): %s" % ", ".join(on_csv))
        print("→ %s" % overlay)
        return 0

    hook_runtime._reset_config_cache()
    refused = set(hook_runtime.hook_config_refused_names())
    touched = hook_runtime.hook_config_overlay_names()
    header = "hook posture — project layer: %s" % (
        overlay if overlay.is_file() else "none (%s)" % overlay)
    if touched:
        header += " (touched: %s)" % ", ".join(touched)
    print(header)
    for name in sorted(known):
        cls = known[name]
        try:
            on = hook_runtime.hook_enabled(name, cls)
            mode = hook_runtime.hook_mode(name, cls)
            source = hook_runtime.hook_enabled_source(name, cls)
        except ValueError:
            continue
        if on and mode == "advisory" and cls == "compliance":
            source = hook_runtime.hook_mode_source(name, cls)
        if name in refused:
            source = "project-refused(no force_reason or record)->" + source
        print("  %-34s %-3s %-9s %-11s %s"
              % (name, "on" if on else "off", mode, cls, source))
    if refused:
        print("refused: %s — a fail-closed gate is loosened only with a force_reason "
              "and its record (reason, actor, ts)"
              % ", ".join(sorted(refused)), file=sys.stderr)
    return 0


# ------------------------------------------- the hs-capabilities wire contract
#
# Declared, not assembled. `hs-capabilities/1` crosses a subprocess seam into the
# tầng-2 orchestrator, which has no other way to discover what hooks and gates exist.
# It used to be a dict literal, and a dict literal cannot be asked what its fields are:
# deleting `class` from the emitted hooks left the harness suite AND the orchestrator's
# own 60-test discovery suite entirely green. A consumer that reads `None` because the
# producer stopped sending a key fails somewhere else, as a wrong answer.
#
# Same fix as `hs_run.Envelope`: each field DECLARES the wire key it owes, and the gate
# is derived from the declaration, so it catches the NEXT field rather than this one.
# `wire` metadata exists for the single field whose JSON key is a Python keyword.

@_attrs.define(frozen=True, kw_only=True)
class RegisteredHook:
    """One row of the hook registry as DISCOVERY sees it, before `enabled` is
    resolved. Separate from `HookCapability` on purpose: this shape is what the
    settings/dispatch files say exists, and the capability is that plus a live
    on/off answer. Collapsing them would make "declared" and "enabled" one
    field, which is the distinction a consumer is reading the map to learn."""

    name = _attrs.field()
    event = _attrs.field(default="")
    hook_class = _attrs.field(default="nudge", metadata={"wire": "class"})

    def as_dict(self) -> dict:
        return {"name": self.name, "event": self.event, "class": self.hook_class}


CAPABILITIES_SCHEMA = "hs-capabilities/1"


@_attrs.define(frozen=True, kw_only=True)
class HookCapability:
    """One registered hook, as the orchestrator reads it."""
    name: str = _attrs.field()
    event: str = _attrs.field()
    enabled: bool = _attrs.field(converter=bool)
    hook_class: str = _attrs.field(metadata={"wire": "class"})


@_attrs.define(frozen=True, kw_only=True)
class EmitReport:
    """One target's projection, as a consumer reads it. A declared shape rather than
    a dict literal so a field dropped here is a TypeError at the construction site,
    not a None surfacing at whatever reads the JSON."""
    schema: str = _attrs.field()
    target: str = _attrs.field()
    decisions: tuple = _attrs.field(converter=tuple)
    refusals: tuple = _attrs.field(converter=tuple)
    ok: bool = _attrs.field(converter=bool)
    written: object = _attrs.field()


@_attrs.define(frozen=True, kw_only=True)
class GateCapability:
    """One stage gate and the artifact kinds it requires."""
    stage: str = _attrs.field()
    requires: tuple = _attrs.field(converter=tuple)


def _as_tuple(value: Iterable[Any]) -> tuple:
    """`tuple`, but with a signature. Passing the builtin directly makes attrs' mypy
    plugin derive the parameter type from `tuple.__init__`, so every caller handing in
    a list — which is the entire reason the converter is here — reads as a type error
    at the call site instead of being converted."""
    return tuple(value)


@_attrs.define(frozen=True, kw_only=True)
class Capabilities:
    """The whole payload. `schema` carries a default because a bump is a deliberate
    edit here, never something a caller passes in per invocation."""
    schema: str = _attrs.field(default=CAPABILITIES_SCHEMA)
    hooks: tuple = _attrs.field(converter=_as_tuple)
    gates: tuple = _attrs.field(converter=_as_tuple)

    def as_dict(self) -> dict:
        return {"schema": self.schema,
                "hooks": [_wire(h) for h in self.hooks],
                "gates": [_wire(g) for g in self.gates]}


def _wire(obj) -> dict:
    """One declared value object as its JSON form — key order and key NAMES both taken
    from the declaration, so a renamed field cannot silently keep its old wire key."""
    out = {}
    for field in _attrs.fields(type(obj)):
        value = getattr(obj, field.name)
        out[field.metadata.get("wire", field.name)] = (
            list(value) if isinstance(value, tuple) else value)
    return out


def cmd_emit(args) -> int:
    """Project the harness's hook registrations onto another runtime and report
    what survives. READ-ONLY — it decides and records, it never writes an install.

    Exit 3 (not 0) when a compliance gate would register and never fire on the
    target. That is the whole point of the verb: an inert gate is invisible at
    runtime, so the only moment it can be caught is here. `--allow-inert-gates`
    suppresses the EXIT, never the record."""
    import target_projection as tp
    try:
        targets = tp.load_targets()
    except Exception as e:  # noqa: BLE001 — a broken declaration is an error state
        print("emit: could not read the target declarations: %s" % e, file=sys.stderr)
        return 2  # broken config source, not a finding about a healthy target
    if args.target not in targets:
        print("emit: unknown target %r — declared targets: %s"
              % (args.target, ", ".join(sorted(targets)) or "(none)"), file=sys.stderr)
        return 2
    proj = tp.project_target(args.target, targets=targets)
    if args.ledger:
        tp.write_ledger(proj, args.ledger, target_id=args.target)
    written = None
    if args.out:
        # Refuse BEFORE writing. An install that exists on disk gets used; the
        # point of the refusal is that this one should not.
        if proj.refusals and not args.allow_inert_gates:
            print("emit: refusing to write an install for %s — %d compliance gate(s) "
                  "would be absent from it. Re-run with --allow-inert-gates once you "
                  "have read the refusals below."
                  % (args.target, len(proj.refusals)), file=sys.stderr)
            for r in proj.refusals:
                print("REFUSE  %s" % r, file=sys.stderr)
            return 3
        if args.skills is None:
            skills = sorted(d.name for d in (_ROOT / "harness" / "plugins" / "hs" /
                                             "skills").iterdir()
                            if (d / "SKILL.md").is_file())
        else:
            skills = [x.strip() for x in args.skills.split(",") if x.strip()]
        if args.agents is None:
            agents = sorted(f.stem for f in (_ROOT / "harness" / "plugins" / "hs" /
                                             "agents").glob("*.md"))
        else:
            agents = [x.strip() for x in args.agents.split(",") if x.strip()]
        try:
            written = str(tp.write_install(proj, args.out, target_id=args.target,
                                           project_root=args.project_root or _ROOT,
                                           skills=skills, agents=agents))
        except ValueError as exc:
            # The writer refuses on purpose. Surfaced as a refusal, not a traceback:
            # an operator reading a stack trace cannot tell a deliberate stop from a
            # crash, and exit 3 is what every other refusal on this path returns.
            shutil.rmtree(args.out, ignore_errors=True)
            print("emit: refusing to write an install for %s — %s"
                  % (args.target, exc), file=sys.stderr)
            return 3
    payload = _wire(EmitReport(
        schema=tp.SCHEMA,
        target=args.target,
        decisions=[d.as_record(args.target) for d in proj.decisions],
        refusals=proj.refusals,
        ok=proj.ok,
        written=written,
    ))
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for d in proj.decisions:
            print("%-11s %-34s %s" % (d.kind, d.group, d.reason))
        for r in proj.refusals:
            print("REFUSE  %s" % r, file=sys.stderr)
    if proj.refusals and not args.allow_inert_gates:
        return 3
    return 0


def cmd_capabilities(args) -> int:
    """Emit the harness's registered hooks + stage gates as JSON (read-only) — the
    tầng-2 orchestrator's path-free discovery source (the CLI exposes no hook/gate
    registry, spike 260709). Wraps the existing config layer: hooks-registration
    (name/event/class) + hook_runtime.hook_enabled (live state) + stage-policy (gates).
    Never re-implements gate logic. Fail-loud: a broken config source exits nonzero +
    stderr so the consumer maps to an explicit error state, never a silent empty map."""
    sys.path.insert(0, str(_SCRIPTS))
    sys.path.insert(0, str(_ROOT / "harness" / "hooks"))
    try:
        import artifact_check
        import hook_runtime
        hooks = []
        for h in _hook_registry(_ROOT):
            enabled = hook_runtime.hook_enabled(h["name"], h["class"])
            hooks.append(HookCapability(name=h["name"], event=h["event"],
                                        enabled=bool(enabled), hook_class=h["class"]))
        stages = artifact_check.load_policy().get("stages", {}) or {}
        gates = [GateCapability(stage=name, requires=list(spec.get("requires") or []))
                 for name, spec in stages.items() if isinstance(spec, dict)]
    except Exception as e:  # noqa: BLE001 — a broken source is an error state, reported
        print("capabilities: could not build map: %s" % e, file=sys.stderr)
        return 2  # broken config source, not a finding about a healthy map
    print(json.dumps(Capabilities(hooks=hooks, gates=gates).as_dict(),
                     ensure_ascii=False))
    return 0


def _did_you_mean(typed: str, known) -> str:
    """` (did you mean 'x'?)`, or "" when nothing is near enough.

    Delegates to `hs_run.suggest_name` rather than carrying its own scorer and cutoff:
    two copies of a fuzzy threshold is two thresholds, and they drift the first time
    one is tuned. Imported inside the function because this path only runs when a
    name is already wrong -- `hs_cli --version` should not pay for the registry
    module."""
    import hs_run
    return hs_run._did_you_mean(typed, known)


def _component_name(group: str) -> str:
    """Accept either the plugin name (hs-flow) or the bare component (flow)."""
    return group[3:] if group.startswith("hs-") else group


def _default_settings_path() -> str:
    """The same .claude/settings.json the installer writes enabledPlugins into.
    Honors CLAUDE_PROJECT_DIR (the running project) like the rest of the CLI."""
    base = Path(os.environ.get("CLAUDE_PROJECT_DIR") or _ROOT)
    return str(base / ".claude" / "settings.json")


def _skill_args(args, enable=None, disable=None):
    """A cmd_skills-shaped namespace, carrying the components verb's --root."""
    import argparse
    return argparse.Namespace(enable=enable, disable=disable,
                              root=getattr(args, "root", None))


def cmd_components(args) -> int:
    import component_config as cc
    base = _catalog_root(args)
    comps = cc.load_components(base / "harness" / "data" / "components.yaml")
    if not args.enable and not args.disable:
        argv = ["show"]
        if args.policy_file:
            argv += ["--policy-file", args.policy_file]
        if args.hooks_file:
            argv += ["--hooks-file", args.hooks_file]
        if args.state_file:
            argv += ["--state-file", args.state_file]
        if args.settings_file:
            argv += ["--settings-file", args.settings_file]
        return cc.main(argv)

    # Post-collapse there is one plugin, so the former plugin groups are SKILL
    # LABELS: toggling them must omit/restore their skill dirs, not flip a dead
    # enabledPlugins key. Only the hook-bearing components (rbac, decision-capture)
    # still ride the hook-flag path. Classify each requested name accordingly.
    hook_sel: dict[str, bool] = {}
    label_enable, label_disable = [], []
    for g in args.enable or []:
        n = _component_name(g)
        if comps.get(n, {}).get("hooks"):
            hook_sel[n] = True
        else:
            label_enable.append(n)
    for g in args.disable or []:
        n = _component_name(g)
        if comps.get(n, {}).get("hooks"):
            hook_sel[n] = False
        else:
            label_disable.append(n)

    rc = 0
    for grp in label_enable:
        skills = list(comps.get(grp, {}).get("skills") or [])
        if not skills:
            print("error: unknown component/group %r%s" % (grp, _did_you_mean(grp, comps)),
                  file=sys.stderr)
            rc = 2
            continue
        rc = cmd_skills(_skill_args(args, enable=skills)) or rc
    for grp in label_disable:
        skills = list(comps.get(grp, {}).get("skills") or [])
        if not skills:
            print("error: unknown component/group %r%s" % (grp, _did_you_mean(grp, comps)),
                  file=sys.stderr)
            rc = 2
            continue
        rc = cmd_skills(_skill_args(args, disable=skills)) or rc

    if hook_sel:
        # --root must govern the hook branch too: write the flags into the TARGET,
        # not this repo's settings. When --root is given, derive every apply_selection
        # path from base; otherwise keep the legacy CLAUDE_PROJECT_DIR/default paths.
        rooted = getattr(args, "root", None) is not None
        settings_path = args.settings_file or (
            str(base / ".claude" / "settings.json") if rooted
            else _default_settings_path())
        try:
            cc.apply_selection(
                hook_sel,
                policy_path=args.policy_file or (
                    str(base / "harness/data/component-policy.yaml") if rooted else None),
                settings_path=settings_path,
                hooks_path=args.hooks_file or (
                    str(base / "harness/data/harness-hooks.yaml") if rooted else None),
                state_path=args.state_file or (
                    str(base / "harness/state/install-state.json") if rooted else None))
        except cc.ComponentConfigError as e:
            print("error: %s" % e, file=sys.stderr)
            return 2
        print("components updated: " + ", ".join(
            "%s=%s" % (k, "on" if v else "off") for k, v in sorted(hook_sel.items())))
    return rc


def _enabled_plugins() -> dict:
    merged: dict = {}
    base = Path(os.environ.get("CLAUDE_PROJECT_DIR") or _ROOT) / ".claude"
    for fn in ("settings.json", "settings.local.json"):
        try:
            d = json.loads((base / fn).read_text(encoding="utf-8"))
            ep = d.get("enabledPlugins")
            if isinstance(ep, dict):
                merged.update(ep)
        except Exception:  # noqa: BLE001 — absent/invalid settings
            continue
    return merged


def _plugin_on(plugin: str, enabled: dict) -> bool:
    if plugin == "hs":
        return True  # spine is always on
    for key, val in enabled.items():
        if str(key).split("@", 1)[0] == plugin:
            return bool(val)
    return False


def cmd_list(args) -> int:
    plugins_dir = _ROOT / "harness/plugins"
    enabled = _enabled_plugins()
    rows = []
    for pdir in sorted(plugins_dir.iterdir()):
        sdir = pdir / "skills"
        if not sdir.is_dir():
            continue
        # a skill dir carries SKILL.md; a bare resource dir (e.g. `common`, shared
        # ai-group helpers) is not a skill — match the 96-invokable count the
        # selection + STANDARDIZE enumerators use.
        skills = sorted(p.name for p in sdir.iterdir()
                        if p.is_dir() and (p / "SKILL.md").is_file())
        if not skills:
            continue
        state = "spine" if pdir.name == "hs" else ("on" if _plugin_on(pdir.name, enabled) else "off")
        rows.append((pdir.name, state, skills))
    width = max((len(n) for n, _, _ in rows), default=2)
    for name, state, skills in rows:
        print("%-*s  [%-5s]  %2d  %s" % (width, name, state, len(skills), ", ".join(skills)))
    return 0


def _skills_paths(args, base=None):
    # `base` is passed in by a caller that already decided read-vs-write (cmd_skills);
    # defaulting to the read root keeps every other caller unchanged.
    base = base if base is not None else _catalog_root(args)
    return (base / "harness/plugins/hs/skills",
            base / "harness/plugins/hs/disabled-skills",
            base / "harness/state/install-omitted-skills.json",
            base / "harness/data/skill-deps.yaml")


def _dependents(skill, deps_path, among) -> set:
    """Skills in `among` that auto-tick `skill` (declare it as a dep)."""
    try:
        import skill_deps
        graph = skill_deps.load_deps(deps_path)["skills"]
    except Exception:  # noqa: BLE001 — no graph -> no known dependents
        return set()
    return {s for s in among if skill in (graph.get(s, {}).get("deps") or [])}


def _split_csv(v):
    """A comma-joined --off/--on value (repeatable) -> flat, de-duped, ordered list."""
    out = []
    for chunk in (v or []):
        for name in chunk.split(","):
            name = name.strip()
            if name and name not in out:
                out.append(name)
    return out


def cmd_skills(args) -> int:
    """Enable/disable individual skills for the collapsed hs plugin (dir-omit).

    --disable stashes the skill dir under harness/plugins/hs/disabled-skills (a
    TRACKED sibling of skills/, so the off skill ships with the bundle and toggles
    without a reinstall; the loader only scans skills/ so it stays hidden) and records
    it in install-omitted-skills.json (the verify_install seam keeps the absence from
    reading as drift). The move is a git rename in the source repo, not a deletion, so
    disabling in-place is safe. --enable restores the dir and auto-restores its deps.
    The spine core is refused. Bare lists on/off."""
    import shutil
    import skill_deps
    # A bare `hs-cli skills` only LISTS; the four toggle flags move directories on
    # disk. Resolve the root through the refusing variant only when this invocation
    # is actually going to write, so listing keeps working under a global install
    # (which is the bug that started this) while a toggle there is refused by name.
    _mutates = any(getattr(args, f, None)
                   for f in ("enable", "disable", "off", "on"))
    _base = _mutable_catalog_root(args) if _mutates else _catalog_root(args)
    skills_dir, stash_dir, omit_path, deps_path = _skills_paths(args, base=_base)

    def _present():
        return ({d.name for d in skills_dir.iterdir() if (d / "SKILL.md").is_file()}
                if skills_dir.is_dir() else set())

    import omit_record

    def _load_omit():
        return omit_record.read_omitted(_base)

    def _save_omit(s):
        omit_record.write_omitted(_base, s)

    # --off/--on <csv>: batch toggle, context-aware. A dev loading the plugin from
    # the repo directory curates via a symlink farm (.harness-dev/dev-off-skills.yaml
    # + dev_skill_farm) so the repo stays full; an installed copy toggles in-tree
    # (the --disable/--enable dir-omit below). Either way a RESTART applies it.
    off_csv = _split_csv(getattr(args, "off", None))
    on_csv = _split_csv(getattr(args, "on", None))
    if off_csv or on_csv:
        import dev_skill_farm as dsf
        if (_base / dsf._OFFLIST_REL).is_file():   # dev symlink-farm setup
            bad = dsf.validate_off(_base, off_csv)
            if bad:
                print("error: " + "; ".join(bad), file=sys.stderr)
                # An unknown name is a usage problem (2); a floor skill is a
                # known-valid name declined on purpose (3). Usage wins when both fire.
                if any(b.kind == "unknown" for b in bad):
                    return 2
                return 3
            dsf.toggle_record(_base, add=off_csv, remove=on_csv)
            dsf.build_farm(_base, _base / dsf._DEFAULT_FARM_REL, dsf.load_off_list(_base))
            if off_csv:
                print("off (dev farm): %s" % ", ".join(off_csv))
            if on_csv:
                print("on (dev farm): %s" % ", ".join(on_csv))
            print("→ restart Claude Code to apply (the plugin reloads on session start)")
            return 0
        # installed copy: fold into the in-tree dir-omit path below
        args.disable = (args.disable or []) + off_csv
        args.enable = (args.enable or []) + on_csv

    # --enable/--disable are repeatable, but the onboarding protocol documents a CSV
    # (`skills --enable <csv-of-cluster-skills>`); split each element so both the CSV
    # and the repeated-flag forms work (a CSV used to collapse into one bogus name).
    args.enable = _split_csv(getattr(args, "enable", None))
    args.disable = _split_csv(getattr(args, "disable", None))

    if args.disable:
        try:
            core = set(skill_deps.core_immutable(deps_path))
        except Exception as e:  # noqa: BLE001 — an unreadable floor must BLOCK a
            # disable, not silently empty the core-immutable set: a typo'd/missing
            # skill-deps.yaml must never let a spine skill through.
            print("error: cannot load %s (%s) — refusing to disable anything "
                  "until the skill dependency graph is readable" % (deps_path, e),
                  file=sys.stderr)
            return 2
        for s in args.disable:
            if s in core:
                print("error: %r is a spine core skill — never disabled" % s,
                      file=sys.stderr)
                return 3  # a known-valid name declined on purpose
        omit, present = _load_omit(), _present()
        still_on = present - set(args.disable)
        # Two DIFFERENT problem shapes used to share one `problems` list and one
        # exit code. `usage_problems` names something wrong with the REQUEST
        # itself (a typo'd/unknown skill name) — exit 2. `refused_problems`
        # names a valid request declined on purpose (nesting into a stale
        # stash would corrupt the re-enable round-trip) — exit 3. When a single
        # invocation hits both, usage wins: part of the input itself was
        # invalid, which is a different (and prior) failure than "your
        # otherwise-valid request is being declined".
        moved, usage_problems, refused_problems = [], [], []
        for s in args.disable:
            src, dst = skills_dir / s, stash_dir / s
            if not src.is_dir():
                if s in omit:
                    print("skills: %r already disabled" % s, file=sys.stderr)
                else:
                    print("skills: %r is not an installed skill" % s, file=sys.stderr)
                    usage_problems.append(s)
                continue
            if dst.exists():
                # never shutil.move into an existing stash dir — it NESTS the skill
                # (disabled-skills/<s>/<s>/) and corrupts the re-enable round-trip.
                print("error: stash already holds %r — refusing to nest; run "
                      "`hs-cli skills --enable %s` first" % (s, s), file=sys.stderr)
                refused_problems.append(s)
                continue
            dependents = _dependents(s, deps_path, still_on)
            if dependents:
                print("warning: %r is a dep of still-enabled %s"
                      % (s, ", ".join(sorted(dependents))), file=sys.stderr)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            omit.add(s)
            # persist per-move, not only once after the loop: a crash mid-loop then
            # leaves at most the in-flight skill moved-but-unrecorded, instead of
            # every already-moved skill — so verify --strict cannot report broad
            # integrity drift for a skill the user merely disabled.
            _save_omit(omit)
            moved.append(s)
        _save_omit(omit)  # final write: also creates the record when nothing moved
        if moved:
            print("skills disabled: %s" % ", ".join(moved))
            print("→ restart Claude Code to apply (the plugin reloads on session start)")
        if usage_problems:
            return 2
        if refused_problems:
            return 3
        return 0

    if args.enable:
        try:
            targets = skill_deps.resolve(args.enable, deps_path)
        except Exception:  # noqa: BLE001
            targets = set(args.enable)
        omit = _load_omit()
        restored, missing = [], []
        for s in sorted(targets):
            src, dst = stash_dir / s, skills_dir / s
            if dst.is_dir():
                omit.discard(s)  # already present
                continue
            if src.is_dir():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
                omit.discard(s)
                restored.append(s)
            else:
                # an install-omitted skill has no stash to restore from — the dir
                # was never copied. Report the real recovery path, do NOT claim
                # success (the omit record is kept so the nudge still points here).
                missing.append(s)
                print("skills: no stashed copy of %r — re-run "
                      "`install --skills %s` to fetch it" % (s, s), file=sys.stderr)
        _save_omit(omit)
        if restored:
            print("skills enabled: %s" % ", ".join(restored))
            print("→ restart Claude Code to apply (the plugin reloads on session start)")
        return 2 if missing else 0

    # bare: list on/off state (dev-farm-aware — a skill still living in skills/ but
    # named in the dev off-list reads as off, not on; also covers stash + omit record)
    import disabled_skills
    off = disabled_skills.effective_disabled(disabled_skills.default_sources(_base))
    present = _present()
    for s in sorted(present - off):
        print("on   %s" % s)
    for s in sorted(off):
        print("off  %s" % s)
    return 0


def cmd_cleanup(args) -> int:
    """Re-run the safe orphan cleanup — the manual door for the prompt layer.

    Lists the classified buckets; --apply backs up + removes the safe layer.
    Modified files stay in the prompt layer unless explicitly promoted with
    --remove (the headless equivalent of the skill's Keep/Change)."""
    import cleanup_orphans
    base = (Path(args.root) if getattr(args, "root", None)
            else Path(os.environ.get("CLAUDE_PROJECT_DIR") or _ROOT)).resolve()
    # On a manual re-run no snapshot is passed — fall back to the durable one
    # install.sh persisted, so the deferred (modified) layer stays reachable.
    snap = args.old_manifest
    if not snap:
        persisted = base / "harness" / "state" / "cleanup-prev-manifest.json"
        if persisted.is_file():
            snap = str(persisted)
    old = cleanup_orphans._load_manifest(snap)
    plan = cleanup_orphans.plan_cleanup(base, old)

    for line in cleanup_orphans.render_plan(plan):
        print(line)

    # --dry-run is explicit intent and wins over a co-passed --apply.
    if not args.apply or args.dry_run:
        if plan["prompt"]:
            print("\n%d modified file(s) — re-run with --remove <path> to clean, or "
                  "use hs:cleanup to decide interactively" % len(plan["prompt"]))
        print("\n(dry-run — re-run with --apply to back up + remove the safe layer)")
        return 0

    # headless promotion: move named prompt-layer files into the remove layer.
    # An unmatched --remove is surfaced, never a silent no-op.
    for rel in (args.remove or []):
        if rel in plan["prompt"]:
            plan["prompt"].remove(rel)
            plan["remove"].append(rel)
        else:
            print("note: %r is not a prompt-layer path (typo, or already removed) "
                  "— nothing promoted" % rel)

    backup = (Path(args.backup_dir) if args.backup_dir
              else base / "harness" / "state" / "cleanup-backup")
    result = cleanup_orphans.apply_cleanup(plan, base, backup)
    if result["backup_dir"]:
        print("\nbacked up to %s; removed %d, unlinked %d"
              % (result["backup_dir"], len(result["removed"]), len(result["unlinked"])))
    else:
        print("\nnothing to clean up")
    return 0


def _hs_run_exit_ladder() -> frozenset:
    """hs-run's closed exit ladder, read from hs_run.py's own constants — 0
    done · 1 findings · 10 a model turn · 11 one more deterministic step ·
    2 broken · 3 a deliberate refusal.

    Imported rather than re-typed as a literal: a second copy is a second thing
    to keep in sync, and the ladder is exactly the kind of constant that gets
    widened in one place and silently not the other. The import is function-
    local (~32ms, and this CLI keeps every verb's cost off startup); it reads
    no registry — hs_run.py loads that lazily, inside its own dispatch."""
    import hs_run
    return frozenset((hs_run.EXIT_DONE, hs_run.EXIT_FINDINGS, hs_run.EXIT_NEEDS_MODEL,
                      hs_run.EXIT_STEP_REMAINING, hs_run.EXIT_BROKEN, hs_run.EXIT_REFUSED))


def cmd_render(args) -> int:
    """`hs render` — draw ONE hs-run envelope read from stdin, then stop.

    The display half of the pair: `hs-run <domain> <verb>` decides and emits
    JSON, this turns that JSON into something a human reads. Use it like this:

        hs-run security next > /tmp/e.json; rc=$?
        hs render --upstream-rc "$rc" < /tmp/e.json

    Deliberately NOT a driver: it renders and stops, running nothing.

    That two-line shape, not `hs-run ... | hs render`, is the documented one —
    and the reason is the whole point of `--upstream-rc`. A bare pipe SWALLOWS
    the verdict: the shell reports the LAST command's code, so a `security
    mechanical-sweep` exiting 2 on a leaked key reads as 0 to
    `hs-run ... | hs render && git push`, which then pushes the key.
    `set -o pipefail` recovers it and is off by default, and nothing in the
    pipe hints that it is required — so the pipe form is not shown here at all.
    An example is an instruction; showing the dangerous shape teaches it.

    Forwarded, never inferred. The envelope has no `exit_code` field, and
    deriving one from the state name would fork the conformance suite's
    `_NEVER_DONE` into shipped code — two vocabularies drifting apart the first
    time a domain adds a state.

    Forwarding is not auto-advance. Rules (a)/(b) forbid RUNNING the next step;
    this still runs nothing. Reporting the verdict the envelope already states
    is what lets a caller stop. Without the flag it returns 0 — that reports on
    the rendering, the only thing this verb can honestly speak to.

    Reads ONE line: the envelope is line 1 of hs-run's stdout by contract, and
    line 2 is the human summary this verb replaces.

    A malformed line is the operator's own pipeline mistake (usually stderr
    merged in, or a non-hs-run command upstream), so it fails loudly with the
    offending text rather than rendering an empty frame that looks like a
    clean result."""
    import hs_cli_render

    line = sys.stdin.readline()
    if not line.strip():
        print("hs render: empty stdin — expected one hs-run JSON envelope",
              file=sys.stderr)
        return 2
    try:
        envelope = json.loads(line)
    except json.JSONDecodeError as exc:
        print("hs render: stdin line 1 is not JSON (%s): %.120s" % (exc.msg, line.rstrip()),
              file=sys.stderr)
        return 2
    if not isinstance(envelope, dict):
        print("hs render: expected a JSON object, got %s" % type(envelope).__name__,
              file=sys.stderr)
        return 2
    upstream = getattr(args, "upstream_rc", None)
    if upstream is not None:
        ladder = _hs_run_exit_ladder()
        if upstream not in ladder:
            print("hs render: --upstream-rc %d is outside hs-run's closed exit ladder %s "
                  "— that pipeline did not come from hs-run"
                  % (upstream, sorted(ladder)), file=sys.stderr)
            return 2

    hs_cli_render.render_envelope(envelope)
    return 0 if upstream is None else upstream


def cmd_install(rest: list[str]) -> int:
    argv = [sys.executable, str(_ROOT / "harness/install/install.py")]
    return _run(argv + (rest or []))


@_attrs.define(frozen=True, kw_only=True)
class DocsPage:
    """What `hs-cli docs` answers: the page (or where it went) and the registry it read."""
    cli: str = _attrs.field()
    registry_sha256: str = _attrs.field()
    commands: int = _attrs.field()
    path: Optional[str] = _attrs.field(default=None)
    markdown: Optional[str] = _attrs.field(default=None)

    def as_dict(self) -> dict:
        return {k: v for k, v in _wire(self).items() if v is not None}


@_attrs.define(frozen=True, kw_only=True)
class StatusBlock:
    """One row of the status book, run: its stdout whole, and its stderr (a failing
    command's reason)."""
    name: str = _attrs.field()
    argv: tuple = _attrs.field(converter=_as_tuple)
    exit_code: int = _attrs.field()
    output: str = _attrs.field()
    stderr: str = _attrs.field()


def cmd_docs(args) -> Outcome:
    """One family CLI's command reference, generated from its registry now (never by
    a hook or CI step): every live command, its help line and its parser's usage,
    headed by the sha256 of the registry files it was read from."""
    from hs_family import docs
    text, digest, count = docs.reference(docs.family_cli(args.cli, _ROOT))
    if args.out:
        page = DocsPage(cli=args.cli, registry_sha256=digest, commands=count,
                        path=str(docs.write(text, Path(args.out))))
        return Outcome(exit_code=0, state="done", data=page.as_dict(),
                       text="wrote %s (%d commands, registry sha256 %s)" % (args.out, count, digest))
    page = DocsPage(cli=args.cli, registry_sha256=digest, commands=count, markdown=text)
    return Outcome(exit_code=0, state="done", data=page.as_dict(), text=text)


# `hs-cli status` composes: each row of the book is one family command, run as its own
# process; its block is what it printed. Nothing here reads a ledger, a lane or a gate.
_STATUS_BOOK = _SCRIPTS.parent / "data" / "hs-cli-status.yaml"
_FAMILY_BIN = _SCRIPTS.parent / "bin"
_STATUS_KEYS = frozenset({"name", "argv"})
#: broken > refused > findings > clean; any other exit is a child that broke
_WORST_FIRST = (2, 3, 1, 0)


def _status_rows(path: Path) -> list:
    try:
        doc = yaml_io.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml_io.YAMLError) as e:
        raise hs_family.UsageError("%s: unreadable: %s" % (path, e)) from e
    if not isinstance(doc, dict) or doc.get("schema_version") != 1:
        raise hs_family.UsageError("%s: needs `schema_version: 1` and `blocks:`" % path)
    rows, names = doc.get("blocks"), set()
    if not isinstance(rows, list) or not rows:
        raise hs_family.UsageError("%s: `blocks:` has no row: status would compose nothing" % path)
    for i, row in enumerate(rows, 1):
        at = "%s: blocks[%d]" % (path, i)
        if not isinstance(row, dict) or set(row) - _STATUS_KEYS or not isinstance(row.get("name"), str):
            raise hs_family.UsageError("%s: a row is {name, argv}; got %s" % (at, json.dumps(row)))
        argv = row.get("argv")
        if not (isinstance(argv, list) and argv and all(isinstance(a, str) and a for a in argv)):
            raise hs_family.UsageError("%s (%s): `argv:` must be a non-empty list of strings"
                                       % (at, row["name"]))
        if argv[0] not in hs_registry.FAMILY:
            raise hs_family.UsageError("%s (%s): %r is not a family CLI (%s)"
                                       % (at, row["name"], argv[0], ", ".join(hs_registry.FAMILY)))
        if row["name"] in names:
            raise hs_family.UsageError("%s: block %r named twice" % (at, row["name"]))
        names.add(row["name"])
    return rows


def _status_block(row: dict) -> StatusBlock:
    exe = _FAMILY_BIN / (row["argv"][0] + (".cmd" if os.name == "nt" else ""))
    try:
        r = process_group.run([str(exe), *row["argv"][1:]], capture_output=True, text=True,
                              timeout=900)
        code, out, err = r.returncode, r.stdout, r.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        code, out, err = 2, "", "%s: %s\n" % (type(e).__name__, e)
    return StatusBlock(name=row["name"], argv=row["argv"], exit_code=code, output=out, stderr=err)


def _worst(codes) -> int:
    codes = {c if c in _WORST_FIRST else 2 for c in codes}
    return next((c for c in _WORST_FIRST if c in codes), 0)


def cmd_status(args) -> Outcome:
    """Every row of `harness/data/hs-cli-status.yaml`, run alone, one block each: what
    it printed, and — the reason a failing command gives there — what it wrote to
    stderr. The exit is the worst block's (2 > 3 > 1 > 0)."""
    blocks = [_status_block(row) for row in _status_rows(_STATUS_BOOK)]
    code = _worst(b.exit_code for b in blocks)
    text = "\n".join("== %s (exit %d): %s ==\n%s%s" % (
        b.name, b.exit_code, " ".join(b.argv), b.output.rstrip("\n"),
        "\n" + b.stderr.rstrip("\n") if b.exit_code and b.stderr.strip() else "")
        for b in blocks)
    return Outcome(exit_code=code, state=state_of(code), data={"blocks": [_wire(b) for b in blocks]},
                   text=text)


# ----------------------------------------------------------------------- cli

def _no_args(p: argparse.ArgumentParser) -> None:
    return None


def _args_gates(gt: argparse.ArgumentParser) -> None:
    gt.add_argument("--off", action="append", default=[],
                    help="csv of gate names to turn off for this project (needs --reason)")
    gt.add_argument("--on", action="append", default=[],
                    help="csv of gate names to turn back on for this project")
    gt.add_argument("--force", action="store_true",
                    help="allow switching a core (fail-closed) hook off")
    gt.add_argument("--reason", help="why — recorded with who and when")
    gt.add_argument("--whole-tree", action="store_true",
                    help="list the gates a change anywhere can redden, one `<gate> <file>` per line")


def _args_hooks(hk: argparse.ArgumentParser) -> None:
    hk.add_argument("--off", action="append", default=[],
                    help="csv of hook names to turn off for this project (repeatable)")
    hk.add_argument("--on", action="append", default=[],
                    help="csv of hook names to turn on for this project (repeatable)")
    hk.add_argument("--force", action="store_true",
                    help="allow toggling a fail-closed safety gate or a guard-policy name")
    hk.add_argument("--reason",
                    help="why, written as force_reason beside a forced --off")


def _args_emit(em: argparse.ArgumentParser) -> None:
    em.add_argument("--target", required=True, help="a target id from runtime-targets.yaml")
    em.add_argument("--json", action="store_true", help="emit the projection as JSON")
    em.add_argument("--ledger", metavar="PATH", help="write the per-decision JSONL ledger here")
    em.add_argument("--out", metavar="DIR", help="write a runnable install for the target here "
                    "(hooks file + degradation ledger + operator notes)")
    em.add_argument("--project-root", metavar="PATH", help="absolute path the emitted hook "
                    "commands address the dispatcher by (default: this harness root)")
    em.add_argument("--skills", metavar="CSV", help="skill directories to ship with the "
                    "install (default: every live skill). `--skills=` ships none.")
    em.add_argument("--agents", metavar="CSV", help="agents to emit as target roles "
                    "(default: every agent on disk). `--agents=` emits none.")
    em.add_argument("--allow-inert-gates", action="store_true",
                    help="exit 0 even when a compliance gate would go inert on the "
                         "target; the refusal is still recorded")


def _args_render(rd: argparse.ArgumentParser) -> None:
    rd.add_argument("--upstream-rc", type=int, default=None, metavar="CODE",
                    help="hs-run's own exit code, so a pipe does not swallow a "
                         "blocking verdict (a bare pipe reports only this verb's "
                         "code; `set -o pipefail` is off by default)")


def _args_components(c: argparse.ArgumentParser) -> None:
    c.add_argument("--enable", action="append", metavar="GROUP")
    c.add_argument("--disable", action="append", metavar="GROUP")
    c.add_argument("--policy-file", default=None)
    c.add_argument("--settings-file", default=None)
    c.add_argument("--hooks-file", default=None)
    c.add_argument("--state-file", default=None)
    c.add_argument("--root", default=None,
                   help="target repo root (default: CLAUDE_PROJECT_DIR or this repo)")
    c.add_argument("--force", action="store_true",
                   help="for a label group: disable even when skill dirs are git-tracked")


def _args_skills(sk: argparse.ArgumentParser) -> None:
    sk.add_argument("--enable", action="append", metavar="SKILL",
                    help="restore a skill (and its deps) from the disabled stash")
    sk.add_argument("--disable", action="append", metavar="SKILL",
                    help="stash a skill dir + record the omission (core is refused)")
    sk.add_argument("--off", action="append", metavar="A,B,C",
                    help="turn skills OFF (comma-list, repeatable) — dev-farm setups "
                         "edit the off-list + rebuild the farm, installs dir-omit; restart applies")
    sk.add_argument("--on", action="append", metavar="A,B,C",
                    help="turn skills back ON (comma-list, repeatable); restart applies")
    sk.add_argument("--root", default=None,
                    help="target repo root (default: CLAUDE_PROJECT_DIR or this repo)")


def _args_cleanup(cl: argparse.ArgumentParser) -> None:
    cl.add_argument("--root", default=None,
                    help="target repo root (default: CLAUDE_PROJECT_DIR or this repo)")
    cl.add_argument("--old-manifest",
                    help="pre-overwrite manifest snapshot (omit on a manual re-run)")
    cl.add_argument("--backup-dir", default=None,
                    help="backup root (default: <root>/harness/state/cleanup-backup)")
    cl.add_argument("--remove", action="append", metavar="PATH",
                    help="headless: promote a modified (prompt-layer) file to removal")
    cl.add_argument("--apply", action="store_true",
                    help="write (default: dry-run plan)")
    cl.add_argument("--dry-run", action="store_true",
                    help="explicit dry-run (the default when --apply is absent)")


def _args_docs(dc: argparse.ArgumentParser) -> None:
    dc.add_argument("cli", choices=hs_registry.FAMILY, help="the family CLI to document")
    dc.add_argument("--out", metavar="FILE", help="write the page here instead of answering it")


def _args_trust(tr: argparse.ArgumentParser) -> None:
    tr.add_argument("repo", nargs="?", default=".",
                    help="repo root to trust (default: current dir)")
    tr.add_argument("--list", action="store_true", help="list trusted repo roots")


def _run_migrate(ns) -> int:
    import migrate_decomposition as md
    return md.main(ns.args)


# One `Command` per `hs-cli` command; `harness/data/hs-cli-registry.d/core.yaml` names each
# with its help line. `migrate`/`install` hand their argv to the wrapped tool untouched.
VERSION = Command(add_args=_no_args, run=cmd_version)
DOCTOR = Command(add_args=_no_args, run=cmd_doctor)
GATES = Command(add_args=_args_gates, run=cmd_gates)
GUARDS = Command(add_args=_no_args, run=cmd_guards)
HOOKS = Command(add_args=_args_hooks, run=cmd_hooks)
CAPABILITIES = Command(add_args=_no_args, run=cmd_capabilities)
LIST = Command(add_args=_no_args, run=cmd_list)
EMIT = Command(add_args=_args_emit, run=cmd_emit)
RENDER = Command(add_args=_args_render, run=cmd_render)
MIGRATE = Command(add_args=_no_args, run=_run_migrate, passthrough=True)
COMPONENTS = Command(add_args=_args_components, run=cmd_components)
SKILLS = Command(add_args=_args_skills, run=cmd_skills)
CLEANUP = Command(add_args=_args_cleanup, run=cmd_cleanup)
INSTALL = Command(add_args=_no_args, run=lambda ns: cmd_install(ns.args), passthrough=True)
TRUST = Command(add_args=_args_trust, run=cmd_trust)
DOCS = Command(add_args=_args_docs, run=cmd_docs)
STATUS = Command(add_args=_no_args, run=cmd_status)


class HsCli(CliBase):
    """`doctor` reads hs-cli's own registry health itself and reports a broken moved row
    as a finding (exit 1): the table is built without the moved-row check, `doctor` is
    never refused as moved, and every OTHER command reads the registry strictly before
    it is routed (exit 2 naming the broken row)."""
    prog, depth = "hs-cli", 1
    description = "harness operator CLI"

    def sources(self, flags) -> list:
        return [RegistrySource(self.registry_env(), self.install_root, depth=1, check_moved=False)]

    def _refuse_if_moved(self, argv, table) -> None:
        if argv[:1] == ["doctor"]:
            return
        try:
            hs_registry.load_registry(self.registry_env(), self.install_root)
        except hs_registry.RegistryError as e:
            raise hs_family.UsageError(str(e)) from e
        super()._refuse_if_moved(argv, table)


def main(argv=None) -> int:
    return HsCli(install_root=_ROOT).main(argv)


if __name__ == "__main__":
    sys.exit(main())
