"""Offline stub of telegram.constants — only the members this project uses."""


class FileSizeLimit:
    # Hard Bot API ceiling on what a bot may download. Mirrors the real value so the
    # size guard is tested against the number that actually applies in production.
    FILESIZE_DOWNLOAD = 20_000_000
    FILESIZE_UPLOAD = 50_000_000
