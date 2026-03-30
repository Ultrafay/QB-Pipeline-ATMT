"""
Google Drive Watcher Service.
Lists new files in a Drive folder, downloads them, and moves them to subfolders.
"""
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from pathlib import Path
from typing import List, Dict, Optional
import io
import os


class GoogleDriveWatcher:
    SCOPES = [
        'https://www.googleapis.com/auth/drive',
        'https://www.googleapis.com/auth/spreadsheets'
    ]
    SUPPORTED_MIME_TYPES = [
        'application/pdf',
        'image/jpeg',
        'image/png',
    ]

    def __init__(self, credentials_path: str, folder_id: str):
        if not folder_id:
            raise ValueError("GOOGLE_DRIVE_FOLDER_ID is required")

        self.folder_id = folder_id
        self.creds = service_account.Credentials.from_service_account_file(
            credentials_path, scopes=self.SCOPES
        )
        self.service = build('drive', 'v3', credentials=self.creds)

        # Cache subfolder IDs
        self._processed_folder_id: Optional[str] = None
        self._failed_folder_id: Optional[str] = None

    # ── Public API ──────────────────────────────────────────────

    def list_new_files(self) -> List[Dict]:
        """List files directly in the watched folder (not in subfolders)."""
        mime_filter = " or ".join(
            f"mimeType='{m}'" for m in self.SUPPORTED_MIME_TYPES
        )
        query = (
            f"'{self.folder_id}' in parents "
            f"and ({mime_filter}) "
            f"and trashed=false"
        )
        results = self.service.files().list(
            q=query,
            fields="files(id, name, mimeType, createdTime)",
            orderBy="createdTime",
            pageSize=50
        ).execute()
        return results.get('files', [])

    def download_file(self, file_id: str, dest_path: str) -> str:
        """Download a Drive file to a local path. Returns the path."""
        request = self.service.files().get_media(fileId=file_id)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)

        done = False
        while not done:
            _, done = downloader.next_chunk()

        with open(dest_path, 'wb') as f:
            f.write(fh.getvalue())

        return dest_path

    def move_to_processed(self, file_id: str):
        """Move a file into the 'Processed' subfolder."""
        target = self._ensure_subfolder("Processed")
        self._move_file(file_id, target)

    def move_to_failed(self, file_id: str):
        """Move a file into the 'Failed' subfolder."""
        target = self._ensure_subfolder("Failed")
        self._move_file(file_id, target)

    # ── Internals ───────────────────────────────────────────────

    def _move_file(self, file_id: str, target_folder_id: str):
        """Move file from watched folder to target subfolder."""
        # Get current parent
        file = self.service.files().get(
            fileId=file_id, fields='parents'
        ).execute()
        previous_parents = ",".join(file.get('parents', []))

        self.service.files().update(
            fileId=file_id,
            addParents=target_folder_id,
            removeParents=previous_parents,
            fields='id, parents'
        ).execute()

    def _ensure_subfolder(self, name: str) -> str:
        """Get or create a subfolder inside the watched folder."""
        # Check cache
        if name == "Processed" and self._processed_folder_id:
            return self._processed_folder_id
        if name == "Failed" and self._failed_folder_id:
            return self._failed_folder_id

        # Search for existing
        query = (
            f"'{self.folder_id}' in parents "
            f"and name='{name}' "
            f"and mimeType='application/vnd.google-apps.folder' "
            f"and trashed=false"
        )
        results = self.service.files().list(
            q=query, fields="files(id)"
        ).execute()
        files = results.get('files', [])

        if files:
            folder_id = files[0]['id']
        else:
            # Create subfolder
            metadata = {
                'name': name,
                'mimeType': 'application/vnd.google-apps.folder',
                'parents': [self.folder_id]
            }
            folder = self.service.files().create(
                body=metadata, fields='id'
            ).execute()
            folder_id = folder['id']
            print(f"[DriveWatcher] Created '{name}' subfolder: {folder_id}")

        # Cache
        if name == "Processed":
            self._processed_folder_id = folder_id
        elif name == "Failed":
            self._failed_folder_id = folder_id

        return folder_id
