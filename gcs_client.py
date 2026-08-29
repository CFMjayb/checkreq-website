"""
gcs_client.py — thin Google Cloud Storage wrapper for the transient
check-request attachment staging bucket (cfm-checkreq-attachments, project
cfm-qbo-mcp). Auth via Application Default Credentials (ambient service
account, matching every other GCP-native call in this codebase) -- no secret
needed, unlike sharepoint_client.py's Graph client-credentials grant.

The bucket is transient staging only -- the SharePoint archive
(sharepoint_client.py) is the permanent record. See CLAUDE.md for the
lifecycle-rule backstop and the not-yet-wired cleanup trigger.
"""
from __future__ import annotations

from google.cloud import storage

_client: storage.Client | None = None


def _get_client() -> storage.Client:
    global _client
    if _client is None:
        _client = storage.Client()
    return _client


def upload_bytes(bucket_name: str, blob_path: str, data: bytes, content_type: str) -> None:
    bucket = _get_client().bucket(bucket_name)
    blob = bucket.blob(blob_path)
    blob.upload_from_string(data, content_type=content_type)


def download_bytes(bucket_name: str, blob_path: str) -> tuple[bytes, str] | None:
    """Returns (data, content_type), or None if the blob doesn't exist. Used
    by main.py's /org-logo/{org_id} route to stream a diocese-uploaded logo
    back out -- the only current reader of a previously-uploaded blob in
    this codebase (every other caller of this module only ever uploads or
    deletes)."""
    blob = _get_client().bucket(bucket_name).blob(blob_path)
    if not blob.exists():
        return None
    blob.reload()
    return blob.download_as_bytes(), (blob.content_type or "application/octet-stream")


def delete_blob(bucket_name: str, blob_path: str) -> None:
    """Best-effort delete -- used both for cleanup-on-QBO-post (not yet wired,
    see cleanup_gcs_attachment() in main.py) and to unwind a GCS upload that
    succeeded when the paired SharePoint upload subsequently failed."""
    bucket = _get_client().bucket(bucket_name)
    blob = bucket.blob(blob_path)
    blob.delete()
