import numpy as np
from typing import Dict

class FaceEmbedding:
    def __init__(self, face_id: str, img_id: str, embedding: np.ndarray, 
                 face_location: tuple, timestamp: float, metadata: Dict = None):
        """
        Initialize a FaceEmbedding instance.
        :param face_id: Unique ID of the face (used as primary key in faces table)
        :param img_id: ID of the image the face belongs to (foreign key referencing images table)
        :param embedding: The face embedding (a numpy array)
        :param face_location: Coordinates (bounding box) of the face in the image
        :param timestamp: Time when the face was detected
        :param metadata: Additional metadata for the face (optional)
        """
        self.face_id = face_id
        self.img_id = img_id  # Updated from image_path to img_id
        self.embedding = embedding
        self.face_location = face_location
        self.timestamp = timestamp
        self.metadata = metadata or {}

    def to_dict(self) -> Dict:
        """
        Convert the FaceEmbedding object to a dictionary for serialization.
        """
        return {
            "face_id": self.face_id,
            "img_id": self.img_id,  # Updated from image_path to img_id
            "embedding": self.embedding.tolist() if self.embedding is not None else None,
            "face_location": self.face_location,
            "timestamp": self.timestamp,
            "metadata": self.metadata
        }

    @classmethod
    def from_dict(cls, data: Dict) -> 'FaceEmbedding':
        """
        Create a FaceEmbedding object from a dictionary.
        :param data: The dictionary containing the data to initialize the FaceEmbedding
        """
        embedding = np.array(data["embedding"]) if data["embedding"] is not None else None
        return cls(
            data["face_id"],
            data["img_id"],  # Updated from image_path to img_id
            embedding,
            tuple(data["face_location"]),
            data["timestamp"],
            data["metadata"]
        )
