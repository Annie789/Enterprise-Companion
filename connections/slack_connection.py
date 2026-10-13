from typing import List


class SlackConnectionService:

    def test_connection(
        self,
        bot_token: str,
        channel_ids: List[str],
    ) -> dict:

        if not bot_token.strip():
            raise ValueError(
                "Slack bot token is required."
            )

        if not channel_ids:
            raise ValueError(
                "At least one Slack channel ID is required."
            )

        from llama_index.readers.slack import SlackReader

        reader = SlackReader(
            slack_token=bot_token
        )

        test_channel = channel_ids[0]

        try:

            documents = reader.load_data(
                channel_ids=[
                    test_channel
                ]
            )

        except Exception as exc:

            raise ConnectionError(
                "Unable to connect to Slack."
            ) from exc

        return {
            "status": "success",
            "channel_id": test_channel,
            "documents_found": len(documents),
        }


slack_connection_service = (
    SlackConnectionService()
)