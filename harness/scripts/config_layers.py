#!/usr/bin/env python3
"""config_layers.py — the one door every layered harness/data/*.yaml is read through.

A reader that builds `root/"harness"/"data"/<name>` itself reads the SHIPPED file, so a
deployment that set `HARNESS_<NAME>` is ignored in silence. That is D1/D2/D5, and it is
silent twice over: the values coincide today, so nothing misbehaves and no assertion
about the live tree can go red.

TWO NAMED DOORS, deliberately not merged — the shape `output_config` already uses, where
`load_output` (fail-closed, gate path) sits beside `resolve_all` (fail-open, hook path):

  read_strict  a GATE reads this. A layer naming a file that is not there raises, because
               a gate that fell back quietly would enforce a policy nobody chose.
  read_soft    a HOOK reads this. Never raises; falls back to the next layer and RECORDS
               the reason. Fail-open is not fail-silent.

`resolve_path` returns (path, layer) so a report can say WHY a value is what it is.

The layer stack, highest first, is `LAYERS`. `project` is opt-in per file in
harness/data/config-layers.yaml: a cage-relevant config does not move into the writeable
project zone, which is what the localized-config ruling refused.
"""
import os
from pathlib import Path

LAYERS = ("explicit", "env", "project", "ship")

_REGISTRY_NAME = "config-layers.yaml"


class ConfigLayerError(Exception):
    """A gate asked for a config and the answer would have been a guess."""


def _yaml_load(text):
    import yaml_io
    return yaml_io.yaml_load(text)


def _paths():
    import harness_paths
    return harness_paths


def _ship_dir(root=None):
    """Where the shipped configs live.

    `root` is an explicit tree to answer about — what a caller with a `--root` flag (or a
    test with a scratch checkout) means. Honouring it is not optional: a resolver that
    silently answers about ITS OWN tree while the caller asked about another one reports
    confidently about the wrong project.

    Without `root`, the shipped configs sit beside THIS MODULE — the same placement the
    registry uses, and for the same reason: they travel with the code that reads them.
    Under a shared-bin install this file IS in the bin, so `__file__` and the bin agree
    without consulting the environment.

    Deliberately NOT `bin_root()`. That reads HARNESS_ROOT/HARNESS_BIN_ROOT, which tests
    and tools set to move the WRITEABLE zone; routing the shipped config through it made
    a test that redirected `plans/` also redirect the frozen grid tables, and the loader
    failed closed on a file that was never meant to move (measured: grid-axes.yaml
    unreadable under a tmp root)."""
    if root is not None:
        return Path(root) / "harness" / "data"
    return Path(__file__).resolve().parent.parent / "data"


def _project_dir(root=None):
    """This project's writeable data home, or None when it cannot be resolved.

    None rather than a guess: harness_paths fails CLOSED under a global layout that
    cannot name a project, and inventing a directory here would put a project layer
    somewhere nobody chose."""
    if root is not None:
        # An explicit tree carries its own project home; `.harness/` under it is the
        # same convention harness_paths uses, and inventing a different one here would
        # put the layer somewhere the caller cannot see.
        d = Path(root) / ".harness"
        return d if d.is_dir() else None
    try:
        d = Path(_paths().data_root())
    except Exception:  # noqa: BLE001
        return None
    if not d or "unresolved" in str(d):
        return None
    return d


_registry_cache = None


def registry(force=False) -> dict:
    """The declared layer stack per file. Parsed once per process.

    Read from beside THIS MODULE, never from a caller's `root` and never through
    `bin_root()`: the registry is the product's statement about which configs answer to
    which layers, so it travels with the code that reads it. Both other placements were
    measured wrong — per-root made every scratch checkout look like a harness with no
    declared configs (the sentinel CLI exited 1 on a tmp root), and bin_root() let a test
    that points HARNESS_BIN_ROOT at a fixture take the registry away from every gate at
    once, which fails CLOSED: five gate tests died with "gate crashed" rather than
    reporting anything about their own subject. This mirrors output_config's
    `_OUTPUT_DEFAULT`, resolved off __file__ for the same reason."""
    global _registry_cache
    if _registry_cache is not None and not force:
        return _registry_cache
    p = Path(__file__).resolve().parent.parent / "data" / _REGISTRY_NAME
    try:
        doc = _yaml_load(p.read_text(encoding="utf-8"))
    except ImportError:
        # A missing YAML dependency is NOT a config-layer problem, and the difference
        # is load-bearing: the compliance wrapper answers an ImportError with the
        # install command and everything else with "gate crashed". Wrapping this one
        # turned "run preflight_deps" into an opaque crash on a machine that skipped it.
        raise
    except Exception as e:  # noqa: BLE001
        raise ConfigLayerError("cannot read the config-layer registry at %s: %s" % (p, e))
    if not isinstance(doc, dict):
        raise ConfigLayerError("%s is not a mapping" % p)
    out: "dict[str, dict]" = {"configs": {}, "exempt": {}}
    for row in doc.get("configs") or []:
        out["configs"][row["file"]] = row
    for row in doc.get("exempt") or []:
        out["exempt"][row["file"]] = row
    _registry_cache = out
    return out


def _entry(name: str, root=None) -> dict:
    reg = registry()
    if name in reg["configs"]:
        return reg["configs"][name]
    if name in reg["exempt"]:
        raise ConfigLayerError(
            "%s is declared EXEMPT from the layered door (%s) — read it through its own "
            "reader" % (name, (reg["exempt"][name].get("why") or "").strip()[:120]))
    raise ConfigLayerError(
        "%s is not declared in %s — a config with no declared layer stack would get one "
        "invented for it here" % (name, _REGISTRY_NAME))


