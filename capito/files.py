"""Local files for requests: multipart parts and readable-path checks."""
from dataclasses import dataclass
from pathlib import Path
import mimetypes


@dataclass(frozen=True, repr=False)
class File:
    """A multipart file part: a local path (relative to the working directory) or bytes.

    The filename defaults to the path's name and the type is guessed from it; both can be
    set explicitly, e.g. to send content that does not match its declared type.
    """
    source: str | Path | bytes
    filename: str | None = None
    content_type: str | None = None

    def __post_init__(self):
        if isinstance(self.source, bytes):
            if not self.filename:
                raise ValueError('File from bytes needs an explicit filename')
        elif not isinstance(self.source, (str, Path)):
            raise TypeError('File source must be a path or bytes')

    @property
    def name(self):
        return self.filename or Path(self.source).name

    @property
    def media_type(self):
        return self.content_type or mimetypes.guess_type(self.name)[0] or 'application/octet-stream'

    def read(self):
        return self.source if isinstance(self.source, bytes) else Path(self.source).read_bytes()

    def describe(self):
        source = f'<{len(self.source)} bytes>' if isinstance(self.source, bytes) else str(self.source)
        return {'file': source, 'filename': self.name, 'content_type': self.media_type}

    def __repr__(self):
        source = f'<{len(self.source)} bytes>' if isinstance(self.source, bytes) else repr(str(self.source))
        extra = ''.join(f', {k}={v!r}' for k, v in (('filename', self.filename), ('content_type', self.content_type)) if v)
        return f'File({source}{extra})'


def readable_file(value, *, base=None):
    """Resolve an existing readable file; relative paths use base or the working directory."""
    path = Path(value).expanduser()
    if base is not None and not path.is_absolute():
        path = Path(base) / path
    path = path.resolve()
    if not path.is_file():
        raise ValueError(f'Expected an existing file: {path}')
    try:
        with path.open('rb') as stream:
            stream.read(1)
    except OSError as exc:
        raise ValueError(f'Cannot read file: {path}') from exc
    return path
