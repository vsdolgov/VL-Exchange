"""Local pilot paths; never install the private SDK or change global Python."""
from pathlib import Path
import sys

sys.dont_write_bytecode = True
PILOT = Path(__file__).resolve().parents[1]
WORKSPACE = PILOT.parents[2]
for relative in (
    "drafts/v0.2/scenario/src",
    "drafts/v0.2/videopipe-pilot/python",
    "drafts/v0.2/model/src",
    "model/src",
    "drafts/v0.2/security-runtime/src",
):
    sys.path.insert(0, str(WORKSPACE / relative))
