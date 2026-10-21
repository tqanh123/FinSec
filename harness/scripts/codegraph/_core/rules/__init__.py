"""codegraph._core.rules — declared-rule engine (importing this registers
built-in kinds)."""
from .base import (RuleCheck, RuleContext, RuleResult, RulesEngine,
                   RulesError, SEVERITIES, Violation, kind_names,
                   register_kind, require_params)

# Built-in kinds register on import; a domain adds its own by importing
# codegraph._core.rules.base and calling register_kind — never by editing
# this file.
from .kinds import layer_direction as _layer_direction  # noqa: F401,E402
from .kinds import threshold as _threshold              # noqa: F401,E402
from .kinds import single_door as _single_door          # noqa: F401,E402
from .kinds import dup_symbol as _dup_symbol            # noqa: F401,E402
from .kinds import same_tier_edge as _same_tier_edge    # noqa: F401,E402
from .kinds import code_shape as _code_shape            # noqa: F401,E402
from .kinds import forbidden_edge as _forbidden_edge    # noqa: F401,E402
from .kinds import file_size as _file_size              # noqa: F401,E402
from .kinds import map as _map                          # noqa: F401,E402
from .kinds import ledger as _ledger                     # noqa: F401,E402
from .kinds import pkg_door as _pkg_door                 # noqa: F401,E402
from .kinds import packaging as _packaging               # noqa: F401,E402
from .kinds import landfall as _landfall                 # noqa: F401,E402
from .kinds import stale_rename as _stale_rename         # noqa: F401,E402

__all__ = ["RuleContext", "RuleCheck", "RuleResult", "RulesEngine", "RulesError",
           "SEVERITIES", "Violation", "kind_names", "register_kind", "require_params"]
