"""
Aurelius — storage.py
Handles all file storage: local disk (dev) or Cloudflare R2 (production).

Auto-detects based on environment variables:
  - If CLOUDFLARE_R2_ACCESS_KEY is set → use R2
  - Otherwise → use local disk

Usage:
  from storage import storage
  url  = await storage.upload_file(local_path, "audio/chapter1.mp3")
  data = await storage.download_file("audio/chapter1.mp3")
         await storage.delete_file("audio/chapter1.mp3")
  url  = storage.get_url("audio/chapter1.mp3")
"""

import os
import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger("aurelius.storage")


# =============================================================================
#  LOCAL STORAGE (development / fallback)
# =============================================================================

class LocalStorage:
    """Stores files on local disk. Simple, no setup needed."""

    def __init__(self):
        self.base = Path(".")
        log.info("Storage: LOCAL DISK (files stored in output/ and uploads/)")

    def upload_file(self, local_path: str, storage_key: str) -> str:
        """
        For local storage, files are already on disk.
        Just return the local path as the 'URL'.
        """
        return local_path

    def get_url(self, storage_key: str) -> str:
        return storage_key

    def delete_file(self, storage_key: str) -> None:
        path = Path(storage_key)
        if path.exists():
            path.unlink(missing_ok=True)

    def is_cloud(self) -> bool:
        return False


# =============================================================================
#  CLOUDFLARE R2 STORAGE (production)
# =============================================================================

class R2Storage:
    """
    Stores files in Cloudflare R2 (S3-compatible).
    Files persist across server restarts.
    """

    def __init__(self):
        import boto3

        self.bucket    = os.environ["CLOUDFLARE_R2_BUCKET"]
        self.endpoint  = os.environ["CLOUDFLARE_R2_ENDPOINT"]
        self.access_key = os.environ["CLOUDFLARE_R2_ACCESS_KEY"]
        self.secret_key = os.environ["CLOUDFLARE_R2_SECRET_KEY"]

        # Public URL base (set this after enabling public access on your bucket)
        self.public_url = os.environ.get(
            "CLOUDFLARE_R2_PUBLIC_URL",
            f"{self.endpoint}/{self.bucket}"
        )

        self.client = boto3.client(
            "s3",
            endpoint_url          = self.endpoint,
            aws_access_key_id     = self.access_key,
            aws_secret_access_key = self.secret_key,
            region_name           = "auto",
        )
        log.info(f"Storage: CLOUDFLARE R2 (bucket: {self.bucket})")

    def upload_file(self, local_path: str, storage_key: str) -> str:
        """
        Upload a local file to R2. Returns the public URL.
        storage_key is the path inside the bucket e.g. 'audio/book123/ch01.mp3'
        """
        content_type = "audio/mpeg" if local_path.endswith(".mp3") else \
                       "audio/wav"  if local_path.endswith(".wav") else \
                       "application/pdf"

        log.info(f"  Uploading to R2: {storage_key}")
        self.client.upload_file(
            local_path,
            self.bucket,
            storage_key,
            ExtraArgs={"ContentType": content_type}
        )

        # Delete local file after upload to save disk space
        try:
            Path(local_path).unlink(missing_ok=True)
        except Exception:
            pass

        url = f"{self.public_url}/{storage_key}"
        log.info(f"  Uploaded: {url}")
        return url

    def get_url(self, storage_key: str) -> str:
        """Return the public URL for a stored file."""
        if storage_key.startswith("http"):
            return storage_key
        return f"{self.public_url}/{storage_key}"

    def delete_file(self, storage_key: str) -> None:
        """Delete a file from R2."""
        try:
            # Handle both full URLs and storage keys
            key = storage_key
            if storage_key.startswith("http"):
                key = storage_key.split(self.bucket + "/")[-1]
            self.client.delete_object(Bucket=self.bucket, Key=key)
            log.info(f"  Deleted from R2: {key}")
        except Exception as e:
            log.warning(f"  Could not delete {storage_key}: {e}")

    def generate_presigned_url(self, storage_key: str, expires: int = 3600) -> str:
        """Generate a temporary signed URL for private buckets."""
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": storage_key},
            ExpiresIn=expires,
        )

    def is_cloud(self) -> bool:
        return True


# =============================================================================
#  AUTO-DETECT which storage to use
# =============================================================================

def _create_storage():
    required = ["CLOUDFLARE_R2_ACCESS_KEY", "CLOUDFLARE_R2_SECRET_KEY",
                "CLOUDFLARE_R2_BUCKET", "CLOUDFLARE_R2_ENDPOINT"]
    if all(os.environ.get(k) for k in required):
        try:
            return R2Storage()
        except Exception as e:
            log.error(f"R2 setup failed: {e} — falling back to local storage")
            return LocalStorage()
    return LocalStorage()


# Singleton — import this everywhere
# Wrapped in try/except so a bad config never crashes the whole app on startup
try:
    storage = _create_storage()
except Exception as e:
    log.error(f"Storage initialization failed: {e} — using local storage")
    storage = LocalStorage()
