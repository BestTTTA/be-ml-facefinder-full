import logging
from typing import Dict, Tuple, List

from facefindr.database import FaceDatabase
from facefindr.utils import cosine_similarity
from facefindr.processor import ImageProcessor
from facefindr.search import FaceFinder
from facefindr.embedding import FaceEmbedding
from facefindr.config import DEFAULT_TOLERANCE


logger = logging.getLogger("FaceFindr")


class FaceFindr:
    def __init__(self, db_path: str = "facedb.sqlite", num_workers: int = None, default_tolerance: float = DEFAULT_TOLERANCE):
        self.db_path = db_path
        self.num_workers = num_workers
        self.default_tolerance = default_tolerance
        self.face_db = FaceDatabase(db_path)
        self.processor = ImageProcessor(self.face_db, num_workers)
        self.finder = FaceFinder(self.face_db, self.processor)

    def index_directory(self, directory: str, recursive: bool = True) -> int:
        """
        สแกนโฟลเดอร์เพื่อประมวลผลภาพทั้งหมด และเก็บใบหน้าที่พบลงฐานข้อมูล
        """
        return self.processor.scan_directory(directory, recursive)

    def add_image(self, img_id: str, img_url: str, cuser_id: str, event_id: str) -> Dict:
        """
        เพิ่มข้อมูลภาพใหม่ลงในฐานข้อมูล โดยใช้ img_id แทน image_path
        """
        faces = self.processor.process_image(img_id, img_url, cuser_id, event_id, save_to_db=True)
        return {
            "faces_found": len(faces),
            "id": faces[0].metadata["id"] if faces else None
        }

    def search_image(self, query_img_id: str, tolerance: float = None, 
                     max_results: int = 30) -> List[Tuple[FaceEmbedding, float]]:
        """
        ค้นหาใบหน้าจากภาพ query โดยใช้ img_id และคืนผลลัพธ์เรียงตามความคล้าย
        """
        tolerance = tolerance or self.default_tolerance
        results = self.finder.search_face(query_img_id, tolerance, max_results)
        self.face_db.delete_image_and_faces(query_img_id)
        logger.info(f"Cleaned up temporary query image: {query_img_id} from DB.")
        return results

    def get_database_stats(self) -> Dict:
        """
        คืนค่าสถิติภาพและใบหน้าในฐานข้อมูล
        """
        return {
            "total_images": self.face_db.get_image_count(),
            "total_faces": self.face_db.get_face_count(),
            "db_path": self.db_path,
        }

    def delete_image_from_db(self, img_id: str) -> bool:
        """
        ลบภาพและใบหน้าที่เกี่ยวข้องออกจากฐานข้อมูล โดยใช้ img_id
        """
        try:
            self.face_db.delete_image_and_faces(img_id)
            logger.info(f"Deleted image {img_id} and its faces from the database.")
            return True
        except Exception as e:
            logger.error(f"Error deleting image {img_id} from database: {e}")
            return False

    def process_image_from_url(self, img_url: str, cuser_id: str, event_id: str) -> List[FaceEmbedding]:
        """
        Process an image from URL and extract faces
        
        Args:
            img_url: URL to the image
            cuser_id: User ID
            event_id: Event ID
            
        Returns:
            List of FaceEmbedding objects
        """
        try:
            faces = self.processor.process_image_from_url(img_url, cuser_id, event_id)
            logger.info(f"Processed image from URL: {img_url}, found {len(faces)} faces")
            return faces
        except Exception as e:
            logger.error(f"Error processing image from URL {img_url}: {e}")
            return []

    def search_image_from_url(self, img_url: str, tolerance: float = None, max_results: int = 30) -> List[Tuple[FaceEmbedding, float]]:
        """
        Search for faces in an image from URL and find matches in the database
        
        Args:
            img_url: URL to the image to search
            tolerance: Similarity threshold (0.0 to 1.0)
            max_results: Maximum number of results to return
            
        Returns:
            List of tuples (FaceEmbedding, confidence)
        """
        tolerance = tolerance or self.default_tolerance
        try:
            # Process the query image to extract faces
            query_faces = self.processor.process_image_from_url(img_url, None, None)
            
            if not query_faces:
                logger.warning(f"No faces found in query image: {img_url}")
                return []

            # Get all faces from database
            db_faces = self.face_db.get_all_face_embeddings()
            if not db_faces:
                logger.warning("No faces found in database")
                return []

            # Find matches
            scores = []
            for query_face in query_faces:
                query_embedding = query_face.embedding
                for db_face in db_faces:
                    if db_face.embedding is not None and query_embedding is not None:
                        sim = cosine_similarity(query_embedding, db_face.embedding)
                        if sim >= tolerance:
                            scores.append((db_face, sim))

            # Sort by confidence and limit results
            scores.sort(key=lambda x: x[1], reverse=True)
            return scores[:max_results]
            
        except Exception as e:
            logger.error(f"Error searching image from URL {img_url}: {e}")
            return []

    def search_image_from_url_formatted(self, img_url: str, tolerance: float = None, max_results: int = 30) -> List[Tuple[Dict, float]]:
        """
        Search for faces in an image from URL and return formatted results for API
        
        Args:
            img_url: URL to the image to search
            tolerance: Similarity threshold (0.0 to 1.0)
            max_results: Maximum number of results to return
            
        Returns:
            List of tuples (formatted_face_data, confidence)
        """
        tolerance = tolerance or self.default_tolerance
        try:
            # Get raw search results
            raw_results = self.search_image_from_url(img_url, tolerance, max_results)
            
            # Format results for API
            formatted_results = []
            for face_embedding, confidence in raw_results:
                # Get image info for this face
                image_info = self.face_db.get_image_by_id(face_embedding.img_id)
                
                # Format face data
                face_data = {
                    "face_id": face_embedding.face_id,
                    "img_id": face_embedding.img_id,
                    "face_location": face_embedding.face_location,
                    "face_create_at": face_embedding.timestamp,
                    "image": image_info or {},
                    "user": {},  # Placeholder for user info
                    "event": {}   # Placeholder for event info
                }
                
                formatted_results.append((face_data, confidence))
            
            return formatted_results
            
        except Exception as e:
            logger.error(f"Error searching image from URL {img_url}: {e}")
            return []

    def search_image_from_local_file_formatted(self, image_path: str, tolerance: float = None, max_results: int = 30, event_id: str = None) -> List[Tuple[Dict, float]]:
        """
        Search for faces in an image from local file and return formatted results for API
        Optionally filter by event_id.
        """
        tolerance = tolerance or self.default_tolerance
        try:
            # Process the query image to extract faces (don't save to database)
            query_faces = self.processor.process_image(image_path, save_to_db=False)
            
            if not query_faces:
                logger.warning(f"No faces found in query image: {image_path}")
                return []

            # Get all faces from database, optionally filtered by event_id
            if event_id:
                db_faces = self.face_db.get_face_embeddings_by_event_id(event_id)
            else:
                db_faces = self.face_db.get_all_face_embeddings()
            if not db_faces:
                logger.warning("No faces found in database")
                return []

            # Find matches
            scores = []
            for query_face in query_faces:
                query_embedding = query_face.embedding
                for db_face in db_faces:
                    if db_face.embedding is not None and query_embedding is not None:
                        sim = cosine_similarity(query_embedding, db_face.embedding)
                        if sim >= tolerance:
                            scores.append((db_face, sim))

            # Sort by confidence and limit results
            scores.sort(key=lambda x: x[1], reverse=True)
            scores = scores[:max_results]
            
            # Format results for API
            formatted_results = []
            for face_embedding, confidence in scores:
                # Get image info for this face
                image_info = self.face_db.get_image_by_id(face_embedding.img_id)
                
                # Format face data
                face_data = {
                    "face_id": face_embedding.face_id,
                    "img_id": face_embedding.img_id,
                    "face_location": face_embedding.face_location,
                    "face_create_at": face_embedding.timestamp,
                    "image": image_info or {},
                    "user": {},  # Placeholder for user info
                    "event": {}   # Placeholder for event info
                }
                
                formatted_results.append((face_data, confidence))
            
            return formatted_results
            
        except Exception as e:
            logger.error(f"Error searching image from local file {image_path}: {e}")
            return []
