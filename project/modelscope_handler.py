# ModelScope Image Generation Handler
# Handles async image generation via ModelScope API

import requests
import time
import threading
import uuid
import logging
from io import BytesIO
from datetime import datetime, timezone
from .modelscope_config import (
    MODELSCOPE_BASE_URL,
    MODELSCOPE_MODELS,
    DEFAULT_TIMEOUT_SECONDS,
    BUTTON_LOCKOUT_SECONDS,
    get_model_by_id
)
from .s3_utils import get_s3_client, get_s3_config, get_public_s3_url

# Setup logger for terminal output only
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class ModelScopeInferenceManager:
    """
    Manages ModelScope image generation requests.
    Tracks active inferences per user to implement button lockout logic.
    """
    
    def __init__(self):
        # {username: {space_id: {...inference_data}}}
        self.active_inferences = {}
        self.lock = threading.Lock()
        # Token rotation index
        self.token_index = 0
    
    def _get_next_token(self, keys):
        """Get the next token from the pool using round-robin."""
        if not keys:
            return None
        with self.lock:
            token = keys[self.token_index % len(keys)]
            self.token_index = (self.token_index + 1) % len(keys)
            return token
    
    def _upload_image_to_s3(self, image_url, username):
        """
        Download image from ModelScope URL and upload to user's S3 bucket.
        Returns: (success: bool, s3_url or None)
        """
        try:
            # Download image from ModelScope
            logger.info(f"[ModelScope] Downloading image for user {username}")
            response = requests.get(image_url, timeout=60)
            response.raise_for_status()
            
            content_type = response.headers.get('Content-Type', 'image/png')
            
            # Determine file extension
            ext = 'png'
            if 'jpeg' in content_type or 'jpg' in content_type:
                ext = 'jpg'
            elif 'webp' in content_type:
                ext = 'webp'
            
            # Generate unique filename
            filename = f"img_{uuid.uuid4().hex[:8]}.{ext}"
            object_key = f"{username}/gen_img/{filename}"
            
            # Get S3 client and upload
            s3_client = get_s3_client()
            s3_config = get_s3_config()
            
            if not s3_client or not s3_config:
                logger.error("[ModelScope] S3 not configured")
                return False, None
            
            bucket_name = s3_config.get('S3_BUCKET_NAME')
            if not bucket_name:
                logger.error("[ModelScope] S3 bucket name not configured")
                return False, None
            
            # Upload to S3 using put_object (like api.py does)
            data_bytes = response.content
            s3_client.put_object(
                Bucket=bucket_name,
                Key=object_key,
                Body=data_bytes,
                ContentType=content_type,
                ContentLength=len(data_bytes),
                ACL='public-read'
            )
            
            # Get public URL
            public_url = get_public_s3_url(object_key)
            logger.info(f"[ModelScope] Uploaded to S3: {object_key}")
            
            return True, public_url
            
        except requests.exceptions.RequestException as e:
            logger.error(f"[ModelScope] Failed to download image: {e}")
            return False, None
        except Exception as e:
            logger.error(f"[ModelScope] Failed to upload to S3: {e}")
            return False, None
    
    def can_user_start_inference(self, username, space_id, timeout_seconds=None):
        """
        Check if user can start a new inference.
        Returns: (can_start: bool, reason: str, wait_seconds: int)
        """
        if timeout_seconds is None:
            timeout_seconds = DEFAULT_TIMEOUT_SECONDS
        
        with self.lock:
            user_inferences = self.active_inferences.get(username, {})
            inference = user_inferences.get(space_id)
            
            if not inference:
                return True, "ready", 0
            
            # Check if inference is complete or timed out
            started_at = inference.get('started_at')
            if not started_at:
                return True, "ready", 0
            
            elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
            
            # If inference succeeded or failed, user can start new one
            if inference.get('status') in ['success', 'failed']:
                return True, "ready", 0
            
            # If timed out, user can retry
            if elapsed >= timeout_seconds:
                return True, "timeout", 0
            
            # Still processing, calculate remaining wait time
            # Button lockout is for BUTTON_LOCKOUT_SECONDS, then disabled until timeout
            if elapsed < BUTTON_LOCKOUT_SECONDS:
                wait = int(BUTTON_LOCKOUT_SECONDS - elapsed)
                return False, "processing", wait
            else:
                # Between lockout and timeout - button still disabled
                wait = int(timeout_seconds - elapsed)
                return False, "waiting", wait
    
    def start_inference(self, username, space_id, model_id, prompt, keys, resolution=None, image_urls=None):
        """
        Start an async image generation task.
        Returns: (success: bool, task_id or error: str)
        resolution: dict with 'width' and 'height' keys
        image_urls: list of image URLs for editing (optional)
        """
        token = self._get_next_token(keys)
        if not token:
            return False, "No ModelScope tokens configured"
        
        model = get_model_by_id(model_id)
        if not model:
            return False, f"Invalid model: {model_id}"
        
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-ModelScope-Async-Mode": "true"
        }
        
        payload = {
            "model": model_id,
            "prompt": prompt
        }
        
        # Add resolution if specified
        if resolution:
            payload["size"] = f"{resolution.get('width', 1024)}x{resolution.get('height', 1024)}"

        # Add image_urls if specified
        if image_urls:
            payload["image_url"] = image_urls
        
        try:
            response = requests.post(
                f"{MODELSCOPE_BASE_URL}v1/images/generations",
                headers=headers,
                json=payload,
                timeout=30
            )
            response.raise_for_status()
            
            data = response.json()
            task_id = data.get("task_id")
            
            if not task_id:
                return False, "No task_id in response"
            
            # Record active inference
            with self.lock:
                if username not in self.active_inferences:
                    self.active_inferences[username] = {}
                
                self.active_inferences[username][space_id] = {
                    'task_id': task_id,
                    'model_id': model_id,
                    'prompt': prompt,
                    'resolution': resolution,
                    'image_urls': image_urls,
                    'token': token,
                    'started_at': datetime.now(timezone.utc),
                    'completed_at': None,
                    'status': 'processing',
                    'result_url': None,
                    'elapsed_seconds': None
                }
            
            return True, task_id
            
        except requests.exceptions.RequestException as e:
            return False, f"API request failed: {str(e)}"
        except Exception as e:
            return False, f"Unexpected error: {str(e)}"
    
    def check_inference_status(self, username, space_id):
        """
        Check the status of an active inference.
        Returns inference data dict or None.
        """
        with self.lock:
            user_inferences = self.active_inferences.get(username, {})
            inference = user_inferences.get(space_id)
            if not inference:
                return None
            return inference.copy()
    
    def poll_task_status(self, username, space_id):
        """
        Poll ModelScope API for task completion.
        Updates internal state and returns current status.
        Uploads successful results to user's S3 bucket.
        Errors are logged to terminal only, not exposed to frontend.
        """
        with self.lock:
            user_inferences = self.active_inferences.get(username, {})
            inference = user_inferences.get(space_id)
            if not inference:
                return None
        
        task_id = inference.get('task_id')
        token = inference.get('token')
        
        if not task_id or not token:
            return inference
        
        # Already completed
        if inference.get('status') in ['success', 'failed']:
            return inference
        
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-ModelScope-Task-Type": "image_generation"
        }
        
        try:
            response = requests.get(
                f"{MODELSCOPE_BASE_URL}v1/tasks/{task_id}",
                headers=headers,
                timeout=15
            )
            response.raise_for_status()
            
            data = response.json()
            task_status = data.get("task_status")
            
            logger.info(f"[ModelScope] Task {task_id} status: {task_status}")
            
            if task_status == "SUCCEED":
                output_images = data.get("output_images", [])
                modelscope_url = output_images[0] if output_images else None
                
                if modelscope_url:
                    # Upload to user's S3 bucket
                    success, s3_url = self._upload_image_to_s3(modelscope_url, username)
                    
                    with self.lock:
                        # Calculate elapsed time
                        started_at = self.active_inferences[username][space_id].get('started_at')
                        elapsed = 0
                        if started_at:
                            elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
                        
                        if success and s3_url:
                            self.active_inferences[username][space_id]['status'] = 'success'
                            self.active_inferences[username][space_id]['result_url'] = s3_url
                            self.active_inferences[username][space_id]['completed_at'] = datetime.now(timezone.utc)
                            self.active_inferences[username][space_id]['elapsed_seconds'] = round(elapsed, 2)
                            logger.info(f"[ModelScope] Success for user {username} in {elapsed:.2f}s, S3 URL: {s3_url}")
                        else:
                            # S3 upload failed - mark as failed
                            self.active_inferences[username][space_id]['status'] = 'failed'
                            self.active_inferences[username][space_id]['elapsed_seconds'] = round(elapsed, 2)
                            logger.error(f"[ModelScope] S3 upload failed for user {username}")
                else:
                    with self.lock:
                        self.active_inferences[username][space_id]['status'] = 'failed'
                        logger.error(f"[ModelScope] No output images for task {task_id}")
                        
            elif task_status == "FAILED":
                error_msg = data.get("error", "Generation failed")
                with self.lock:
                    self.active_inferences[username][space_id]['status'] = 'failed'
                # Only log to terminal, don't store error for frontend
                logger.error(f"[ModelScope] Task {task_id} failed: {error_msg}")
            # else: still processing
            
            with self.lock:
                return self.active_inferences[username][space_id].copy()
                
        except Exception as e:
            # Log error to terminal only
            logger.error(f"[ModelScope] Poll error for task {task_id}: {e}")
            return inference
    
    def clear_inference(self, username, space_id):
        """Clear completed or failed inference from tracking."""
        with self.lock:
            if username in self.active_inferences:
                self.active_inferences[username].pop(space_id, None)
                if not self.active_inferences[username]:
                    del self.active_inferences[username]


# Global manager instance
modelscope_manager = ModelScopeInferenceManager()
