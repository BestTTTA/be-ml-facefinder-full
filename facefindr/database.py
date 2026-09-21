import psycopg2
import pickle
import json
import os
from typing import List, Dict
from .embedding import FaceEmbedding
from .utils import convert_numpy
import logging

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.DEBUG)


class FaceDatabase:
    def __init__(self, db_url: str = None):
        self.db_url = db_url or os.getenv("DATABASE_URL")
        self._init_db()

    def _connect(self):
        return psycopg2.connect(self.db_url)
    
    def mark_image_processed(self, img_id: str):
        """
        Mark the image as processed in the database.
        :param img_id: The unique identifier for the image.
        """
        try:
            with self._connect() as conn:
                with conn.cursor() as cursor:
                    cursor.execute('''
                        UPDATE images
                        SET processed = TRUE
                        WHERE id = %s
                    ''', (img_id,))
                conn.commit()
        except Exception as e:
            logger.error(f"Error marking image {img_id} as processed: {e}")
            raise e

    def get_image_info(self, source_identifier: str) -> Dict:
        """
        Get image information by source identifier (file path or URL)
        """
        try:
            with self._connect() as conn:
                with conn.cursor() as cursor:
                    cursor.execute('''
                        SELECT id, file_hash, last_modified, processed, metadata 
                        FROM images 
                        WHERE img_url = %s
                    ''', (source_identifier,))
                    row = cursor.fetchone()
                    if row:
                        img_id, file_hash, last_modified, processed, metadata_str = row
                        return {
                            "id": img_id,
                            "file_hash": file_hash,
                            "last_modified": last_modified,
                            "processed": processed,
                            "metadata": json.loads(metadata_str) if metadata_str else {}
                        }
                    return None
        except Exception as e:
            logger.error(f"Error getting image info for {source_identifier}: {e}")
            return None

    def _init_db(self):
        with self._connect() as conn:
            with conn.cursor() as cursor:
                # ---------- CUSER DETAILS ----------
                cursor.execute(''' 
                    CREATE TABLE IF NOT EXISTS cuser_details (
                        id TEXT PRIMARY KEY,
                        province TEXT,
                        code INTEGER,
                        bank_name TEXT,
                        bank_id TEXT,
                        phone VARCHAR(15),
                        bank_copy_img_url TEXT,
                        line_id VARCHAR(50),
                        first_name VARCHAR(100),
                        last_name VARCHAR(100),
                        show_name VARCHAR(100),
                        set_details TEXT,
                        can_paid BOOLEAN,
                        create_at TIMESTAMP,
                        update_at TIMESTAMP
                    )
                ''')

                # ---------- CUSERS ----------
                cursor.execute(''' 
                    CREATE TABLE IF NOT EXISTS cusers (
                        id TEXT PRIMARY KEY,
                        email VARCHAR(100),
                        password TEXT,
                        img_url TEXT,
                        create_at TIMESTAMP,
                        update_at TIMESTAMP,
                        cuser_details_fk TEXT,
                        FOREIGN KEY (cuser_details_fk) REFERENCES cuser_details(id)
                    )
                ''')

                # ---------- EVENTS ----------
                cursor.execute(''' 
                    CREATE TABLE IF NOT EXISTS events (
                        id TEXT PRIMARY KEY,
                        event_name VARCHAR(100),
                        event_details VARCHAR(100),
                        event_img_url TEXT,
                        start_at TIMESTAMP,
                        end_at TIMESTAMP,
                        create_at TIMESTAMP,
                        update_at TIMESTAMP
                    )
                ''')

                # ---------- DUSERS ----------
                cursor.execute(''' 
                    CREATE TABLE IF NOT EXISTS dusers (
                        id TEXT PRIMARY KEY,
                        display_name VARCHAR(50),
                        display_details VARCHAR(50),
                        profile_url TEXT,
                        consent BOOLEAN,
                        create_at TIMESTAMP,
                        update_at TIMESTAMP
                    )
                ''')

                # ---------- IMAGES ----------
                cursor.execute(''' 
                    CREATE TABLE IF NOT EXISTS images (
                        id TEXT PRIMARY KEY,
                        img_url TEXT,
                        cuser_id TEXT,
                        event_id TEXT,
                        file_hash TEXT,
                        last_modified DOUBLE PRECISION,
                        processed BOOLEAN,
                        metadata JSONB,
                        create_at TIMESTAMP,
                        update_at TIMESTAMP,
                        FOREIGN KEY (cuser_id) REFERENCES cusers(id),
                        FOREIGN KEY (event_id) REFERENCES events(id)
                    )
                ''')

                # ---------- FACES ----------
                cursor.execute(''' 
                    CREATE TABLE IF NOT EXISTS faces (
                        id TEXT PRIMARY KEY,
                        face_embedding BYTEA,
                        face_location TEXT,
                        img_id TEXT,
                        create_at TIMESTAMP,
                        update_at TIMESTAMP,
                        FOREIGN KEY (img_id) REFERENCES images(id)
                    )
                ''')

                # ---------- CUSER EVENT HISTORY ----------
                cursor.execute(''' 
                    CREATE TABLE IF NOT EXISTS cuser_event_history (
                        id TEXT PRIMARY KEY,
                        create_at TIMESTAMP,
                        update_at TIMESTAMP,
                        cuser_id TEXT,
                        event_id TEXT,
                        FOREIGN KEY (cuser_id) REFERENCES cusers(id),
                        FOREIGN KEY (event_id) REFERENCES events(id)
                    )
                ''')

                # ---------- IMAGES CART ----------
                cursor.execute(''' 
                    CREATE TABLE IF NOT EXISTS images_cart (
                        id TEXT PRIMARY KEY,
                        create_at TIMESTAMP,
                        update_at TIMESTAMP,
                        duser_id TEXT,
                        img_id TEXT,
                        FOREIGN KEY (duser_id) REFERENCES dusers(id),
                        FOREIGN KEY (img_id) REFERENCES images(id)
                    )
                ''')

                # ---------- LEADERBOARD ----------
                cursor.execute(''' 
                    CREATE TABLE IF NOT EXISTS leaderboard (
                        id TEXT PRIMARY KEY,
                        event_id TEXT,
                        duser_id TEXT,
                        donate_price DOUBLE PRECISION,
                        user_details VARCHAR(50),
                        create_at TIMESTAMP,
                        update_at TIMESTAMP,
                        FOREIGN KEY (event_id) REFERENCES events(id),
                        FOREIGN KEY (duser_id) REFERENCES dusers(id)
                    )
                ''')

                # ---------- ALTER LEADERBOARD ----------
                cursor.execute('''
                    ALTER TABLE leaderboard ADD COLUMN IF NOT EXISTS stripe_payment_intent_id TEXT
                ''')
                cursor.execute('''
                    ALTER TABLE leaderboard ADD COLUMN IF NOT EXISTS payment_status VARCHAR(20) DEFAULT 'pending'
                ''')
                cursor.execute('''
                    CREATE INDEX IF NOT EXISTS idx_leaderboard_stripe_payment_intent ON leaderboard(stripe_payment_intent_id)
                ''')
                cursor.execute('''
                    CREATE INDEX IF NOT EXISTS idx_leaderboard_payment_status ON leaderboard(payment_status)
                ''')

                # ---------- PENDING PAYMENTS ----------
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS pending_payments (
                        id TEXT PRIMARY KEY,
                        stripe_payment_intent_id TEXT UNIQUE NOT NULL,
                        event_id TEXT NOT NULL,
                        duser_id TEXT NOT NULL,
                        amount DOUBLE PRECISION NOT NULL,
                        currency VARCHAR(3) DEFAULT 'thb',
                        status VARCHAR(20) DEFAULT 'pending',
                        create_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        update_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (event_id) REFERENCES events(id),
                        FOREIGN KEY (duser_id) REFERENCES dusers(id)
                    )
                ''')
                cursor.execute('''
                    CREATE INDEX IF NOT EXISTS idx_pending_payments_stripe_intent ON pending_payments(stripe_payment_intent_id)
                ''')
                cursor.execute('''
                    CREATE INDEX IF NOT EXISTS idx_pending_payments_event ON pending_payments(event_id)
                ''')
                cursor.execute('''
                    CREATE INDEX IF NOT EXISTS idx_pending_payments_duser ON pending_payments(duser_id)
                ''')
                cursor.execute('''
                    CREATE INDEX IF NOT EXISTS idx_pending_payments_status ON pending_payments(status)
                ''')

                # ---------- PAYMENT LOGS ----------
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS payment_logs (
                        id TEXT PRIMARY KEY,
                        stripe_payment_intent_id TEXT NOT NULL,
                        event_type VARCHAR(100) NOT NULL,
                        event_data JSONB,
                        processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        status VARCHAR(20) DEFAULT 'processed'
                    )
                ''')
                cursor.execute('''
                    CREATE INDEX IF NOT EXISTS idx_payment_logs_stripe_intent ON payment_logs(stripe_payment_intent_id)
                ''')
                cursor.execute('''
                    CREATE INDEX IF NOT EXISTS idx_payment_logs_event_type ON payment_logs(event_type)
                ''')
                cursor.execute('''
                    CREATE INDEX IF NOT EXISTS idx_payment_logs_processed_at ON payment_logs(processed_at)
                ''')

                # ---------- UPDATE LEADERBOARD PAYMENT STATUS ----------
                cursor.execute('''
                    UPDATE leaderboard
                    SET payment_status = 'direct'
                    WHERE payment_status IS NULL OR payment_status = 'pending'
                ''')

            conn.commit()


    def add_image(self, img_id: str, img_url: str, cuser_id: str, event_id: str, file_hash: str, last_modified: float, metadata: Dict):
        """
        Add image record to database without processing faces.
        Face processing should be done in ImageProcessor class.
        """
        metadata = convert_numpy(metadata or {})
        try:
            with self._connect() as conn:
                with conn.cursor() as cursor:
                    cursor.execute('''
                        INSERT INTO images (id, img_url, cuser_id, event_id, file_hash, last_modified, processed, metadata, create_at, update_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
                        ON CONFLICT (id) DO UPDATE SET
                            img_url = EXCLUDED.img_url,
                            cuser_id = EXCLUDED.cuser_id,
                            event_id = EXCLUDED.event_id,
                            file_hash = EXCLUDED.file_hash,
                            last_modified = EXCLUDED.last_modified,
                            metadata = EXCLUDED.metadata,
                            update_at = NOW()
                    ''', (
                        img_id,
                        img_url,
                        cuser_id,
                        event_id,
                        file_hash,
                        last_modified,
                        False,  # processed
                        json.dumps(metadata)  # metadata as JSON
                    ))
                conn.commit()
                logger.debug(f"Successfully added image {img_id} to database")
                return {"success": True, "id": img_id}
        except Exception as e:
            logger.error(f"Error inserting image data: {e}")
            raise e

    def add_face(self, face: FaceEmbedding, img_id: str):
        """
        Add face embedding to database
        """
        face_location = [int(x) for x in face.face_location]
        try:
            with self._connect() as conn:
                with conn.cursor() as cursor:
                    cursor.execute('''
                        INSERT INTO faces (id, img_id, face_embedding, face_location, create_at, update_at)
                        VALUES (%s, %s, %s, %s, to_timestamp(%s), NOW())
                        ON CONFLICT (id) DO UPDATE SET
                            face_embedding = EXCLUDED.face_embedding,
                            face_location = EXCLUDED.face_location,
                            update_at = NOW()
                    ''', (
                        face.face_id,  # face_id
                        img_id,  # img_id
                        psycopg2.Binary(pickle.dumps(face.embedding)),  # face embedding
                        json.dumps(face_location),  # JSON-encoded face_location
                        int(face.timestamp)  # Unix timestamp
                    ))
                conn.commit()
                logger.debug(f"Successfully added face {face.face_id} to database")
        except Exception as e:
            logger.error(f"Error inserting face data: {e}")
            raise e

    def get_all_face_embeddings(self) -> List[FaceEmbedding]:
        """
        Get all face embeddings from database
        """
        try:
            with self._connect() as conn:
                with conn.cursor() as cursor:
                    cursor.execute('''
                        SELECT f.id, f.face_embedding, f.face_location, f.img_id, f.create_at
                        FROM faces f
                    ''')
                    faces = []
                    for row in cursor.fetchall():
                        try:
                            face_id, embedding_blob, face_location_str, img_id, create_at = row
                            
                            # Skip if embedding_blob is None
                            if embedding_blob is None:
                                logger.warning(f"Skipping face {face_id} with null embedding")
                                continue
                                
                            # Try to unpickle the embedding
                            try:
                                embedding = pickle.loads(embedding_blob)
                            except Exception as pickle_error:
                                logger.error(f"Error unpickling embedding for face {face_id}: {pickle_error}")
                                continue
                            
                            # Parse face location
                            try:
                                face_location = tuple(json.loads(face_location_str))
                            except Exception as json_error:
                                logger.error(f"Error parsing face location for face {face_id}: {json_error}")
                                face_location = (0, 0, 0, 0)  # Default location
                            
                            face = FaceEmbedding(
                                face_id=face_id,
                                img_id=img_id,
                                embedding=embedding,
                                face_location=face_location,
                                timestamp=create_at.timestamp() if create_at else None,
                                metadata={"img_id": img_id}
                            )
                            faces.append(face)
                        except Exception as row_error:
                            logger.error(f"Error processing face row: {row_error}")
                            continue
                    return faces
        except Exception as e:
            logger.error(f"Error getting all face embeddings: {e}")
            return []

    def get_faces_by_image(self, source_identifier: str) -> List[FaceEmbedding]:
        """
        Get faces by source identifier (file path or URL)
        """
        try:
            with self._connect() as conn:
                with conn.cursor() as cursor:
                    cursor.execute('''
                        SELECT f.id, f.face_embedding, f.face_location, f.img_id, f.create_at
                        FROM faces f
                        JOIN images i ON f.img_id = i.id
                        WHERE i.img_url = %s
                    ''', (source_identifier,))
                    faces = []
                    for row in cursor.fetchall():
                        try:
                            face_id, embedding_blob, face_location_str, img_id, create_at = row
                            
                            # Skip if embedding_blob is None
                            if embedding_blob is None:
                                logger.warning(f"Skipping face {face_id} with null embedding")
                                continue
                                
                            # Try to unpickle the embedding
                            try:
                                embedding = pickle.loads(embedding_blob)
                            except Exception as pickle_error:
                                logger.error(f"Error unpickling embedding for face {face_id}: {pickle_error}")
                                continue
                            
                            # Parse face location
                            try:
                                face_location = tuple(json.loads(face_location_str))
                            except Exception as json_error:
                                logger.error(f"Error parsing face location for face {face_id}: {json_error}")
                                face_location = (0, 0, 0, 0)  # Default location
                            
                            face = FaceEmbedding(
                                face_id=face_id,
                                img_id=img_id,
                                embedding=embedding,
                                face_location=face_location,
                                timestamp=create_at.timestamp() if create_at else None,
                                metadata={"img_id": img_id}
                            )
                            faces.append(face)
                        except Exception as row_error:
                            logger.error(f"Error processing face row: {row_error}")
                            continue
                    return faces
        except Exception as e:
            logger.error(f"Error getting faces by image {source_identifier}: {e}")
            return []

    def delete_image_and_faces(self, img_id: str):
        """
        Delete image and all associated faces
        """
        try:
            with self._connect() as conn:
                with conn.cursor() as cursor:
                    cursor.execute('DELETE FROM faces WHERE img_id = %s', (img_id,))
                    cursor.execute('DELETE FROM images WHERE id = %s', (img_id,))
                conn.commit()
                logger.debug(f"Successfully deleted image {img_id} and associated faces")
        except Exception as e:
            logger.error(f"Error deleting image {img_id} and faces: {e}")
            raise e

    def clear_corrupted_faces(self):
        """
        Clear faces with corrupted or null embeddings
        """
        try:
            with self._connect() as conn:
                with conn.cursor() as cursor:
                    # Delete faces with null embeddings
                    cursor.execute('DELETE FROM faces WHERE face_embedding IS NULL')
                    null_count = cursor.rowcount
                    
                    # Delete faces with empty embeddings
                    cursor.execute('DELETE FROM faces WHERE LENGTH(face_embedding) = 0')
                    empty_count = cursor.rowcount
                    
                    # Find and delete faces with corrupted pickle data
                    corrupted_count = 0
                    cursor.execute('SELECT id, face_embedding FROM faces WHERE face_embedding IS NOT NULL AND LENGTH(face_embedding) > 0')
                    for row in cursor.fetchall():
                        face_id, embedding_blob = row
                        try:
                            pickle.loads(embedding_blob)
                        except Exception:
                            # This embedding is corrupted, delete it
                            cursor.execute('DELETE FROM faces WHERE id = %s', (face_id,))
                            corrupted_count += 1
                    
                conn.commit()
                logger.info(f"Cleared {null_count} null embeddings, {empty_count} empty embeddings, and {corrupted_count} corrupted embeddings")
                return {
                    "null_cleared": null_count, 
                    "empty_cleared": empty_count,
                    "corrupted_cleared": corrupted_count
                }
        except Exception as e:
            logger.error(f"Error clearing corrupted faces: {e}")
            raise e

    def find_corrupted_embeddings(self) -> Dict:
        """
        Find and report corrupted embeddings without deleting them
        """
        try:
            with self._connect() as conn:
                with conn.cursor() as cursor:
                    cursor.execute('SELECT COUNT(*) FROM faces WHERE face_embedding IS NULL')
                    null_count = cursor.fetchone()[0]
                    
                    cursor.execute('SELECT COUNT(*) FROM faces WHERE LENGTH(face_embedding) = 0')
                    empty_count = cursor.fetchone()[0]
                    
                    # Check for corrupted pickle data
                    corrupted_faces = []
                    cursor.execute('SELECT id, img_id, LENGTH(face_embedding) as embedding_size FROM faces WHERE face_embedding IS NOT NULL AND LENGTH(face_embedding) > 0')
                    for row in cursor.fetchall():
                        face_id, img_id, embedding_size = row
                        cursor.execute('SELECT face_embedding FROM faces WHERE id = %s', (face_id,))
                        embedding_blob = cursor.fetchone()[0]
                        try:
                            pickle.loads(embedding_blob)
                        except Exception as e:
                            corrupted_faces.append({
                                'face_id': face_id,
                                'img_id': img_id,
                                'embedding_size': embedding_size,
                                'error': str(e)
                            })
                    
                    return {
                        'null_embeddings': null_count,
                        'empty_embeddings': empty_count,
                        'corrupted_embeddings': len(corrupted_faces),
                        'corrupted_faces': corrupted_faces
                    }
        except Exception as e:
            logger.error(f"Error finding corrupted embeddings: {e}")
            return {'error': str(e)}

    def get_image_count(self) -> int:
        """
        Get total number of images in database
        """
        try:
            with self._connect() as conn:
                with conn.cursor() as cursor:
                    cursor.execute('SELECT COUNT(*) FROM images')
                    return cursor.fetchone()[0]
        except Exception as e:
            logger.error(f"Error getting image count: {e}")
            return 0

    def get_face_count(self) -> int:
        """
        Get total number of faces in database
        """
        try:
            with self._connect() as conn:
                with conn.cursor() as cursor:
                    cursor.execute('SELECT COUNT(*) FROM faces')
                    return cursor.fetchone()[0]
        except Exception as e:
            logger.error(f"Error getting face count: {e}")
            return 0

    def debug_face_data(self) -> Dict:
        """
        Debug method to check face data in database
        """
        try:
            with self._connect() as conn:
                with conn.cursor() as cursor:
                    # Check total faces
                    cursor.execute('SELECT COUNT(*) FROM faces')
                    total_faces = cursor.fetchone()[0]
                    
                    # Check faces with null embeddings
                    cursor.execute('SELECT COUNT(*) FROM faces WHERE face_embedding IS NULL')
                    null_embeddings = cursor.fetchone()[0]
                    
                    # Check faces with valid embeddings
                    cursor.execute('SELECT COUNT(*) FROM faces WHERE face_embedding IS NOT NULL')
                    valid_embeddings = cursor.fetchone()[0]
                    
                    # Get sample face data
                    cursor.execute('''
                        SELECT id, img_id, LENGTH(face_embedding) as embedding_size, face_location
                        FROM faces 
                        LIMIT 5
                    ''')
                    sample_faces = []
                    for row in cursor.fetchall():
                        face_id, img_id, embedding_size, face_location = row
                        sample_faces.append({
                            'face_id': face_id,
                            'img_id': img_id,
                            'embedding_size': embedding_size,
                            'face_location': face_location
                        })
                    
                    return {
                        'total_faces': total_faces,
                        'null_embeddings': null_embeddings,
                        'valid_embeddings': valid_embeddings,
                        'sample_faces': sample_faces
                    }
        except Exception as e:
            logger.error(f"Error debugging face data: {e}")
            return {'error': str(e)}

    def get_processed_image_count(self) -> int:
        """
        Get number of processed images
        """
        try:
            with self._connect() as conn:
                with conn.cursor() as cursor:
                    cursor.execute('SELECT COUNT(*) FROM images WHERE processed = TRUE')
                    return cursor.fetchone()[0]
        except Exception as e:
            logger.error(f"Error getting processed image count: {e}")
            return 0

    def get_image_by_id(self, img_id: str) -> Dict:
        """
        Get image by ID
        """
        try:
            with self._connect() as conn:
                with conn.cursor() as cursor:
                    cursor.execute('''
                        SELECT id, img_url, cuser_id, event_id, file_hash, last_modified, processed, metadata
                        FROM images 
                        WHERE id = %s
                    ''', (img_id,))
                    row = cursor.fetchone()
                    if row:
                        img_id, img_url, cuser_id, event_id, file_hash, last_modified, processed, metadata_raw = row
                        
                        # Handle metadata parsing - it might be a dict or JSON string
                        metadata = {}
                        if metadata_raw:
                            if isinstance(metadata_raw, dict):
                                metadata = metadata_raw
                            elif isinstance(metadata_raw, str):
                                try:
                                    metadata = json.loads(metadata_raw)
                                except json.JSONDecodeError:
                                    logger.warning(f"Could not parse metadata JSON for image {img_id}")
                                    metadata = {}
                            else:
                                logger.warning(f"Unexpected metadata type for image {img_id}: {type(metadata_raw)}")
                                metadata = {}
                        
                        return {
                            "id": img_id,
                            "img_url": img_url,
                            "cuser_id": cuser_id,
                            "event_id": event_id,
                            "file_hash": file_hash,
                            "last_modified": last_modified,
                            "processed": processed,
                            "metadata": metadata
                        }
                    return None
        except Exception as e:
            logger.error(f"Error getting image by ID {img_id}: {e}")
            return None

    def get_face_embeddings_by_event_id(self, event_id: str) -> List[FaceEmbedding]:
        """
        Get all face embeddings for a specific event_id
        """
        try:
            with self._connect() as conn:
                with conn.cursor() as cursor:
                    cursor.execute('''
                        SELECT f.id, f.face_embedding, f.face_location, f.img_id, f.create_at
                        FROM faces f
                        JOIN images i ON f.img_id = i.id
                        WHERE i.event_id = %s
                    ''', (event_id,))
                    faces = []
                    for row in cursor.fetchall():
                        try:
                            face_id, embedding_blob, face_location_str, img_id, create_at = row
                            if embedding_blob is None:
                                logger.warning(f"Skipping face {face_id} with null embedding")
                                continue
                            try:
                                embedding = pickle.loads(embedding_blob)
                            except Exception as pickle_error:
                                logger.error(f"Error unpickling embedding for face {face_id}: {pickle_error}")
                                continue
                            try:
                                face_location = tuple(json.loads(face_location_str))
                            except Exception as json_error:
                                logger.error(f"Error parsing face location for face {face_id}: {json_error}")
                                face_location = (0, 0, 0, 0)
                            face = FaceEmbedding(
                                face_id=face_id,
                                img_id=img_id,
                                embedding=embedding,
                                face_location=face_location,
                                timestamp=create_at.timestamp() if create_at else None,
                                metadata={"img_id": img_id}
                            )
                            faces.append(face)
                        except Exception as row_error:
                            logger.error(f"Error processing face row: {row_error}")
                            continue
                    return faces
        except Exception as e:
            logger.error(f"Error getting face embeddings by event_id {event_id}: {e}")
            return []
        
