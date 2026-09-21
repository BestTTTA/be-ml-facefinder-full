"""Face engine protocol. Implementations must be replaceable without touching the API layer."""
from dataclasses import dataclass, field
from typing import List, Protocol

import numpy as np


@dataclass
class DetectedFace:
    embedding: np.ndarray            # L2-normalised, shape (EMBEDDING_DIMENSION,)
    bbox: dict = field(default_factory=dict)  # {"x1","y1","x2","y2"} in pixels
    det_score: float = 0.0


class FaceEngine(Protocol):
    name: str
    dimension: int

    def load(self) -> None:
        """Load model weights. Called once per process at startup."""

    def detect(self, image_bgr: np.ndarray) -> List[DetectedFace]:
        """Detect all faces in a BGR uint8 image and return their embeddings."""

    def is_ready(self) -> bool: ...
