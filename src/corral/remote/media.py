"""Phone image previews: resolve an image a message refers to and shrink it.

The phone never downloads an original large file into the conversation. It asks
for ``media.image`` with a long-edge pixel limit and JPEG quality chosen from its
own network path; this module turns the reference into a bounded preview.

Encoder order: Pillow (``corral[remote]`` extra) → macOS ``sips`` → pass the
original through only when it already fits the byte budget. Anything else is
reported as unavailable instead of shipping an oversized frame.
"""

from __future__ import annotations

import base64
import hashlib
import io
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import threading
import urllib.parse
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

IMAGE_EXTENSIONS = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".heif", ".bmp", ".tif", ".tiff"}
)

MIN_PX = 64
MAX_PX = 4096
DEFAULT_PX = 1080
MIN_QUALITY = 30
MAX_QUALITY = 95
DEFAULT_QUALITY = 70

_MAX_SOURCE_BYTES = 40 * 1024 * 1024
_MAX_URL_BYTES = 25 * 1024 * 1024
_URL_TIMEOUT = 6.0
# Frames cap uncompressed payloads at 4 MiB; base64 adds a third.
_HARD_BUDGET = 2_400_000
_CACHE_ENTRIES = 48


class MediaError(Exception):
    """``code`` is ``not_found`` or ``unavailable``; callers map it to the wire error."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Preview:
    mime: str
    data: bytes
    width: int
    height: int
    source_width: int
    source_height: int
    source_bytes: int

    def to_wire(self) -> dict:
        return {
            "mime": self.mime,
            "data": base64.b64encode(self.data).decode("ascii"),
            "width": self.width,
            "height": self.height,
            "source_width": self.source_width,
            "source_height": self.source_height,
            "bytes": len(self.data),
            "source_bytes": self.source_bytes,
        }


def clamp_px(value: int | None) -> int:
    if not value:
        return DEFAULT_PX
    return max(MIN_PX, min(MAX_PX, int(value)))


def clamp_quality(value: int | None) -> int:
    if not value:
        return DEFAULT_QUALITY
    return max(MIN_QUALITY, min(MAX_QUALITY, int(value)))


def budget_for(max_px: int) -> int:
    """Byte budget scales with the requested area; low tiers stay small."""
    return max(96_000, min(_HARD_BUDGET, int(max_px * max_px * 0.35)))


def normalize_ref(ref: str) -> str:
    text = (ref or "").strip()
    if text.startswith("<") and text.endswith(">"):
        text = text[1:-1].strip()
    return text


def is_url(ref: str) -> bool:
    scheme = urllib.parse.urlsplit(ref).scheme.lower()
    return scheme in ("http", "https")


def resolve_local(ref: str, cwd: str | None) -> Path:
    """Map a path-like reference to an existing image file or raise ``not_found``."""
    text = ref
    if text.lower().startswith("file://"):
        text = urllib.parse.unquote(urllib.parse.urlsplit(text).path)
    path = Path(os.path.expanduser(text))
    if not path.is_absolute():
        if not cwd:
            raise MediaError("not_found")
        path = Path(os.path.expanduser(cwd)) / path
    if path.suffix.lower() not in IMAGE_EXTENSIONS:
        raise MediaError("unavailable")
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise MediaError("not_found") from exc
    if not resolved.is_file():
        raise MediaError("not_found")
    return resolved


def sniff_mime(raw: bytes) -> str | None:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    if raw[4:8] == b"ftyp" and raw[8:12] in (b"heic", b"heix", b"heif", b"mif1", b"msf1", b"hevc"):
        return "image/heic"
    if raw.startswith(b"BM"):
        return "image/bmp"
    if raw[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"
    return None


class PreviewService:
    """Resolves references and caches encoded previews per source + tier."""

    def __init__(self, *, fetch=None, encoders=None) -> None:
        self._cache: OrderedDict[tuple, Preview] = OrderedDict()
        self._lock = threading.Lock()
        self._fetch = fetch or _fetch_url
        self._encoders = encoders if encoders is not None else (_encode_pillow, _encode_sips)

    def preview(self, ref: str, *, cwd: str | None, max_px: int, quality: int) -> Preview:
        ref = normalize_ref(ref)
        if not ref:
            raise MediaError("not_found")
        max_px = clamp_px(max_px)
        quality = clamp_quality(quality)
        if is_url(ref):
            identity: tuple = ("url", ref)
            cached = self._cached(identity, max_px, quality)
            if cached is not None:
                return cached
            raw = self._fetch(ref)
        else:
            path = resolve_local(ref, cwd)
            stat = path.stat()
            if stat.st_size > _MAX_SOURCE_BYTES:
                raise MediaError("unavailable")
            identity = ("file", str(path), stat.st_mtime_ns, stat.st_size)
            cached = self._cached(identity, max_px, quality)
            if cached is not None:
                return cached
            try:
                raw = path.read_bytes()
            except OSError as exc:
                raise MediaError("not_found") from exc
        preview = self.encode(raw, max_px=max_px, quality=quality)
        with self._lock:
            self._cache[(identity, max_px, quality)] = preview
            while len(self._cache) > _CACHE_ENTRIES:
                self._cache.popitem(last=False)
        return preview

    def encode(self, raw: bytes, *, max_px: int, quality: int) -> Preview:
        source_mime = sniff_mime(raw)
        if source_mime is None:
            raise MediaError("unavailable")
        budget = budget_for(max_px)
        for encoder in self._encoders:
            try:
                result = encoder(raw, max_px, quality, budget)
            except Exception:
                result = None
            if result is not None and len(result.data) <= _HARD_BUDGET:
                return result
        if len(raw) <= budget and source_mime != "image/heic":
            return Preview(source_mime, raw, 0, 0, 0, 0, len(raw))
        raise MediaError("unavailable")

    def _cached(self, identity: tuple, max_px: int, quality: int) -> Preview | None:
        with self._lock:
            key = (identity, max_px, quality)
            hit = self._cache.get(key)
            if hit is not None:
                self._cache.move_to_end(key)
            return hit


def _fetch_url(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "corral-remote"})
    try:
        with urllib.request.urlopen(request, timeout=_URL_TIMEOUT) as response:  # noqa: S310 - http(s) only
            raw = response.read(_MAX_URL_BYTES + 1)
    except Exception as exc:
        raise MediaError("not_found") from exc
    if len(raw) > _MAX_URL_BYTES:
        raise MediaError("unavailable")
    return raw


def _encode_pillow(raw: bytes, max_px: int, quality: int, budget: int) -> Preview | None:
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return None
    with Image.open(io.BytesIO(raw)) as opened:
        opened.seek(0)
        image = ImageOps.exif_transpose(opened)
        source_width, source_height = image.size
        image.thumbnail((max_px, max_px), Image.Resampling.LANCZOS)
        has_alpha = image.mode in ("RGBA", "LA") or (
            image.mode == "P" and "transparency" in image.info
        )
        if has_alpha:
            rgba = image.convert("RGBA")
            buffer = io.BytesIO()
            rgba.save(buffer, format="PNG", optimize=True)
            if buffer.tell() <= budget:
                return Preview(
                    "image/png", buffer.getvalue(), *rgba.size, source_width, source_height, len(raw)
                )
            flattened = Image.new("RGB", rgba.size, (255, 255, 255))
            flattened.paste(rgba, mask=rgba.getchannel("A"))
            image = flattened
        else:
            image = image.convert("RGB")
        step = quality
        while True:
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=step, optimize=True, progressive=True)
            if buffer.tell() <= budget or step <= MIN_QUALITY:
                return Preview(
                    "image/jpeg", buffer.getvalue(), *image.size, source_width, source_height, len(raw)
                )
            step = max(MIN_QUALITY, step - 12)


def _encode_sips(raw: bytes, max_px: int, quality: int, budget: int) -> Preview | None:
    sips = shutil.which("sips")
    if sips is None:
        return None
    with tempfile.TemporaryDirectory(prefix="corral-media-") as work:
        source = Path(work) / "source"
        target = Path(work) / "preview.jpg"
        source.write_bytes(raw)
        width, height = _sips_size(sips, source)
        if not width or not height:
            return None
        for step in dict.fromkeys((quality, max(MIN_QUALITY, quality - 20))):
            command = [sips, "-s", "format", "jpeg", "-s", "formatOptions", str(step)]
            if max(width, height) > max_px:
                command += ["-Z", str(max_px)]
            command += [str(source), "--out", str(target)]
            done = subprocess.run(command, capture_output=True, timeout=20, check=False)
            if done.returncode != 0 or not target.exists():
                return None
            data = target.read_bytes()
            if len(data) <= budget:
                out_width, out_height = _sips_size(sips, target)
                return Preview("image/jpeg", data, out_width, out_height, width, height, len(raw))
        return None


def _sips_size(sips: str, path: Path) -> tuple[int, int]:
    done = subprocess.run(
        [sips, "-g", "pixelWidth", "-g", "pixelHeight", str(path)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    values: dict[str, int] = {}
    for line in done.stdout.splitlines():
        name, _, value = line.strip().partition(":")
        if name in ("pixelWidth", "pixelHeight"):
            try:
                values[name] = int(value.strip())
            except ValueError:
                return 0, 0
    return values.get("pixelWidth", 0), values.get("pixelHeight", 0)


# --- Linked file downloads (media.file) -----------------------------------

MAX_FILE_BYTES = 20 * 1024 * 1024
DEFAULT_FILE_SLICE_BYTES = 1_572_864
MAX_FILE_SLICE_BYTES = 2_097_152

_LOCATION_HASH_RE = re.compile(r"#.*$")
_LOCATION_LINE_RE = re.compile(r":\d+(?::\d+)?$")

_EXT_MIME_OVERRIDES = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".mdx": "text/markdown",
    ".txt": "text/plain",
    ".log": "text/plain",
    ".json": "application/json",
    ".jsonl": "application/x-ndjson",
    ".yaml": "text/yaml",
    ".yml": "text/yaml",
    ".toml": "application/toml",
    ".py": "text/x-python",
    ".swift": "text/x-swift",
    ".ts": "text/typescript",
    ".tsx": "text/typescript",
    ".js": "text/javascript",
    ".jsx": "text/javascript",
    ".sh": "text/x-shellscript",
    ".zsh": "text/x-shellscript",
    ".pdf": "application/pdf",
}


class FileError(Exception):
    """``code`` is ``not_found`` or ``unavailable``; ``reason`` refines the cause."""

    def __init__(self, code: str, reason: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.reason = reason or code


def containment_candidates(ref: str) -> list[str]:
    """Candidate literal substrings that a message text may hold for ``ref``."""
    raw = normalize_ref(ref)
    if not raw:
        return []
    candidates: list[str] = []

    def add(value: str) -> None:
        cleaned = value.strip()
        if cleaned and cleaned not in candidates:
            candidates.append(cleaned)

    add(raw)
    unquoted = urllib.parse.unquote(raw)
    add(unquoted)
    for item in (raw, unquoted):
        no_hash = _LOCATION_HASH_RE.sub("", item)
        add(no_hash)
        add(_LOCATION_LINE_RE.sub("", no_hash))
        if item.lower().startswith("file://"):
            path_part = urllib.parse.urlsplit(item).path
            add(path_part)
            add(urllib.parse.unquote(path_part))
            no_line = _LOCATION_LINE_RE.sub("", urllib.parse.unquote(path_part))
            add(no_line)
    return candidates


def _strip_file_location(text: str) -> str:
    """Strip ``file://``, URL fragments, and optional ``:line[:col]`` suffixes."""
    cleaned = text
    if cleaned.lower().startswith("file://"):
        cleaned = urllib.parse.unquote(urllib.parse.urlsplit(cleaned).path)
    else:
        cleaned = urllib.parse.unquote(_LOCATION_HASH_RE.sub("", cleaned))
    candidate = Path(os.path.expanduser(cleaned))
    if not candidate.exists():
        stripped = _LOCATION_LINE_RE.sub("", cleaned)
        if stripped != cleaned:
            cleaned = stripped
    return cleaned


