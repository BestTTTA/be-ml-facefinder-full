"""Deterministic stand-in engine for tests/CI (FACE_ENGINE=fake). Never use in production.

Face count is derived from image width (<50px: none, <200px: one, else two).
The embedding is a seeded random unit vector keyed on the dominant colour, so
images of the same colour "belong to the same person".
"""
from typing import List

import numpy as np

from app.engines.base import DetectedFace


class FakeFaceEngine:
    name = "fake"

    def __init__(self, dimension: int = 512):
        self.dimension = dimension
        self._ready = False

    def load(self) -> None:
        self._ready = True

    def is_ready(self) -> bool:
        return self._ready

    def detect(self, image_bgr: np.ndarray) -> List[DetectedFace]:
        h, w = image_bgr.shape[:2]
        n = 0 if w < 50 else (1 if w < 200 else 2)
        mean = image_bgr.reshape(-1, image_bgr.shape[-1]).mean(axis=0)
        seed = int(sum(int(c) // 16 * (17 ** i) for i, c in enumerate(mean))) % (2**31)
        faces = []
        for i in range(n):
            rng = np.random.default_rng(seed + i * 7919)
            v = rng.standard_normal(self.dimension).astype(np.float32)
            v /= np.linalg.norm(v)
            faces.append(DetectedFace(embedding=v, bbox={"x1": i * 10, "y1": 0, "x2": i * 10 + 40, "y2": 40},
                                      det_score=0.99))
        return faces
