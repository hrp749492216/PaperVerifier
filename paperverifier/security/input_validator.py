"""Comprehensive input validation for all untrusted inputs.

Provides SSRF protection (URL validation with DNS resolution and IP blocking),
path traversal prevention, file upload validation with magic-byte verification,
and GitHub URL sanitization.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import urllib.parse
import uuid
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SAFE_URL_SCHEMES = {"https"}  # Only HTTPS allowed

ALLOWED_FILE_EXTENSIONS = {".pdf", ".docx", ".doc", ".md", ".tex", ".txt", ".bib"}

MAX_FILE_SIZE_BYTES = 100 * 1024 * 1024  # 100 MB
MAX_FILENAME_LENGTH = 255

# Private / reserved IP ranges to block (SSRF protection).
BLOCKED_IP_RANGES = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),  # Link-local / AWS metadata
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),  # Unique local
    ipaddress.ip_network("fe80::/10"),  # Link-local IPv6
    ipaddress.ip_network("::ffff:0:0/96"),  # IPv4-mapped IPv6 (HIGH-S2)
    ipaddress.ip_network("100.64.0.0/10"),  # Carrier-grade NAT
    ipaddress.ip_network("::/128"),  # Unspecified IPv6
    ipaddress.ip_network("2001:db8::/32"),  # Documentation range
    ipaddress.ip_network("ff00::/8"),  # Multicast
    ipaddress.ip_network("2002::/16"),  # 6to4 (can tunnel to internal)
]

# Magic bytes for file-type verification.
MAGIC_BYTES: dict[str, bytes] = {
    ".pdf": b"%PDF",
    ".docx": b"PK",  # ZIP / OOXML header
    ".doc": b"\xd0\xcf\x11\xe0",  # OLE2 compound document
}

# GitHub URL must be exactly https://github.com/{owner}/{repo}.
GITHUB_URL_PATTERN = re.compile(r"^https://github\.com/[\w\-\.]+/[\w\-\.]+/?$")

# Characters allowed in sanitized filenames.
_SAFE_FILENAME_RE = re.compile(r"[^a-zA-Z0-9\-_\.]")

# Control characters (C0 + DEL).
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class InputValidationError(Exception):
    """Raised when any input fails validation."""


# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------


def validate_url(url: str) -> str:
    """Validate a URL for SSRF protection.

    1. Parse *url* and ensure the scheme is ``https``.
    2. Reject URLs containing embedded credentials (``user:pass@host``).
    3. Resolve the hostname via DNS and verify none of the resulting IPs
       fall within :data:`BLOCKED_IP_RANGES`.
    4. Return the validated URL unchanged.

    Raises:
        InputValidationError: On any validation failure.
    """
    if not url or not isinstance(url, str):
        raise InputValidationError("URL must be a non-empty string.")

    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError as exc:
        raise InputValidationError(f"Malformed URL: {exc}") from exc

    # -- Scheme --
    scheme = (parsed.scheme or "").lower()
    if scheme not in SAFE_URL_SCHEMES:
        raise InputValidationError(
            f"URL scheme '{scheme}' is not allowed. Only {SAFE_URL_SCHEMES} permitted."
        )

    # -- Credentials in URL --
    if parsed.username or parsed.password:
        raise InputValidationError("URLs must not contain embedded credentials.")

    # -- Hostname --
    hostname = parsed.hostname
    if not hostname:
        raise InputValidationError("URL must contain a valid hostname.")

    # -- DNS resolution and IP check --
    _check_hostname_ip(hostname)

    logger.debug("url_validated", url=url)
    return url


def resolve_and_validate_url(url: str) -> tuple[str, str]:
    """Validate a URL and return a validated IP for DNS-pinning.

    Performs the same validation as :func:`validate_url` but additionally
    returns one of the resolved IP addresses so the HTTP client can
    connect directly to it (preventing DNS rebinding / TOCTOU attacks).

    Returns:
        A tuple of ``(validated_url, resolved_ip)``.

    Raises:
        InputValidationError: On any validation failure.
    """
    if not url or not isinstance(url, str):
        raise InputValidationError("URL must be a non-empty string.")

    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError as exc:
        raise InputValidationError(f"Malformed URL: {exc}") from exc

    scheme = (parsed.scheme or "").lower()
    if scheme not in SAFE_URL_SCHEMES:
        raise InputValidationError(
            f"URL scheme '{scheme}' is not allowed. Only {SAFE_URL_SCHEMES} permitted."
        )

    if parsed.username or parsed.password:
        raise InputValidationError("URLs must not contain embedded credentials.")

    hostname = parsed.hostname
    if not hostname:
        raise InputValidationError("URL must contain a valid hostname.")

    resolved_ip = _resolve_and_check_ip(hostname)
    logger.debug("url_validated_with_pin", url=url, pinned_ip=resolved_ip)
    return url, resolved_ip


def _resolve_and_check_ip(hostname: str) -> str:
    """Resolve *hostname*, validate all IPs, and return the first safe one."""
    try:
        addr = ipaddress.ip_address(hostname)
        _assert_ip_not_blocked(addr, hostname)
        return str(addr)
    except ValueError:
        pass

    try:
        addrinfos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise InputValidationError(f"DNS resolution failed for '{hostname}': {exc}") from exc

    if not addrinfos:
        raise InputValidationError(f"DNS resolution returned no addresses for '{hostname}'.")

    first_ip: str | None = None
    for _family, _type, _proto, _canonname, sockaddr in addrinfos:
        ip_str = sockaddr[0]
        try:
            addr = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        _assert_ip_not_blocked(addr, hostname)
        if first_ip is None:
            first_ip = ip_str

    if first_ip is None:
        raise InputValidationError(f"DNS resolution returned no usable addresses for '{hostname}'.")

    return first_ip


def _check_hostname_ip(hostname: str) -> None:
    """Resolve *hostname* and verify all addresses are public."""
    try:
        # Attempt to parse directly as an IP literal first.
        addr = ipaddress.ip_address(hostname)
        _assert_ip_not_blocked(addr, hostname)
        return
    except ValueError:
        pass  # Not a literal IP; resolve via DNS.

    try:
        addrinfos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise InputValidationError(f"DNS resolution failed for '{hostname}': {exc}") from exc

    if not addrinfos:
        raise InputValidationError(f"DNS resolution returned no addresses for '{hostname}'.")

    for _family, _type, _proto, _canonname, sockaddr in addrinfos:
        ip_str = sockaddr[0]
        try:
            addr = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        _assert_ip_not_blocked(addr, hostname)


def _assert_ip_not_blocked(
    addr: ipaddress.IPv4Address | ipaddress.IPv6Address, hostname: str
) -> None:
    """Raise if *addr* falls in any blocked range."""
    # Unwrap IPv4-mapped IPv6 addresses (e.g. ::ffff:169.254.169.254) to their
    # IPv4 equivalents so they are checked against IPv4 blocklists (HIGH-S2).
    check_addr = addr
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        check_addr = addr.ipv4_mapped

    for network in BLOCKED_IP_RANGES:
        if check_addr in network:
            logger.warning("ssrf_blocked", hostname=hostname, ip=str(addr), network=str(network))
            raise InputValidationError(
                f"Access to private/reserved IP range is blocked "
                f"(host='{hostname}', ip={addr}, range={network})."
            )


# ---------------------------------------------------------------------------
# GitHub URL validation
# ---------------------------------------------------------------------------


def validate_github_url(url: str) -> str:
    """Validate a GitHub repository URL.

    The URL must match ``https://github.com/{owner}/{repo}`` exactly.
    Query parameters, fragments, and extra path components are rejected.
    A trailing slash and ``.git`` suffix are stripped from the returned value.

    Raises:
        InputValidationError: On any validation failure.
    """
    if not url or not isinstance(url, str):
        raise InputValidationError("GitHub URL must be a non-empty string.")

    # Also validate for SSRF protection (DNS resolution + IP blocking).
    validate_url(url)

    # Strip common suffixes for normalization before pattern matching.
    normalized = url.rstrip("/")
    if normalized.endswith(".git"):
        normalized = normalized[:-4]

    # Re-add trailing slash for pattern match, then strip.
    candidate = normalized + "/"

    if not GITHUB_URL_PATTERN.match(candidate):
        raise InputValidationError(
            f"Invalid GitHub URL. Expected format: "
            f"https://github.com/{{owner}}/{{repo}} -- got: {url}"
        )

    # Reject query params / fragments present in the original URL.
    parsed = urllib.parse.urlparse(url)
    if parsed.query:
        raise InputValidationError("GitHub URL must not contain query parameters.")
    if parsed.fragment:
        raise InputValidationError("GitHub URL must not contain URL fragments.")

    logger.debug("github_url_validated", url=normalized)
    return normalized


# ---------------------------------------------------------------------------
# File upload validation
# ---------------------------------------------------------------------------


def validate_uploaded_file(
    filename: str,
    content: bytes,
    max_size: int = MAX_FILE_SIZE_BYTES,
) -> tuple[str, bytes]:
    """Validate an uploaded file.

    Steps:
    1. Check *content* size against *max_size*.
    2. Sanitize the filename (see :func:`sanitize_filename`).
    3. Verify the extension is in :data:`ALLOWED_FILE_EXTENSIONS`.
    4. For extensions with known magic bytes, verify the content matches.
    5. Return ``(safe_filename, content)``.

    Raises:
        InputValidationError: On any validation failure.
    """
    # -- Size --
    if len(content) > max_size:
        raise InputValidationError(
            f"File too large: {len(content):,} bytes exceeds maximum of {max_size:,} bytes."
        )

    if len(content) == 0:
        raise InputValidationError("Uploaded file is empty.")

    # -- Filename --
    safe_name = sanitize_filename(filename)

    # -- Extension --
    ext = Path(safe_name).suffix.lower()
    if ext not in ALLOWED_FILE_EXTENSIONS:
        raise InputValidationError(
            f"File extension '{ext}' is not allowed. Permitted: {sorted(ALLOWED_FILE_EXTENSIONS)}"
        )

    # -- Magic bytes --
    if not verify_magic_bytes(content, ext):
        raise InputValidationError(
            f"File content does not match expected format for '{ext}'. "
            f"The file may be corrupted or mislabelled."
        )

    logger.info("file_validated", filename=safe_name, size=len(content), extension=ext)
    return safe_name, content


def sanitize_filename(filename: str) -> str:
    """Create a safe filename with a UUID prefix.

    Strips path separators, null bytes, control characters, and any character
    outside ``[a-zA-Z0-9\\-_\\.]``.  The returned name is prefixed with a
    short UUID to avoid collisions::

        sanitize_filename("../../etc/passwd")
        # -> "a1b2c3d4_etcpasswd"

    Raises:
        InputValidationError: If the original filename is empty after
            stripping or the resulting name is too long.
    """
    if not filename or not isinstance(filename, str):
        raise InputValidationError("Filename must be a non-empty string.")

    # Strip path separators -- take only the basename.
    name = filename.replace("\\", "/")
    name = name.split("/")[-1]

    # Remove null bytes and control characters.
    name = name.replace("\x00", "")
    name = _CONTROL_CHARS_RE.sub("", name)

    # Collapse to safe characters only.
    name = _SAFE_FILENAME_RE.sub("", name)

    if not name:
        raise InputValidationError(
            f"Filename '{filename}' contains no usable characters after sanitization."
        )

    # Prefix with a short UUID.
    prefix = str(uuid.uuid4())[:8]
    safe_name = f"{prefix}_{name}"

    if len(safe_name) > MAX_FILENAME_LENGTH:
        # Truncate the original part, keeping the extension.
        ext = Path(name).suffix
        stem_budget = MAX_FILENAME_LENGTH - len(prefix) - 1 - len(ext)
        if stem_budget < 1:
            raise InputValidationError(
                f"Filename is too long even after truncation (length={len(safe_name)})."
            )
        truncated_stem = Path(name).stem[:stem_budget]
        safe_name = f"{prefix}_{truncated_stem}{ext}"

    return safe_name


# ---------------------------------------------------------------------------
# Path traversal protection
# ---------------------------------------------------------------------------


def validate_file_path(path: Path, allowed_dir: Path) -> Path:
    """Validate that *path* is within *allowed_dir*.

    1. Resolve both paths to absolute form.
    2. Verify the resolved *path* starts with *allowed_dir*.
    3. If the path is a symlink, verify its target also resolves within
       *allowed_dir*.

    Raises:
        InputValidationError: On any validation failure.
    """
    try:
        resolved_dir = allowed_dir.resolve(strict=True)
    except OSError as exc:
        raise InputValidationError(
            f"Allowed directory does not exist or is inaccessible: {allowed_dir}"
        ) from exc

    try:
        resolved_path = path.resolve()
    except OSError as exc:
        raise InputValidationError(f"Cannot resolve path: {path}") from exc

    # Check containment.
    try:
        resolved_path.relative_to(resolved_dir)
    except ValueError as err:
        logger.warning(
            "path_traversal_blocked",
            path=str(path),
            resolved=str(resolved_path),
            allowed_dir=str(resolved_dir),
        )
        raise InputValidationError(
            f"Path '{path}' resolves to '{resolved_path}', which is outside "
            f"the allowed directory '{resolved_dir}'."
        ) from err

    # Symlink target check (if the file already exists).
    if path.is_symlink():
        link_target = path.resolve(strict=False)
        try:
            link_target.relative_to(resolved_dir)
        except ValueError as err:
            logger.warning(
                "symlink_traversal_blocked",
                path=str(path),
                target=str(link_target),
                allowed_dir=str(resolved_dir),
            )
            raise InputValidationError(
                f"Symlink '{path}' points to '{link_target}', which is outside "
                f"the allowed directory '{resolved_dir}'."
            ) from err

    logger.debug("path_validated", path=str(resolved_path))
    return resolved_path


# ---------------------------------------------------------------------------
# Magic byte verification
# ---------------------------------------------------------------------------


def verify_magic_bytes(content: bytes, extension: str) -> bool:
    """Check that the first bytes of *content* match the expected signature
    for *extension*.

    Returns ``True`` if the extension has no known magic bytes (i.e., text
    formats like ``.md``, ``.tex``, ``.txt``, ``.bib``) or if the signature
    matches.  Returns ``False`` only when a known signature is expected but
    does not match.
    """
    expected = MAGIC_BYTES.get(extension.lower())
    if expected is None:
        # No magic bytes to check (text-based formats).
        return True

    if len(content) < len(expected):
        return False

    return content[: len(expected)] == expected
