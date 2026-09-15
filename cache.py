"""
Persistent cache (a JSON file) of:
- video details, so a link sent again shows its buttons without a new request
- files already uploaded to the relay channel, so they can be re-sent instantly

Methods are thread-safe. Reads never wait for the disk: writes serialize the
state under the lock, then write the file outside it (the bot calls the
writing methods in a thread).
"""

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class Store:
    def __init__(self, path: Path, relay_channel_id: int, max_files: int, video_ttl: int, file_ttl: int):
        self._path = path
        self._relay_channel_id = relay_channel_id
        self._max_files = max_files
        self._video_ttl = video_ttl
        self._file_ttl = file_ttl
        self._lock = threading.RLock()
        self._videos: dict[str, dict] = {}
        self._aliases: dict[str, str] = {}
        # Insertion order doubles as recency: oldest first, evicted first
        self._files: dict[str, dict] = {}
        # One disk write at a time, and never an older state over a newer one
        self._write_lock = threading.Lock()
        self._version = 0
        self._written_version = 0

    def load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            logger.warning("Ignoring unreadable cache file %s: %s", self._path, e)
            return
        with self._lock:
            self._videos = data.get("videos") or {}
            self._aliases = data.get("aliases") or {}
            self._files = data.get("files") or {}
            if self._files and data.get("relay_channel_id") != self._relay_channel_id:
                # Message IDs only mean something in the channel they were posted to
                logger.warning("Relay channel changed: forgetting %d cached files", len(self._files))
                self._files = {}
            self._prune_videos()
            logger.info("Cache loaded: %d videos, %d files", len(self._videos), len(self._files))

    # -- video details ------------------------------------------------------

    def video(self, key: str) -> Optional[dict]:
        """Cached video details for a key (or an alias of it), if still fresh."""
        with self._lock:
            entry = self._videos.get(self._aliases.get(key, key))
            if entry and time.time() - entry["saved"] < self._video_ttl:
                return entry["info"]
            return None

    def put_video(self, info: dict, alias: Optional[str] = None) -> None:
        """Remember video details. `alias` is another key the same video was reached by."""
        with self._lock:
            key = info["key"]
            self._videos[key] = {"info": info, "saved": time.time()}
            if alias and alias != key:
                self._aliases[alias] = key
            self._prune_videos()
            snapshot = self._snapshot()
        self._write(snapshot)

    # -- uploaded files -----------------------------------------------------

    def file(self, key: str, label: str) -> Optional[int]:
        """
        Relay channel message ID of a cached file, if fresh. An expired entry is
        left in place: the new upload replaces it and put_file() returns the old
        message for deletion.
        """
        with self._lock:
            name = f"{key}|{label}"
            entry = self._files.get(name)
            if entry is None or time.time() - entry["saved"] >= self._file_ttl:
                return None
            self._files[name] = self._files.pop(name)  # recently used (saved with the next write)
            return entry["message_id"]

    def put_file(self, key: str, label: str, message_id: int) -> list[int]:
        """
        Remember an uploaded file. Returns relay message IDs that are no longer
        cached — an entry this one replaced, and entries pushed out by the size
        limit. The caller should delete those messages. With caching disabled
        (max_files = 0) the new message itself is returned.
        """
        with self._lock:
            name = f"{key}|{label}"
            evicted = []
            old = self._files.pop(name, None)
            if old is not None and old["message_id"] != message_id:
                evicted.append(old["message_id"])
            self._files[name] = {"message_id": message_id, "saved": time.time()}
            while len(self._files) > self._max_files:
                evicted.append(self._files.pop(next(iter(self._files)))["message_id"])
            snapshot = self._snapshot()
        self._write(snapshot)
        return evicted

    def drop_file(self, key: str, label: str, message_id: int) -> None:
        """Forget a cached file whose relay message is gone — unless it was already replaced."""
        with self._lock:
            name = f"{key}|{label}"
            entry = self._files.get(name)
            if entry is None or entry["message_id"] != message_id:
                return
            del self._files[name]
            snapshot = self._snapshot()
        self._write(snapshot)

    # -- internals ----------------------------------------------------------

    def _prune_videos(self) -> None:
        cutoff = time.time() - self._video_ttl
        self._videos = {k: v for k, v in self._videos.items() if v["saved"] >= cutoff}
        self._aliases = {a: k for a, k in self._aliases.items() if k in self._videos}

    def _snapshot(self) -> tuple[int, str]:
        """Serialize the current state (call with _lock held)."""
        self._version += 1
        data = {
            "relay_channel_id": self._relay_channel_id,
            "videos": self._videos,
            "aliases": self._aliases,
            "files": self._files,
        }
        return self._version, json.dumps(data, ensure_ascii=False)

    def _write(self, snapshot: tuple[int, str]) -> None:
        version, payload = snapshot
        with self._write_lock:
            if version <= self._written_version:
                return  # a newer state is already on disk
            tmp_path = self._path.with_name(self._path.name + ".tmp")
            try:
                tmp_path.write_text(payload, encoding="utf-8")
                os.chmod(tmp_path, 0o600)
                os.replace(tmp_path, self._path)  # atomic: a crash never leaves half a file
                self._written_version = version
            except OSError as e:
                logger.warning("Could not save cache file %s: %s", self._path, e)