def resolve_local_file(ref: str, cwd: str | None) -> Path:
    """Map a file link reference to a regular file or raise ``FileError``."""
    text = normalize_ref(ref)
    if not text or is_url(text):
        raise FileError("not_found" if not text else "unavailable")
    scheme = urllib.parse.urlsplit(text).scheme.lower()
    if scheme and scheme != "file" and not re.match(r"^[a-zA-Z]:[\\/]", text):
        raise FileError("unavailable")
    local = _strip_file_location(text)
    if not local:
        raise FileError("not_found")
    path = Path(os.path.expanduser(local))
    if not path.is_absolute():
        if not cwd:
            raise FileError("not_found")
        path = Path(os.path.expanduser(cwd)) / path
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise FileError("not_found") from exc
    if resolved.is_dir():
        raise FileError("unavailable", "is_directory")
    if not resolved.is_file():
        raise FileError("unavailable")
    return resolved


def guess_file_mime(path: Path, sample: bytes = b"") -> str:
    ext = path.suffix.lower()
    if ext in _EXT_MIME_OVERRIDES:
        return _EXT_MIME_OVERRIDES[ext]
    sniffed = sniff_mime(sample) if sample else None
    if sniffed:
        return sniffed
    guessed, _ = mimetypes.guess_type(path.name)
    if guessed:
        return guessed
    return "application/octet-stream"


