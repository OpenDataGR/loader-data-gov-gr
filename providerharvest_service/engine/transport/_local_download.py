"""Shared helper for transports that can't offer a true streamed,
random-access read the way SFTP's file handle does -- SCP and FTP are
both whole-file push/pull protocols, not seek/chunk ones. Both download
to a throwaway local temp file and expose it through this real, seekable
file object, deleting it the moment the caller is done.

Seekability specifically matters, not just as a nicety: CKAN's own
resource uploader (``ckan.lib.uploader``) seeks to the end of the given
stream to compute the file size before copying it into storage, then
seeks back to the start -- confirmed by a real CI failure the first time
ScpTransport's temp-file handle didn't implement ``seek()``
("'_TempFileHandle' object has no attribute 'seek'"). A raw network
socket (what a truly zero-copy FTP stream would have to be) fundamentally
cannot support that, which is why FTPTransport downloads first too rather
than streaming the data connection directly, despite that meaning an
extra local disk round-trip.
"""

from __future__ import annotations

import os


class TempFileHandle:
    def __init__(self, path: str):
        self._path = path
        self._fh = open(path, "rb")

    def read(self, size: int = -1) -> bytes:
        return self._fh.read(size)

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._fh.seek(offset, whence)

    def tell(self) -> int:
        return self._fh.tell()

    def close(self) -> None:
        try:
            self._fh.close()
        finally:
            try:
                os.remove(self._path)
            except OSError:
                pass

    def __enter__(self) -> "TempFileHandle":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
