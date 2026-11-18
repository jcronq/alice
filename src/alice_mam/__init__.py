"""alice_mam — MyAnonamouse (MAM) private-tracker integration.

Slice 1 is read-only: a JSON-Search API wrapper that returns
normalised :class:`alice_mam.search.Candidate` rows for a free-text
query.

Slice 2 layers acquisition + delivery on top:
  * :mod:`alice_mam.acquire` — .torrent fetch + seedbox handoff.
    SEEDBOX_HOST + FORBIDDEN_DESTINATIONS enforce that private-tracker
    torrents NEVER reach local qBit.
  * :mod:`alice_mam.deliver` — wait for rclone, extract EPUB,
    ``calibredb add``, then SMTP to Kindle using calibre-web's
    Fernet-decrypted creds. CLI: ``python -m alice_mam.deliver``.
"""

from alice_mam.acquire import (
    FORBIDDEN_DESTINATIONS,
    SEEDBOX_HOST,
    TorrentFetchError,
    download_torrent_bytes,
    load_cookie_from_secrets,
    send_to_seedbox,
    send_to_seedbox_via_rclone,
)
from alice_mam.search import (
    Candidate,
    load_mam_cookie,
    pick_candidate,
    search_mam,
)

__all__ = [
    "Candidate",
    "FORBIDDEN_DESTINATIONS",
    "SEEDBOX_HOST",
    "TorrentFetchError",
    "download_torrent_bytes",
    "load_cookie_from_secrets",
    "load_mam_cookie",
    "pick_candidate",
    "search_mam",
    "send_to_seedbox",
    "send_to_seedbox_via_rclone",
]
