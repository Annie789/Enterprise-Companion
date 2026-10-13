from typing import List


class JiraConnectionService:

    def test_connection(
        self,
        server_url: str,
        email: str,
        api_token: str,
        project_keys: List[str],
    ) -> dict:

        if not server_url.strip():
            raise ValueError(
                "Jira server URL is required."
            )

        if not email.strip():
            raise ValueError(
                "Jira email is required."
            )

        if not api_token.strip():
            raise ValueError(
                "Jira API token is required."
            )

        if not project_keys:
            raise ValueError(
                "At least one Jira project key is required."
            )

        from llama_index.readers.jira import JiraReader

        reader = JiraReader(
            email=email,
            api_token=api_token,
            server_url=server_url,
        )

        test_project = project_keys[0]

        try:

            documents = reader.load_data(
                query=f"project = {test_project}"
            )

        except Exception as exc:

            raise ConnectionError(
                "Unable to connect to Jira."
            ) from exc

        return {
            "status": "success",
            "project_key": test_project,
            "documents_found": len(documents),
        }


jira_connection_service = (
    JiraConnectionService()
)