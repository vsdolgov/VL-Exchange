"""VL Scenario Package: neutral Python scenario validation and execution.

External delivery uses the standalone VL Exchange security library. Plaintext
execution is an explicit development/test mode. No block/editor API is required.
"""

__version__ = "0.1.0"

from .errors import ScenarioError
from .contracts import event_id
from .executor import ScenarioExecutor
from .golden import check_golden, run_steps
from .validation import ValidatedPackage, validate_package

__all__ = ["ScenarioError", "ScenarioExecutor", "ValidatedPackage", "event_id", "check_golden", "run_steps", "validate_package"]
