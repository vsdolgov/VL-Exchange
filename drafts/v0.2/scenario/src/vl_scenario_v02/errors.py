"""Stable error envelopes for the draft VL Scenario Package contract."""

from __future__ import annotations


class ScenarioError(Exception):
    def __init__(self, code: str, message: str, *, stage: str | None = None,
                 fatal: bool = False, retryable: bool = False):
        super().__init__(message)
        self.code, self.message = code, message
        self.stage = stage or STAGES.get(code, "execution")
        self.fatal, self.retryable = fatal, retryable

    def to_dict(self) -> dict:
        return {"stage": self.stage, "code": self.code, "message": self.message,
                "fatal": self.fatal, "retryable": self.retryable}


STAGES = {
    "manifest_invalid": "manifest", "version_unsupported": "manifest",
    "capability_missing": "requirements", "dependency_missing": "requirements",
    "dependency_mismatch": "requirements", "model_link_mismatch": "requirements",
    "path_unsafe": "integrity", "file_missing": "integrity", "digest_mismatch": "integrity",
    "inventory_mismatch": "integrity", "parameter_invalid": "parameters",
    "input_contract": "input", "input_order": "input", "input_mutation": "input", "event_contract": "output",
    "state_contract": "state", "process_timeout": "execution", "scenario_exception": "execution",
    "lifecycle": "lifecycle", "golden_mismatch": "golden", "policy_rejected": "security",
    "authorization_failed": "security", "service_failed": "execution",
    "service_contract": "execution", "hardware_rejected": "requirements",
}

EXIT_CODES = {"manifest": 2, "requirements": 3, "integrity": 4, "parameters": 5,
              "input": 5, "output": 5, "state": 5, "execution": 6,
              "lifecycle": 6, "golden": 7, "security": 8}
