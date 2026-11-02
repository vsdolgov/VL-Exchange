"""Narrow experimental CPU ONNX detection profile; not a Box 5 loader."""
from .runtime import ModelError, load_model

__all__ = ["ModelError", "load_model"]
