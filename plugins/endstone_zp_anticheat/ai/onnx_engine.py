"""Optional local ONNX Runtime inference. Review-only by design."""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any

FEATURE_ORDER = (
    "move_speed_mean", "move_speed_peak", "combat_rate",
    "target_diversity", "ore_ratio", "click_cv", "scaffold_rate",
)


class OnnxBehaviorModel:
    def __init__(self, model_path: str | Path, enabled: bool = True) -> None:
        self.model_path = Path(model_path)
        self.enabled = bool(enabled)
        self.session: Any = None
        self.input_name: str | None = None
        self.output_name: str | None = None
        self.status = "disabled" if not enabled else "not_loaded"
        if self.enabled:
            self.load()

    def load(self) -> bool:
        if not self.enabled:
            self.status = "disabled"
            return False
        if not self.model_path.is_file():
            self.status = "model_missing"
            return False
        try:
            import onnxruntime as ort  # optional dependency
            options = ort.SessionOptions()
            options.intra_op_num_threads = 1
            options.inter_op_num_threads = 1
            self.session = ort.InferenceSession(
                str(self.model_path), sess_options=options,
                providers=["CPUExecutionProvider"],
            )
            inputs = self.session.get_inputs()
            outputs = self.session.get_outputs()
            if len(inputs) != 1 or not outputs:
                raise ValueError("model must expose one input and at least one output")
            shape = inputs[0].shape
            if len(shape) != 2 or (isinstance(shape[1], int) and shape[1] != len(FEATURE_ORDER)):
                raise ValueError(f"expected input shape [N,{len(FEATURE_ORDER)}], got {shape}")
            self.input_name = inputs[0].name
            self.output_name = outputs[0].name
            self.status = "ready"
            return True
        except Exception as exc:
            self.session = None
            self.status = f"unavailable:{type(exc).__name__}"
            return False

    def predict(self, features: dict[str, float]) -> dict[str, Any] | None:
        if self.session is None:
            return None
        try:
            import numpy as np
            vector = [float(features.get(key, 0.0)) for key in FEATURE_ORDER]
            if not all(math.isfinite(value) for value in vector):
                return None
            raw = self.session.run([self.output_name], {
                self.input_name: np.asarray([vector], dtype=np.float32)
            })[0]
            values = np.asarray(raw, dtype=np.float32).reshape(-1)
            if values.size == 0 or not np.all(np.isfinite(values)):
                return None
            # Contract: one output is an anomaly probability; two or more
            # outputs use the final class probability (normal, anomaly).
            score = float(values[0] if values.size == 1 else values[-1])
            score = min(1.0, max(0.0, score))
            return {"kind": "onnx_behavior_score", "score": round(score, 6),
                    "model": self.model_path.name, "features": len(vector)}
        except Exception:
            return None
