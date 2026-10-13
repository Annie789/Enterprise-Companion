# connections/confluence_connection.py

from typing import List
import requests
from requests.auth import HTTPBasicAuth


class ConfluenceConnectionService:

    def test_connection(
        self,
        base_url: str,
        api_token: str,
        email: str,
        space_keys: List[str],
    ) -> dict:

        base_url = base_url.strip().rstrip("/")
        email = email.strip()
        space_keys = [
            key.strip() for key in space_keys if key.strip()
        ]

        if not base_url or not email or not api_token:
            raise ValueError(
                "Confluence URL, email and API token are required."
            )

        if not space_keys:
            raise ValueError(
                "At least one Confluence space key is required."
            )

        test_space = space_keys[0]

        url = (
            f"{base_url}/rest/api/space/{test_space}"
        )

        try:
            response = requests.get(
                url,
                auth=HTTPBasicAuth(email, api_token),
                headers={"Accept": "application/json"},
                timeout=20,
            )

            response.raise_for_status()
            data = response.json()

        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code

            if status == 401:
                message = "Confluence authentication failed (401)."
            elif status == 403:
                message = "Confluence permission denied (403)."
            elif status == 404:
                message = "Confluence space not found or inaccessible (404)."
            else:
                message = f"Confluence returned HTTP {status}."

            raise ConnectionError(message) from exc

        except requests.exceptions.RequestException as exc:
            raise ConnectionError(
                f"Confluence network connection failed: {type(exc).__name__}"
            ) from exc

        return {
            "status": "success",
            "space_key": data.get("key", test_space),
            "space_name": data.get("name", ""),
            "http_status": response.status_code,
        }


confluence_connection_service = ConfluenceConnectionService()