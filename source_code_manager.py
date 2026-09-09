import os
import re
import shutil
import stat
import subprocess
import tempfile
import uuid
import zipfile
from pathlib import Path


MAX_EXTRACTED_SIZE = 50 * 1024 * 1024
MAX_FILE_COUNT = 500

REGISTRY_SECRET_NAME = "private-registry-credentials"


class SourceCodeError(RuntimeError):
    pass


def run_command(
    command: list[str],
    input_text: str | None = None,
) -> str:
    try:
        result = subprocess.run(
            command,
            input=input_text,
            check=False,
            text=True,
            capture_output=True,
            timeout=600,
        )

    except FileNotFoundError as error:
        raise SourceCodeError(
            f"Required command was not found: {command[0]}"
        ) from error

    except subprocess.TimeoutExpired as error:
        raise SourceCodeError(
            f"The command took too long: {command[0]}"
        ) from error

    if result.returncode != 0:
        message = (
            result.stderr.strip()
            or result.stdout.strip()
            or f"Command failed: {command[0]}"
        )

        raise SourceCodeError(message)

    return result.stdout.strip()


def safe_extract_zip(
    zip_path: Path,
    destination: Path,
) -> None:
    try:
        archive = zipfile.ZipFile(zip_path)

    except zipfile.BadZipFile as error:
        raise SourceCodeError(
            "The uploaded file is not a valid ZIP file."
        ) from error

    with archive:
        files = archive.infolist()

        if len(files) > MAX_FILE_COUNT:
            raise SourceCodeError(
                "The ZIP contains too many files."
            )

        extracted_size = sum(
            item.file_size
            for item in files
        )

        if extracted_size > MAX_EXTRACTED_SIZE:
            raise SourceCodeError(
                "The extracted source code is larger than 50 MB."
            )

        destination = destination.resolve()

        for item in files:
            item_path = (
                destination / item.filename
            ).resolve()

            try:
                item_path.relative_to(destination)

            except ValueError as error:
                raise SourceCodeError(
                    "The ZIP contains an unsafe file path."
                ) from error

            file_mode = item.external_attr >> 16

            if stat.S_ISLNK(file_mode):
                raise SourceCodeError(
                    "Symbolic links are not allowed in the ZIP."
                )

        archive.extractall(destination)


def locate_application_directory(
    extracted_directory: Path,
) -> Path:
    matches = []

    for app_file in extracted_directory.rglob("app.py"):
        directory = app_file.parent

        if (directory / "requirements.txt").is_file():
            matches.append(directory)

    if not matches:
        raise SourceCodeError(
            "The ZIP must contain app.py and requirements.txt "
            "inside the same folder."
        )

    if len(matches) > 1:
        raise SourceCodeError(
            "The ZIP contains more than one application. "
            "Upload only one application."
        )

    return matches[0]


def create_dockerfile(
    application_directory: Path,
    application_port: int,
) -> Path:
    dockerfile_path = (
        application_directory
        / "Dockerfile.zero-touch"
    )

    dockerfile_content = f"""FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt \\
    && pip install --no-cache-dir gunicorn

COPY . .

EXPOSE {application_port}

CMD ["gunicorn", "--bind", "0.0.0.0:{application_port}", "app:app"]
"""

    dockerfile_path.write_text(
        dockerfile_content,
        encoding="utf-8",
    )

    return dockerfile_path


def registry_settings() -> dict:
    settings = {
        "server": os.environ.get(
            "REGISTRY_SERVER",
            "ghcr.io",
        ).strip(),
        "username": os.environ.get(
            "REGISTRY_USERNAME",
            "",
        ).strip(),
        "owner": os.environ.get(
            "REGISTRY_OWNER",
            "",
        ).strip().lower(),
        "email": os.environ.get(
            "REGISTRY_EMAIL",
            "",
        ).strip(),
        "token": os.environ.get(
            "REGISTRY_TOKEN",
            "",
        ).strip(),
    }

    missing = [
        name
        for name, value in settings.items()
        if not value
    ]

    if missing:
        raise SourceCodeError(
            "Private registry configuration is missing: "
            + ", ".join(missing)
        )

    return settings


def normalize_image_part(value: str) -> str:
    normalized = re.sub(
        r"[^a-z0-9._-]+",
        "-",
        value.strip().lower(),
    )

    normalized = normalized.strip(".-_")

    if not normalized:
        raise SourceCodeError(
            "The application name cannot be used as an image name."
        )

    return normalized


def build_and_push_source(
    zip_path: str,
    application_name: str,
    application_port: int,
) -> dict:
    source_zip = Path(zip_path)

    if not source_zip.is_file():
        raise SourceCodeError(
            "The uploaded source-code ZIP could not be found."
        )

    settings = registry_settings()

    temporary_directory = Path(
        tempfile.mkdtemp(
            prefix="zero-touch-build-"
        )
    )

    extracted_directory = (
        temporary_directory / "source"
    )

    extracted_directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    try:
        safe_extract_zip(
            source_zip,
            extracted_directory,
        )

        application_directory = (
            locate_application_directory(
                extracted_directory
            )
        )

        dockerfile = create_dockerfile(
            application_directory,
            application_port,
        )

        image_application_name = normalize_image_part(
            application_name
        )

        image_tag = uuid.uuid4().hex[:12]

        image_name = (
            f'{settings["server"]}/'
            f'{settings["owner"]}/'
            f"{image_application_name}:{image_tag}"
        )

        run_command(
            [
                "docker",
                "login",
                settings["server"],
                "--username",
                settings["username"],
                "--password-stdin",
            ],
            input_text=settings["token"],
        )

        run_command(
            [
                "docker",
                "build",
                "--file",
                str(dockerfile),
                "--tag",
                image_name,
                str(application_directory),
            ]
        )

        run_command(
            [
                "docker",
                "push",
                image_name,
            ]
        )

        return {
            "image": image_name,
            "registry_server": settings["server"],
            "registry_username": settings["username"],
            "registry_email": settings["email"],
            "registry_token": settings["token"],
            "registry_secret": REGISTRY_SECRET_NAME,
        }

    finally:
        shutil.rmtree(
            temporary_directory,
            ignore_errors=True,
        )


def create_registry_secret(
    cluster_name: str,
    namespace: str = "default",
) -> str:
    settings = registry_settings()

    secret_yaml = run_command(
        [
            "kubectl",
            "--context",
            cluster_name,
            "--namespace",
            namespace,
            "create",
            "secret",
            "docker-registry",
            REGISTRY_SECRET_NAME,
            f'--docker-server={settings["server"]}',
            f'--docker-username={settings["username"]}',
            f'--docker-password={settings["token"]}',
            f'--docker-email={settings["email"]}',
            "--dry-run=client",
            "--output=yaml",
        ]
    )

    run_command(
        [
            "kubectl",
            "--context",
            cluster_name,
            "--namespace",
            namespace,
            "apply",
            "--filename=-",
        ],
        input_text=secret_yaml,
    )

    return REGISTRY_SECRET_NAME