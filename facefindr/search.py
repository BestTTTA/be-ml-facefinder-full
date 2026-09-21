import os
from typing import List, Tuple
from PIL import Image, ImageDraw
import logging

from .database import FaceDatabase
from .processor import ImageProcessor
from .embedding import FaceEmbedding
from .utils import cosine_similarity

logger = logging.getLogger("FaceFinder")

class FaceFinder:
    def __init__(self, face_db: FaceDatabase, processor: ImageProcessor):
        self.face_db = face_db  
        self.processor = processor

    def search_face(self, query_img_id: str, tolerance: float = 0.4, max_results: int = 30) -> List[Tuple[FaceEmbedding, float]]:
        """
        ค้นหาใบหน้าทุกใบจากภาพ query โดยใช้ cosine similarity กับใบหน้าทั้งหมดในฐานข้อมูล
        """
        query_faces = self.processor.process_image(query_img_id, save_to_db=False)

        if not query_faces:
            logger.warning(f"ไม่พบใบหน้าในภาพค้นหา: {query_img_id}")
            return []

        db_faces = self.face_db.get_all_face_embeddings()
        if not db_faces:
            logger.warning("ไม่พบใบหน้าในฐานข้อมูล")
            return []

        scores = []
        for query_face in query_faces:
            query_embedding = query_face.embedding
            for db_face in db_faces:
                if db_face.embedding is not None and query_embedding is not None:
                    sim = cosine_similarity(query_embedding, db_face.embedding)
                    if sim >= tolerance:
                        scores.append((db_face, sim))

        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:max_results]

    def display_results(self, results: List[Tuple[FaceEmbedding, float]], output_dir: str = None):
        """
        แสดงผลลัพธ์การค้นหา และบันทึกภาพที่ตรงกันถ้ามีการระบุ output_dir
        """
        if not results:
            print("❌ ไม่พบผลลัพธ์ที่ตรงกัน")
            return

        print(f"✅ พบใบหน้าที่ตรงกัน {len(results)} รายการ:")

        if output_dir:
            os.makedirs(output_dir, exist_ok=True)

        for i, (face, confidence) in enumerate(results, 1):
            img_id = face.img_id  # Use img_id instead of image_path
            location = face.face_location
            print(f"{i}. similarity: {confidence:.3f}")
            print(f"   ไฟล์: {img_id}")  # Log the img_id instead of image path
            try:
                # Retrieve the image path from the database using img_id
                img_path = self.face_db.get_image_info(img_id)["image_path"]
                image = Image.open(img_path)
                draw = ImageDraw.Draw(image)
                top, right, bottom, left = location
                # draw.rectangle([(left, top), (right, bottom)], outline="red", width=3)  # Optional
                if output_dir:
                    result_path = os.path.join(output_dir, f"result_{i}_{os.path.basename(img_path)}")
                    image.save(result_path)
                    print(f"   บันทึกผลลัพธ์ที่: {result_path}")
            except Exception as e:
                logger.error(f"ไม่สามารถแสดงภาพ {img_id}: {e}")
