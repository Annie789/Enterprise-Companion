# connections/manager.py

import uuid
from typing import Dict, List, Optional


class ConnectionManager:
    """
    In-memory connection store for the hackathon
    prototype.

    Production systems should replace this with
    encrypted secret storage.
    """

    def __init__(self):

        self._connections: Dict[
            str,
            dict,
        ] = {}


    def create_connection(
        self,
        user_id: str,
        source: str,
        credentials: dict,
    ) -> str:
        """
        Store source credentials and return
        a connection ID.
        """

        connection_id = str(
            uuid.uuid4()
        )

        self._connections[
            connection_id
        ] = {
            "connection_id": connection_id,
            "user_id": user_id,
            "source": source,
            "credentials": credentials,
            "status": "connected",
        }

        return connection_id


    def get_connection(
        self,
        connection_id: str,
    ) -> Optional[dict]:

        return self._connections.get(
            connection_id
        )


    def get_user_connections(
        self,
        user_id: str,
    ) -> List[dict]:

        return [
            connection
            for connection
            in self._connections.values()
            if (
                connection["user_id"]
                == user_id
            )
        ]


    def delete_connection(
        self,
        connection_id: str,
    ) -> bool:

        if connection_id not in self._connections:

            return False

        del self._connections[
            connection_id
        ]

        return True


connection_manager = ConnectionManager()