"""Experimental, offline-only validation of attribution metadata and assets.

All names and preferred text are untrusted plain text. No network requests are
made; website, license and source URLs are identifiers, never fetched here.
"""
from __future__ import annotations

import json
import re
import struct
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit
import zlib

from jsonschema import Draft202012Validator

KEY = 'org.vl-exchange.attribution'
SCHEMA = Path(__file__).with_name('attribution.schema.json')
MAX_PNG_BYTES = 256 * 1024
MAX_DIMENSION = 1024


class AttributionError(ValueError):
    pass


def _url(value):
    # Reject ambiguous URLs as well as credentials. URLs are metadata only.
    if not isinstance(value, str) or any(ord(c) < 33 or ord(c) == 127 or c == '\\' for c in value):
        raise AttributionError('unsafe_url')
    try:
        u = urlsplit(value)
        if u.scheme != 'https' or not u.hostname or u.username is not None or u.password is not None or u.port not in (None, 443):
            raise AttributionError('unsafe_url')
        if not re.fullmatch(r'[A-Za-z0-9.-]+', u.hostname) or '..' in u.hostname or u.hostname.startswith('.') or u.hostname.endswith('.'):
            raise AttributionError('unsafe_url')
    except ValueError as exc:
        raise AttributionError('unsafe_url') from exc


def _logo(root, name, files):
    if not isinstance(name, str) or name not in files or not re.fullmatch(r'[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*[.]png', name):
        raise AttributionError('logo_not_in_inventory_or_unsafe_path')
    parts = name.split('/')
    if PurePosixPath(name).is_absolute() or any(p in ('', '.', '..') for p in parts):
        raise AttributionError('unsafe_logo_path')
    root = Path(root).resolve(strict=True)
    path = root
    for part in parts:
        path = path / part
        if path.is_symlink():
            raise AttributionError('unsafe_logo_path')
    if not path.is_file() or not path.resolve().is_relative_to(root):
        raise AttributionError('logo_missing')
    if path.stat().st_size > MAX_PNG_BYTES:
        raise AttributionError('logo_too_large')
    _png(path.read_bytes())


def _png(raw):
    """Accept only a bounded, non-interlaced 8-bit PNG; reject extra chunks.

    This is deliberately narrower than PNG. Decompression and filters are
    validated under strict bounds; the host still needs a safe image decoder.
    """
    if len(raw) > MAX_PNG_BYTES or not raw.startswith(b'\x89PNG\r\n\x1a\n'):
        raise AttributionError('invalid_png')
    pos = 8
    parts = []
    while pos < len(raw):
        if pos + 12 > len(raw): raise AttributionError('invalid_png')
        length = struct.unpack_from('>I', raw, pos)[0]
        end = pos + 12 + length
        if end > len(raw): raise AttributionError('invalid_png')
        kind, content = raw[pos+4:pos+8], raw[pos+8:pos+8+length]
        crc = struct.unpack_from('>I', raw, pos+8+length)[0]
        if zlib.crc32(kind + content) & 0xffffffff != crc: raise AttributionError('invalid_png_crc')
        parts.append((kind, content)); pos = end
    if len(parts) < 3 or parts[0][0] != b'IHDR' or parts[-1] != (b'IEND', b'') or any(k != b'IDAT' for k, _ in parts[1:-1]):
        raise AttributionError('unsupported_png_chunks')
    if len(parts[0][1]) != 13: raise AttributionError('invalid_png')
    width, height, depth, color, compression, filtering, interlace = struct.unpack('>IIBBBBB', parts[0][1])
    channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(color)
    if not channels or depth != 8 or compression != 0 or filtering != 0 or interlace != 0 or not 1 <= width <= MAX_DIMENSION or not 1 <= height <= MAX_DIMENSION:
        raise AttributionError('unsupported_png')
    row_bytes = 1 + width * channels
    expected = row_bytes * height
    decoder = zlib.decompressobj()
    try:
        pixels = decoder.decompress(b''.join(value for _, value in parts[1:-1]), expected + 1)
    except zlib.error as exc:
        raise AttributionError('invalid_png_data') from exc
    if len(pixels) != expected or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
        raise AttributionError('invalid_png_data')
    if any(pixels[i * row_bytes] > 4 for i in range(height)):
        raise AttributionError('invalid_png_filter')


def validate_attribution(root, manifest):
    """Validate optional extension; return its data, or None if absent.

    The caller must independently validate the full manifest and inventory
    digests, including before trusting any author or image. No signature or
    identity verification is implied by this function.
    """
    extension = manifest.get('extensions', {}).get(KEY)
    if extension is None:
        return None
    if not isinstance(extension, dict) or set(extension) != {'required', 'data'} or extension.get('required') is not False:
        raise AttributionError('invalid_attribution_extension')
    data = extension['data']
    schema = json.loads(SCHEMA.read_text(encoding='utf-8'))
    errors = sorted(Draft202012Validator(schema).iter_errors(data), key=lambda e: e.json_path)
    if errors:
        raise AttributionError(f'invalid_attribution: {errors[0].json_path}: {errors[0].message}')
    if data['package_version'] != manifest.get('version'):
        raise AttributionError('package_version_mismatch')
    if (data['white_label'] == 'prohibited' or 'required' in data['display'].values()) and 'license_url' not in data:
        raise AttributionError('license_url_required')
    ids = [(author['id'], author['role']) for author in data['authors']]
    if len(ids) != len(set(ids)):
        raise AttributionError('duplicate_author')
    for item in [data['publisher'], *data['authors']]:
        if 'website' in item: _url(item['website'])
        if 'logo' in item: _logo(root, item['logo'], manifest['files'])
    if 'source_url' in data.get('provenance', {}): _url(data['provenance']['source_url'])
    if 'license_url' in data: _url(data['license_url'])
    return data
