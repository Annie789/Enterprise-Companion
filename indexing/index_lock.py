# indexing/index_lock.py
#
# Simple per-user threading lock for concurrent
# synchronization/query safety.
#
# For the hackathon, we use a simple in-process
# threading.Lock per user_id. This prevents concurrent
# sync operations from corrupting the same user's index.
#
# In production, this would be replaced with a distributed
# lock (e.g. Redis, etcd) but the interface stays the same.

import threading
from contextlib import contextmanager
from typing import Dict, Generator


class IndexLockManager:
    """
    Manages per-user locks to prevent concurrent
    index modifications from causing corruption.
    """

    def __init__(self):
        # RLock (reentrant) allows the same thread to acquire
        # the lock multiple times without deadlocking. This is
        # essential because add_nodes_to_index calls create_index,
        # both of which acquire the per-user lock.
        self._locks: Dict[str, threading.RLock] = {}
        self._meta = threading.Lock()

    def get_lock(self, user_id: str) -> threading.RLock:
        """Get or create a lock for the given user_id."""
        with self._meta:
            if user_id not in self._locks:
                self._locks[user_id] = threading.RLock()
            return self._locks[user_id]

    @contextmanager
    def acquire(self, user_id: str) -> Generator[None, None, None]:
        """
        Context manager: acquire the per-user lock.

        Usage:
            with index_lock_manager.acquire(user_id):
                # modify index
        """
        lock = self.get_lock(user_id)
        lock.acquire()
        try:
            yield
        finally:
            lock.release()


# Singleton
index_lock_manager = IndexLockManager()
