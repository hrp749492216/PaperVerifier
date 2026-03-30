"""Security layer for PaperVerifier.

Provides SSRF-safe URL validation, path traversal protection, file upload
validation with magic-byte verification, and subprocess sandboxing for
untrusted operations (e.g., ``git clone``).
"""

from __future__ import annotations

from paperverifier.security.input_validator import (
    ALLOWED_FILE_EXTENSIONS,
    BLOCKED_IP_RANGES,
    MAX_FILE_SIZE_BYTES,
    SAFE_URL_SCHEMES,
    InputValidationError,
    sanitize_filename,
    validate_file_path,
    validate_github_url,
    validate_uploaded_file,
    validate_url,
    verify_magic_bytes,
)
from paperverifier.security.sandbox import (
    SandboxError,
    SandboxTimeoutError,
    cleanup_temp_dir,
    clone_github_repo,
    run_sandboxed,
)

__all__ = [
    # Exceptions
    "InputValidationError",
    "SandboxError",
    "SandboxTimeoutError",
    # URL validation
    "validate_url",
    "validate_github_url",
    # File validation
    "validate_uploaded_file",
    "sanitize_filename",
    "validate_file_path",
    "verify_magic_bytes",
    # Sandbox
    "run_sandboxed",
    "clone_github_repo",
    "cleanup_temp_dir",
    # Constants
    "SAFE_URL_SCHEMES",
    "ALLOWED_FILE_EXTENSIONS",
    "MAX_FILE_SIZE_BYTES",
    "BLOCKED_IP_RANGES",
]