def display_host_path(path: Path) -> str:
    home = str(Path.home())
    text = str(path)
    if text == home:
        return "~"
    if text.startswith(home + os.sep):
        return "~" + text[len(home) :]
    return text


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(256 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def read_file_slice(
    ref: str,
    *,
    cwd: str | None,
    offset: int = 0,
    max_bytes: int | None = None,
) -> dict:
    """Read one bounded slice of a message-referenced regular file."""
    resolved = resolve_local_file(ref, cwd)
    try:
        stat = resolved.stat()
    except OSError as exc:
        raise FileError("not_found") from exc
    size = int(stat.st_size)
    if size > MAX_FILE_BYTES:
        raise FileError("unavailable", "too_large")
    start = max(0, int(offset))
    limit = DEFAULT_FILE_SLICE_BYTES if max_bytes is None else max(1, min(MAX_FILE_SLICE_BYTES, int(max_bytes)))
    try:
        digest = _file_sha256(resolved)
        with resolved.open("rb") as handle:
            sample = handle.read(64)
            handle.seek(min(start, size))
            chunk = handle.read(limit)
    except OSError as exc:
        raise FileError("not_found") from exc
    return {
        "name": resolved.name,
        "display_path": display_host_path(resolved),
        "mime": guess_file_mime(resolved, sample),
        "size": size,
        "offset": min(start, size),
        "data": base64.b64encode(chunk).decode("ascii"),
        "eof": min(start, size) + len(chunk) >= size,
        "sha256": digest,
    }
