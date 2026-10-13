from typing import List, Optional

from ingestion.loader.drive import _build_drive_service


class GoogleDriveConnectionService:
    def test_connection(self, folder_ids: List[str], credentials: Optional[dict] = None) -> dict:
        folder_ids = [f.strip() for f in folder_ids if f and f.strip()]
        if not folder_ids:
            raise ValueError("At least one Google Drive folder ID is required.")

        # Same credential factory used by the actual loader.
        service = _build_drive_service(credentials or {})
        folder_id = folder_ids[0]
        try:
            folder = service.files().get(
                fileId=folder_id,
                fields="id,name,mimeType",
                supportsAllDrives=True,
            ).execute()
        except Exception as exc:
            raise ConnectionError(
                f"Google Drive folder access test failed: {type(exc).__name__}"
            ) from exc

        if folder.get("mimeType") != "application/vnd.google-apps.folder":
            raise ValueError("Provided Google Drive ID is not a folder")

        return {"status": "success", "folder_id": folder_id, "folder_name": folder.get("name", "")}


google_drive_connection_service = GoogleDriveConnectionService()
