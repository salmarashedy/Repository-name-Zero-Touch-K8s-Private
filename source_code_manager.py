"""Secure source archive validation and private container image publishing."""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import tempfile
import uuid
import zipfile
from pathlib import Path, PurePosixPath


MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", 50 * 1024 * 1024))
MAX_EXTRACTED_BYTES = int(os.environ.get("MAX_EXTRACTED_BYTES", 250 * 1024 * 1024))
MAX_ARCHIVE_FILES = int(os.environ.get("MAX_ARCHIVE_FILES", 5000))


def save_source_upload(upload, upload_directory: Path) -> Path:
    """Save a Flask upload under an unpredictable server-side name."""
    upload_directory.mkdir(parents=True, exist_ok=True)
    destination = upload_directory / f"{uuid.uuid4().hex}.zip"
    upload.save(destination)
    if destination.stat().st_size > MAX_UPLOAD_BYTES:
        destination.unlink(missing_ok=True)
        raise ValueError(f"The ZIP exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MiB limit.")
    validate_source_archive(destination)
    return destination


def validate_source_archive(archive_path: Path) -> None:
    if not zipfile.is_zipfile(archive_path):
        raise ValueError("Upload a valid ZIP archive.")
    total_size = 0
    with zipfile.ZipFile(archive_path) as archive:
        members = archive.infolist()
        if not members or len(members) > MAX_ARCHIVE_FILES:
            raise ValueError("The ZIP is empty or contains too many files.")
        for member in members:
            path = PurePosixPath(member.filename.replace("\\", "/"))
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("The ZIP contains an unsafe extraction path.")
            mode = member.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise ValueError("Symbolic links are not allowed in source ZIP files.")
            total_size += member.file_size
            if total_size > MAX_EXTRACTED_BYTES:
                raise ValueError("The extracted project would exceed the safety limit.")


def _extract_project(archive_path: Path, target: Path) -> Path:
    validate_source_archive(archive_path)
    with zipfile.ZipFile(archive_path) as archive:
        archive.extractall(target)
    entries = [item for item in target.iterdir() if item.name not in {"__MACOSX"}]
    root = entries[0] if len(entries) == 1 and entries[0].is_dir() else target
    if not (root / "Dockerfile").is_file():
        raise ValueError("Dockerfile must exist at the root of the uploaded project.")
    return root


def _run(command: list[str], *, input_text: str | None = None, timeout: int = 900) -> str:
    try:
        result = subprocess.run(
            command, input=input_text, text=True, capture_output=True,
            timeout=timeout, check=False,
        )
    except FileNotFoundError as error:
        raise RuntimeError(f"Required command '{command[0]}' was not found.") from error
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"Command timed out: {' '.join(command[:2])}") from error
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(detail[-4000:] or f"Command failed: {' '.join(command)}")
    return result.stdout.strip()


def build_and_push_private_image(archive_path: str, app_name: str) -> dict:
    """Build the uploaded project and push it to the configured private registry."""
    registry = os.environ.get("REGISTRY_SERVER", "").strip().rstrip("/")
    username = os.environ.get("REGISTRY_USERNAME", "").strip()
    password = os.environ.get("REGISTRY_PASSWORD", "")
    repository = os.environ.get("REGISTRY_REPOSITORY", "zero-touch").strip("/")
    if not registry or not username or not password:
        raise RuntimeError(
            "Private registry settings are incomplete. Configure REGISTRY_SERVER, "
            "REGISTRY_USERNAME, and REGISTRY_PASSWORD."
        )
    safe_app = re.sub(r"[^a-z0-9._-]+", "-", app_name.lower()).strip("-.")
    tag = uuid.uuid4().hex[:12]
    image = f"{registry}/{repository}/{safe_app}:{tag}"
    with tempfile.TemporaryDirectory(prefix="zero-touch-build-") as temporary:
        project_root = _extract_project(Path(archive_path), Path(temporary))
        _run(["docker", "login", registry, "--username", username, "--password-stdin"], input_text=password, timeout=120)
        _run(["docker", "build", "--pull", "--tag", image, str(project_root)])
        _run(["docker", "push", image])
    return {"image": image, "registry_server": registry, "registry_username": username}


def remove_staged_upload(path: str | None) -> None:
    if path:
        candidate = Path(path)
        if candidate.name.endswith(".zip") and candidate.parent.name == "uploads":
            candidate.unlink(missing_ok=True)
