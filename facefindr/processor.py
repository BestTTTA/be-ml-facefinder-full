import os
import cv2
import time
import hashlib
import json
import uuid
import requests
import numpy as np
from tqdm import tqdm
from PIL import Image, ExifTags
from typing import List, Dict, Optional
from insightface.app import FaceAnalysis

from .embedding import FaceEmbedding
from .utils import convert_numpy
from .database import FaceDatabase
import logging

logger = logging.getLogger("ImageProcessor")

class ImageProcessor:
    def __init__(self, face_db: FaceDatabase, num_workers: int = None):
        self.face_db = face_db
        self.num_workers = num_workers or os.cpu_count()
        self.valid_extensions = {'.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.webp'}

        # Initialize the face recognition model
        self.retinapp = FaceAnalysis(name="buffalo_l")
        self.retinapp.prepare(ctx_id=0, det_size=(640, 640))  # 0 = GPU, -1 = CPU

    def _calculate_file_hash(self, file_path: str) -> str:
        """Calculate MD5 hash of a file"""
        hasher = hashlib.md5()
        with open(file_path, 'rb') as f:
            buf = f.read(65536)
            while len(buf) > 0:
                hasher.update(buf)
                buf = f.read(65536)
        return hasher.hexdigest()

    def _calculate_url_hash(self, url: str) -> str:
        """Calculate MD5 hash of a URL string"""
        return hashlib.md5(url.encode()).hexdigest()

    def _extract_image_metadata(self, image_path: str) -> Dict:
        """Extract metadata from image file"""
        try:
            with Image.open(image_path) as img:
                metadata = {
                    'width': img.width,
                    'height': img.height,
                    'format': img.format,
                    'mode': img.mode
                }
                exif_data = {}
                if hasattr(img, '_getexif') and img._getexif():
                    exif = {
                        ExifTags.TAGS.get(tag, tag): value
                        for tag, value in img._getexif().items()
                        if tag in ExifTags.TAGS
                    }
                    for key, value in exif.items():
                        if isinstance(value, (bytes, bytearray)):
                            exif_data[key] = "binary_data"
                        elif hasattr(value, 'isoformat'):
                            exif_data[key] = value.isoformat()
                        else:
                            try:
                                json.dumps({key: value})
                                exif_data[key] = value
                            except:
                                exif_data[key] = str(value)
                    metadata['exif'] = exif_data
                return metadata
        except Exception as e:
            logger.warning(f"Cannot read metadata from {image_path}: {e}")
            return {}

    def read_image_from_url(self, url: str) -> Optional[np.ndarray]:
        """Read image from URL and return as numpy array"""
        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()  # Raise exception for bad status codes
            
            image_array = np.asarray(bytearray(response.content), dtype=np.uint8)
            img = cv2.imdecode(image_array, cv2.IMREAD_COLOR)
            
            if img is None:
                logger.error(f"Failed to decode image from URL: {url}")
                return None
                
            return img
        except requests.exceptions.RequestException as e:
            logger.error(f"Error downloading image from URL {url}: {e}")
            return None
        except Exception as e:
            logger.error(f"Error reading image from URL {url}: {e}")
            return None

    def read_image_from_file(self, file_path: str) -> Optional[np.ndarray]:
        """Read image from local file and return as numpy array"""
        try:
            if not os.path.exists(file_path):
                logger.warning(f"Image file not found: {file_path}")
                return None
                
            img = cv2.imread(file_path)
            if img is None:
                logger.warning(f"Cannot read image file: {file_path}")
                return None
                
            return img
        except Exception as e:
            logger.error(f"Error reading image file {file_path}: {e}")
            return None

    def process_image(self, image_path: str = None, img_url: str = None, cuser_id: str = None, event_id: str = None, save_to_db: bool = True) -> List[FaceEmbedding]:
        """
        Process an image either from local file or URL and extract faces
        
        Args:
            image_path: Path to local image file (optional)
            img_url: URL to image (optional)
            cuser_id: User ID
            event_id: Event ID
            
        Returns:
            List of FaceEmbedding objects
        """
        try:
            # Validate input - must have either image_path or img_url
            if not image_path and not img_url:
                logger.error("Either image_path or img_url must be provided")
                return []
                
            # Generate unique image ID
            img_id = str(uuid.uuid4())
            
            # Determine source and load image
            if img_url:
                # Process from URL
                img_cv = self.read_image_from_url(img_url)
                if img_cv is None:
                    logger.warning(f"Cannot read image from URL: {img_url}")
                    return []
                
                # For URL processing
                file_hash = self._calculate_url_hash(img_url)
                last_modified = time.time()
                metadata = {
                    'source': 'url',
                    'url': img_url,
                    'width': img_cv.shape[1],
                    'height': img_cv.shape[0],
                    'channels': img_cv.shape[2] if len(img_cv.shape) > 2 else 1
                }
                source_identifier = img_url
                
            else:
                # Process from local file
                img_cv = self.read_image_from_file(image_path)
                if img_cv is None:
                    logger.warning(f"Cannot read image file: {image_path}")
                    return []
                
                file_hash = self._calculate_file_hash(image_path)
                last_modified = os.path.getmtime(image_path)
                metadata = self._extract_image_metadata(image_path)
                metadata = convert_numpy(metadata)
                metadata['source'] = 'file'
                source_identifier = image_path

            # Only save to database if save_to_db is True
            if save_to_db:
                # Check if already processed (for both files and URLs)
                existing_info = self.face_db.get_image_info(source_identifier)
                if existing_info and existing_info.get("file_hash") == file_hash and existing_info.get("processed"):
                    logger.debug(f"Skipping already processed image: {source_identifier}")
                    return self.face_db.get_faces_by_image(source_identifier)

                # Add image to database
                self.face_db.add_image(
                    img_id=img_id,
                    img_url=source_identifier,  # Use source_identifier for both files and URLs
                    cuser_id=cuser_id,
                    event_id=event_id,
                    file_hash=file_hash,
                    last_modified=last_modified,
                    metadata=metadata
                )

            # Process the image for faces
            faces = self.retinapp.get(img_cv)
            if not faces:
                logger.debug(f"No faces found in image: {source_identifier}")
                self.face_db.mark_image_processed(img_id)
                return []

            # Extract face embeddings
            faces_out = []
            timestamp = time.time()
            
            for i, face in enumerate(faces):
                try:
                    # Extract bounding box
                    x1, y1, x2, y2 = face.bbox.astype(int)
                    top, right, bottom, left = y1, x2, y2, x1
                    
                    # Get face embedding
                    embedding = face.embedding
                    
                    # Generate unique face ID
                    face_id = f"{img_id}_{i}_{int(timestamp * 1000)}"
                    
                    # Create face embedding object
                    face_emb = FaceEmbedding(
                        face_id=face_id,
                        img_id=img_id,
                        embedding=embedding,
                        face_location=(top, right, bottom, left),
                        timestamp=timestamp,
                        metadata={
                            "index": i,
                            "img_id": img_id,
                            "confidence": getattr(face, 'det_score', 0.0),
                            "source": source_identifier
                        }
                    )
                    
                    # Add face to database only if save_to_db is True
                    if save_to_db:
                        self.face_db.add_face(face_emb, img_id=img_id)
                    faces_out.append(face_emb)
                    
                except Exception as e:
                    logger.error(f"Error processing face {i} in image {source_identifier}: {e}")
                    continue

            # Mark image as processed only if save_to_db is True
            if save_to_db:
                self.face_db.mark_image_processed(img_id)
            
            logger.info(f"Successfully processed {len(faces_out)} faces from {source_identifier}")
            return faces_out

        except Exception as e:
            logger.error(f"Error processing image {image_path or img_url}: {e}")
            return []

    def process_image_from_file(self, image_path: str, cuser_id: str = None, event_id: str = None) -> List[FaceEmbedding]:
        """Convenience method to process image from file"""
        return self.process_image(image_path=image_path, cuser_id=cuser_id, event_id=event_id, save_to_db=True)

    def process_image_from_url(self, img_url: str, cuser_id: str = None, event_id: str = None) -> List[FaceEmbedding]:
        """Convenience method to process image from URL"""
        return self.process_image(img_url=img_url, cuser_id=cuser_id, event_id=event_id, save_to_db=True)

    def scan_directory(self, directory: str, recursive: bool = True, cuser_id: str = None, event_id: str = None) -> int:
        """
        Scan directory to process all images and store found faces in database
        
        Args:
            directory: Directory path to scan
            recursive: Whether to scan subdirectories
            cuser_id: User ID for all images
            event_id: Event ID for all images
            
        Returns:
            Number of successfully processed images
        """
        directory = os.path.abspath(directory)
        logger.info(f"Scanning directory: {directory}")
        
        if not os.path.exists(directory):
            logger.error(f"Directory does not exist: {directory}")
            return 0
            
        image_paths = []

        try:
            if recursive:
                for root, _, files in os.walk(directory):
                    for file in files:
                        file_path = os.path.join(root, file)
                        if os.path.splitext(file)[1].lower() in self.valid_extensions:
                            image_paths.append(file_path)
            else:
                for file in os.listdir(directory):
                    file_path = os.path.join(directory, file)
                    if os.path.isfile(file_path) and os.path.splitext(file)[1].lower() in self.valid_extensions:
                        image_paths.append(file_path)
        except Exception as e:
            logger.error(f"Error scanning directory {directory}: {e}")
            return 0

        logger.info(f"Found {len(image_paths)} image files")
        
        if not image_paths:
            logger.info("No image files found")
            return 0

        processed_count = 0
        
        def process_single_image(path):
            """Helper function to process a single image"""
            try:
                faces = self.process_image_from_file(path, cuser_id=cuser_id, event_id=event_id)
                if faces:
                    logger.debug(f"Found {len(faces)} faces in image {path}")
                return True
            except Exception as e:
                logger.error(f"Error processing {path}: {e}")
                return False

        # Process images with progress bar
        with tqdm(total=len(image_paths), desc="Processing images") as pbar:
            if self.num_workers == 1:
                # Single-threaded processing
                for path in image_paths:
                    if process_single_image(path):
                        processed_count += 1
                    pbar.update(1)
            else:
                # Multi-threaded processing
                from concurrent.futures import ThreadPoolExecutor, as_completed
                with ThreadPoolExecutor(max_workers=self.num_workers) as executor:
                    futures = {executor.submit(process_single_image, path): path for path in image_paths}
                    
                    for future in as_completed(futures):
                        path = futures[future]
                        try:
                            if future.result():
                                processed_count += 1
                        except Exception as e:
                            logger.error(f"Error processing {path}: {e}")
                        pbar.update(1)

        logger.info(f"Processing completed: Successfully processed {processed_count}/{len(image_paths)} files")
        return processed_count

    def get_processing_stats(self) -> Dict:
        """Get processing statistics"""
        try:
            stats = {
                'total_images': self.face_db.get_image_count(),
                'total_faces': self.face_db.get_face_count(),
                'processed_images': self.face_db.get_processed_image_count(),
                'valid_extensions': list(self.valid_extensions),
                'num_workers': self.num_workers
            }
            return stats
        except Exception as e:
            logger.error(f"Error getting processing stats: {e}")
            return {}