def candidates(name: str, *, explicit=None, root=None):
    """[(layer, path)] highest-first for the layers this file answers to.

    A layer whose home cannot be resolved is OMITTED rather than pointed at a guess; a
    layer whose file simply does not exist is KEPT, because 'you named a file that is not
    there' and 'this layer does not apply here' are different answers and only the first
    one is a mistake worth reporting."""
    entry = _entry(name, root=root)
    out = []
    if explicit is not None:
        out.append(("explicit", Path(explicit)))
    env_var = entry.get("env")
    if env_var:
        raw = os.environ.get(env_var)
        if raw:
            out.append(("env", Path(raw)))
    if entry.get("project"):
        pd = _project_dir(root)
        if pd is not None:
            out.append(("project", pd / name))
    out.append(("ship", _ship_dir(root) / name))
    return out


def resolve_path(name: str, *, explicit=None, root=None):
    """(path, layer) of the highest layer that HAS the file.

    A layer that NAMES a file — `explicit` or `env` — is answered with that path even
    when it is not there, and the caller's own read then fails loudly naming it. Falling
    through to the shipped file instead is the exact silent shape this module exists to
    kill: the operator set HARNESS_<NAME>, the reader quietly used the default, and the
    values agreed so nothing said a word. `read_soft` is where a fall-through belongs,
    because it costs a diagnostic line.

    Only `project` and `ship` fall through, and that is not a mistake: those layers are
    derived, so an absent file means the layer does not apply here."""
    cands = candidates(name, explicit=explicit, root=root)
    for layer, p in cands:
        if layer in ("explicit", "env"):
            return p, layer
        try:
            if p.is_file():
                return p, layer
        except OSError:
            continue
    return cands[-1][1], cands[-1][0]


def _named_layer_problem(name, cands, root=None):
    """The one diagnosis both doors share: a layer that NAMES a file which is not there.

    Only `explicit` and `env` can be wrong this way. A missing project or ship file means
    the layer does not apply here, which is ordinary."""
    for layer, p in cands:
        if layer not in ("explicit", "env"):
            continue
        try:
            if p.is_file():
                return None
        except OSError:
            pass
        if layer == "env":
            var = _entry(name).get("env")
            return "%s=%s names a file that is not there" % (var, p)
        return "the explicit path %s is not there" % p
    return None


def layer_problem(name: str, *, explicit=None, root=None):
    """The shared diagnosis, for a caller that resolves a PATH and reads it itself.

    Returns the reason string when a layer that NAMES a file points at nothing, else
    None. `resolve_path` deliberately falls through such a layer — that is right for a
    reader with a usable default, and wrong for a fail-LOUD one, which would otherwise
    read the shipped file while the operator believes their override is in force."""
    return _named_layer_problem(name, candidates(name, explicit=explicit, root=root),
                                root=root)


def read_strict(name: str, *, explicit=None, root=None) -> dict:
    """The GATE door. Raises ConfigLayerError rather than answer with a guess."""
    cands = candidates(name, explicit=explicit, root=root)
    problem = _named_layer_problem(name, cands, root=root)
    if problem:
        raise ConfigLayerError("%s: %s" % (name, problem))
    p, layer = resolve_path(name, explicit=explicit, root=root)
    try:
        doc = _yaml_load(p.read_text(encoding="utf-8"))
    except ImportError:
        raise  # a missing dependency travels as itself — see registry()
    except Exception as e:  # noqa: BLE001
        raise ConfigLayerError("%s: cannot read the %s layer at %s: %s"
                               % (name, layer, p, e))
    if doc is None:
        return {}
    if not isinstance(doc, dict):
        raise ConfigLayerError("%s: the %s layer at %s is not a mapping" % (name, layer, p))
    return doc


def read_soft(name: str, *, explicit=None, root=None):
    """The HOOK door: `(document, diagnostics)`, and it never raises.

    Every fallback it takes is named in `diagnostics`. An empty document with an empty
    diagnostic list would be the fail-SILENT shape this repo rules out, so a fallback
    always costs a line."""
    doc, diag, _source = read_soft_layer(name, explicit=explicit, root=root)
    return doc, diag


def read_soft_layer(name: str, *, explicit=None, root=None):
    """`read_soft` plus the `(layer, path)` that answered, or None when no layer did.

    A reader that reports WHERE its setting came from asks this rather than re-walking
    the layers, so the layer it names is the one it actually read. The env layer is
    named with its variable (`env:HARNESS_<NAME>`), which is what an operator unsets."""
    diag = []
    try:
        cands = candidates(name, explicit=explicit, root=root)
    except ConfigLayerError as e:
        return {}, [str(e)], None
    problem = _named_layer_problem(name, cands, root=root)
    if problem:
        diag.append("%s: %s — falling through to the next layer" % (name, problem))
    for layer, p in cands:
        try:
            if not p.is_file():
                continue
            doc = _yaml_load(p.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            diag.append("%s: the %s layer at %s is unreadable: %s" % (name, layer, p, e))
            continue
        label = "env:%s" % _entry(name, root=root).get("env") if layer == "env" else layer
        if doc is None:
            return {}, diag, (label, p)
        if not isinstance(doc, dict):
            diag.append("%s: the %s layer at %s is not a mapping" % (name, layer, p))
            continue
        return doc, diag, (label, p)
    diag.append("%s: no layer resolved to a readable file" % name)
    return {}, diag, None


def _reset_cache():
    """Test seam: drop the parsed registry so a fresh file is re-read."""
    global _registry_cache
    _registry_cache = None
