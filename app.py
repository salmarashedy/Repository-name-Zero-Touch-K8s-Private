import hashlib
import copy
import os
import random
import re
import smtplib
import sqlite3
import subprocess
import threading
import time
import uuid
from email.message import EmailMessage
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from deployment_manager import (
    delete_pod,
    execute_web_action,
    list_application_choices,
    list_cluster_choices,
    list_pod_choices,
)
from cluster_manager import cluster_exists, delete_cluster_profile
from application_gateway import create_application_token, proxy_application
from source_code_manager import save_source_upload


app = Flask(__name__)

secret = os.environ.get("FLASK_SECRET_KEY")

if not secret:
    secret = random.SystemRandom().randbytes(32).hex()

app.secret_key = secret
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_UPLOAD_BYTES", 50 * 1024 * 1024))
UPLOAD_DIRECTORY = Path(__file__).resolve().parent / "uploads"


DATABASE_PATH = (
    Path(__file__).resolve().parent
    / "verified_emails.db"
)

ADMIN_EMAIL = os.environ.get(
    "ADMIN_EMAIL",
    "k8sadmin@gmail.com",
).strip().lower()

VERIFICATION_EXPIRY_SECONDS = 120
MAX_VERIFICATION_ATTEMPTS = 5
MAX_CPU_MILLICORES = 1000
MAX_MEMORY_MIB = 1024
MAX_NODE_COUNT = 5

ACTIONS = {
    "create",
    "deploy",
    "update",
    "delete",
    "inspect",
}

OPERATION_JOBS = {}
OPERATION_JOBS_LOCK = threading.Lock()
OPERATION_EXECUTION_LOCK = threading.Lock()
OPERATION_JOB_TTL_SECONDS = 3600


def initialize_database() -> None:
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS verified_emails (
                email TEXT PRIMARY KEY,
                verified_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        existing_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(verified_emails)"
            ).fetchall()
        }

        if "user_name" not in existing_columns:
            connection.execute(
                "ALTER TABLE verified_emails ADD COLUMN user_name TEXT"
            )

        if "username_key" not in existing_columns:
            connection.execute(
                "ALTER TABLE verified_emails ADD COLUMN username_key TEXT"
            )

        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                unique_verified_username
            ON verified_emails (username_key)
            WHERE username_key IS NOT NULL
            """
        )

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_accounts (
                email TEXT PRIMARY KEY,
                password_hash TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS owned_clusters (
                email TEXT NOT NULL,
                cluster_name TEXT NOT NULL UNIQUE,
                PRIMARY KEY (email, cluster_name)
            )
            """
        )

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS owned_applications (
                email TEXT NOT NULL,
                cluster_name TEXT NOT NULL,
                namespace TEXT NOT NULL,
                app_name TEXT NOT NULL,
                PRIMARY KEY (
                    email,
                    cluster_name,
                    namespace,
                    app_name
                ),
                UNIQUE (
                    cluster_name,
                    namespace,
                    app_name
                )
            )
            """
        )


initialize_database()


def normalize_email(value: str) -> str:
    return value.strip().lower()


def normalize_username(value: str) -> str:
    return " ".join(value.strip().split()).casefold()


def valid_email(value: str) -> bool:
    return (
        re.fullmatch(
            r"[^@\s]+@[^@\s]+\.[^@\s]+",
            value,
        )
        is not None
    )


def normalize_kubernetes_name(value: str) -> str:
    value = re.sub(
        r"[^a-z0-9]+",
        "-",
        value.strip().lower(),
    ).strip("-")

    return value[:63].rstrip("-")


def valid_kubernetes_name(value: str) -> bool:
    return bool(value) and (
        re.fullmatch(
            r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?",
            value,
        )
        is not None
    )


def parse_integer(
    value: str,
    label: str,
    minimum: int,
    maximum: int,
) -> int:
    value = value.strip()

    if not re.fullmatch(r"\d+", value):
        raise ValueError(
            f"{label} must be a whole number."
        )

    number = int(value)

    if not minimum <= number <= maximum:
        raise ValueError(
            f"{label} must be between "
            f"{minimum} and {maximum}."
        )

    return number


def parse_cpu_millicores(value: str) -> int:
    value = value.strip()

    if not re.fullmatch(r"[1-9]\d*", value):
        raise ValueError(
            "CPU must be a positive whole number "
            "in millicores."
        )

    number = int(value)

    if number > MAX_CPU_MILLICORES:
        raise ValueError(
            "Each user can request a maximum of "
            "1000 millicores (1000m or 1 CPU core)."
        )

    return number


def parse_memory_mib(value: str) -> int:
    value = value.strip()

    if not re.fullmatch(r"[1-9]\d*", value):
        raise ValueError(
            "Memory must be a positive whole "
            "number in MiB."
        )

    number = int(value)

    if number > MAX_MEMORY_MIB:
        raise ValueError(
            "Each user can request a maximum of "
            "1024 MiB (1 GiB)."
        )

    return number


def normalize_docker_image(value: str) -> str:
    """Accept an image reference or Docker pull command."""

    value = value.strip()

    if value.lower().startswith("docker pull "):
        value = value[len("docker pull "):].strip()

    return value


def valid_docker_image_reference(value: str) -> bool:
    """Validate the common Docker/OCI image-reference syntax."""

    return (
        len(value) <= 255
        and re.fullmatch(
            r"(?:[a-zA-Z0-9.-]+(?::\d+)?/)?"
            r"(?:[a-z0-9]+(?:[._-][a-z0-9]+)*/)*"
            r"[a-z0-9]+(?:[._-][a-z0-9]+)*"
            r"(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?"
            r"(?:@sha256:[a-fA-F0-9]{64})?",
            value,
        )
        is not None
    )


def public_image_is_available(image: str) -> tuple[bool, str | None]:
    """Check that Docker can resolve a public image without pulling it."""

    try:
        result = subprocess.run(
            ["docker", "manifest", "inspect", image],
            check=False,
            text=True,
            capture_output=True,
            timeout=60,
        )
    except FileNotFoundError:
        return False, "Docker was not found. Start Docker Desktop and try again."
    except subprocess.TimeoutExpired:
        return False, "The image registry did not respond within 60 seconds."

    if result.returncode == 0:
        return True, None

    detail = (result.stderr or result.stdout).strip().lower()
    if "unauthorized" in detail or "denied" in detail:
        message = "The image is private or access to it was denied."
    elif "no such manifest" in detail or "manifest unknown" in detail:
        message = "The Docker image or tag does not exist in the public registry."
    else:
        message = "The public Docker image could not be verified. Check its name, tag, and your network connection."

    return False, message


def email_is_verified(email: str) -> bool:
    with sqlite3.connect(DATABASE_PATH) as connection:
        result = connection.execute(
            """
            SELECT 1
            FROM verified_emails
            WHERE email = ?
            """,
            (normalize_email(email),),
        ).fetchone()

    return result is not None


def get_identity_by_email(email: str):
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(
            """
            SELECT email, user_name, username_key
            FROM verified_emails
            WHERE email = ?
            """,
            (normalize_email(email),),
        ).fetchone()


def get_identity_by_username(user_name: str):
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(
            """
            SELECT email, user_name, username_key
            FROM verified_emails
            WHERE username_key = ?
            """,
            (normalize_username(user_name),),
        ).fetchone()


def save_verified_identity(email: str, user_name: str) -> None:
    normalized_email = normalize_email(email)
    clean_user_name = " ".join(user_name.strip().split())
    username_key = normalize_username(clean_user_name)

    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.execute(
            """
            INSERT INTO verified_emails (
                email,
                user_name,
                username_key
            )
            VALUES (?, ?, ?)
            ON CONFLICT(email) DO UPDATE SET
                user_name = excluded.user_name,
                username_key = excluded.username_key
            """,
            (
                normalized_email,
                clean_user_name,
                username_key,
            ),
        )


def get_admin_password_hash() -> str | None:
    with sqlite3.connect(DATABASE_PATH) as connection:
        row = connection.execute(
            """
            SELECT password_hash
            FROM admin_accounts
            WHERE email = ?
            """,
            (ADMIN_EMAIL,),
        ).fetchone()

    return row[0] if row else None


def admin_account_exists() -> bool:
    return get_admin_password_hash() is not None


def save_admin_password(password: str) -> None:
    password_hash = generate_password_hash(password)

    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.execute(
            """
            INSERT INTO admin_accounts (
                email,
                password_hash
            )
            VALUES (?, ?)
            ON CONFLICT(email) DO UPDATE SET
                password_hash = excluded.password_hash,
                updated_at = CURRENT_TIMESTAMP
            """,
            (ADMIN_EMAIL, password_hash),
        )


def admin_password_is_correct(password: str) -> bool:
    password_hash = get_admin_password_hash()

    return bool(
        password_hash
        and check_password_hash(
            password_hash,
            password,
        )
    )


def registered_users() -> list[dict]:
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT
                verified_emails.email,
                verified_emails.verified_at,
                COUNT(DISTINCT owned_clusters.cluster_name)
                    AS cluster_count,
                COUNT(DISTINCT
                    owned_applications.cluster_name || '|' ||
                    owned_applications.namespace || '|' ||
                    owned_applications.app_name
                ) AS application_count
            FROM verified_emails
            LEFT JOIN owned_clusters
                ON owned_clusters.email = verified_emails.email
            LEFT JOIN owned_applications
                ON owned_applications.email = verified_emails.email
            GROUP BY
                verified_emails.email,
                verified_emails.verified_at
            ORDER BY verified_emails.email
            """
        ).fetchall()

    return [dict(row) for row in rows]


def all_cluster_owners() -> dict[str, str]:
    with sqlite3.connect(DATABASE_PATH) as connection:
        rows = connection.execute(
            """
            SELECT cluster_name, email
            FROM owned_clusters
            """
        ).fetchall()

    return {
        cluster_name: email
        for cluster_name, email in rows
    }


def all_application_owners() -> dict[str, str]:
    with sqlite3.connect(DATABASE_PATH) as connection:
        rows = connection.execute(
            """
            SELECT
                cluster_name,
                namespace,
                app_name,
                email
            FROM owned_applications
            """
        ).fetchall()

    return {
        f"{cluster}|{namespace}|{application}": email
        for cluster, namespace, application, email in rows
    }


def remove_all_cluster_records(cluster_name: str) -> None:
    """Remove ownership records after an administrator deletes a cluster."""
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.execute(
            "DELETE FROM owned_applications WHERE cluster_name = ?",
            (cluster_name,),
        )
        connection.execute(
            "DELETE FROM owned_clusters WHERE cluster_name = ?",
            (cluster_name,),
        )


def delete_cluster_after_response(cluster_name: str) -> None:
    """Delete a cluster after the browser has received the dashboard response."""
    time.sleep(4)

    with OPERATION_EXECUTION_LOCK:
        try:
            delete_cluster_profile(cluster_name)
            remove_all_cluster_records(cluster_name)
            print(f"Administrator deleted cluster '{cluster_name}'.")
        except RuntimeError as error:
            print(
                f"Administrator cluster deletion failed for "
                f"'{cluster_name}': {error}"
            )


def owned_cluster_names(email: str) -> set[str]:
    with sqlite3.connect(DATABASE_PATH) as connection:
        rows = connection.execute(
            """
            SELECT cluster_name
            FROM owned_clusters
            WHERE email = ?
            """,
            (normalize_email(email),),
        ).fetchall()

    return {
        row[0]
        for row in rows
    }


def cluster_owner(
    cluster_name: str,
) -> str | None:
    with sqlite3.connect(DATABASE_PATH) as connection:
        row = connection.execute(
            """
            SELECT email
            FROM owned_clusters
            WHERE cluster_name = ?
            """,
            (cluster_name,),
        ).fetchone()

    return row[0] if row else None


def save_owned_cluster(
    email: str,
    cluster_name: str,
) -> None:
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO owned_clusters (
                email,
                cluster_name
            )
            VALUES (?, ?)
            """,
            (
                normalize_email(email),
                cluster_name,
            ),
        )


def owned_application_keys(
    email: str,
) -> set[str]:
    with sqlite3.connect(DATABASE_PATH) as connection:
        rows = connection.execute(
            """
            SELECT
                cluster_name,
                namespace,
                app_name
            FROM owned_applications
            WHERE email = ?
            """,
            (normalize_email(email),),
        ).fetchall()

    return {
        f"{cluster}|{namespace}|{application}"
        for cluster, namespace, application in rows
    }


def save_owned_application(
    email: str,
    application: dict,
    cluster: dict,
) -> None:
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO owned_applications (
                email,
                cluster_name,
                namespace,
                app_name
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                normalize_email(email),
                cluster["cluster_name"],
                application.get(
                    "namespace",
                    "default",
                ),
                application["app_name"],
            ),
        )


def remove_owned_application(
    email: str,
    application: dict,
    cluster: dict,
) -> None:
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.execute(
            """
            DELETE FROM owned_applications
            WHERE email = ?
              AND cluster_name = ?
              AND namespace = ?
              AND app_name = ?
            """,
            (
                normalize_email(email),
                cluster["cluster_name"],
                application.get(
                    "namespace",
                    "default",
                ),
                application["app_name"],
            ),
        )


def hash_code(code: str) -> str:
    return hashlib.sha256(
        code.encode()
    ).hexdigest()


def send_verification_email(
    email: str,
    name: str,
    code: str,
) -> None:
    sender = os.environ.get("SENDER_EMAIL")
    password = os.environ.get(
        "GMAIL_APP_PASSWORD"
    )

    if not sender or not password:
        raise RuntimeError(
            "Email settings are not configured."
        )

    message = EmailMessage()
    message["From"] = sender
    message["To"] = email
    message["Subject"] = (
        "Verify your email - Zero Touch Kubernetes"
    )

    message.set_content(
        f"Hello {name},\n\n"
        f"Your verification code is {code}.\n"
        "It expires in 2 minutes.\n"
    )

    with smtplib.SMTP_SSL(
        "smtp.gmail.com",
        465,
        timeout=30,
    ) as smtp:
        smtp.login(sender, password)
        smtp.send_message(message)


def prepare_user_friendly_result(
    result: dict,
    action: str,
    cluster: dict,
    user: dict,
) -> dict:
    """Hide infrastructure details while retaining them for administrators."""
    if result.get("success", False):
        return result

    technical_message = str(
        result.get("message", "Unknown operation error.")
    )
    cluster_name = cluster.get("cluster_name", "the requested cluster")

    app.logger.error(
        "Kubernetes operation failed | action=%s | cluster=%s | owner=%s | error=%s",
        action,
        cluster_name,
        user.get("email", "unknown"),
        technical_message,
    )

    result["technical_message"] = technical_message

    start_failure_markers = (
        "starting cluster",
        "starthost failed",
        "guest_provision",
        "docker container exited",
        "unable to inspect a not running container",
        "too many open files",
    )
    normalized_message = technical_message.lower()

    if any(marker in normalized_message for marker in start_failure_markers):
        result["error_title"] = "Cluster could not be started"
        result.pop("details", None)
        result["message"] = (
            "The cluster could not be started. Please contact the "
            "administrator for assistance."
        )

    return result


def send_operation_email(
    user: dict,
    action: str,
    cluster: dict,
    application: dict | None,
    result: dict,
) -> None:
    sender = os.environ.get("SENDER_EMAIL")
    password = os.environ.get(
        "GMAIL_APP_PASSWORD"
    )

    if not sender or not password:
        raise RuntimeError(
            "Operation email settings are "
            "not configured."
        )

    status = (
        "Successful"
        if result.get("success", False)
        else "Failed"
    )

    details = [
        f"Hello {user['user_name']},",
        "",
        f"Operation: {action.title()}",
        f"Status: {status}",
        f"Cluster: {cluster['cluster_name']}",
    ]

    if (
        application
        and application.get("app_name")
    ):
        details.append(
            f"Application: "
            f"{application['app_name']}"
        )

    if result.get("cluster_duration") is not None:
        details.append(
            "Cluster deployment time: "
            f"{result['cluster_duration']:.2f} "
            "seconds"
        )

    if (
        result.get("application_duration")
        is not None
    ):
        details.append(
            "Application deployment time: "
            f"{result['application_duration']:.2f} "
            "seconds"
        )

    if result.get("application_url"):
        details.extend(["", f"Open application: {result['application_url']}"])

    details.extend(
        [
            "",
            "Result:",
            result.get(
                "message",
                "No result message was provided.",
            ),
        ]
    )

    checks = result.get("checks", [])

    if checks:
        details.extend(
            [
                "",
                "Verification checks:",
            ]
        )

        details.extend(
            f"- {check}"
            for check in checks
        )

    details.extend(
        [
            "",
            "Zero-Touch Kubernetes "
            "Deployment System",
        ]
    )

    message = EmailMessage()
    message["From"] = sender
    message["To"] = user["email"]
    message["Subject"] = (
        "Zero-Touch Kubernetes - "
        f"{action.title()} - {status}"
    )

    message.set_content(
        "\n".join(details)
    )

    with smtplib.SMTP_SSL(
        "smtp.gmail.com",
        465,
        timeout=30,
    ) as smtp:
        smtp.login(sender, password)
        smtp.send_message(message)


def field_error(
    template: str,
    errors: dict,
    values: dict,
    **context,
):
    return render_template(
        template,
        errors=errors,
        values=values,
        **context,
    )


def create_verification_code() -> str:
    return (
        f"{random.SystemRandom().randint(0, 999999):06d}"
    )


def start_admin_verification(purpose: str) -> None:
    code = create_verification_code()

    send_verification_email(
        ADMIN_EMAIL,
        "Administrator",
        code,
    )

    session["admin_verification"] = {
        "code_hash": hash_code(code),
        "expires_at": (
            time.time()
            + VERIFICATION_EXPIRY_SECONDS
        ),
        "attempts": 0,
        "purpose": purpose,
    }


def require_admin() -> bool:
    return bool(
        session.get("role") == "admin"
        and session.get("admin_authenticated")
        and session.get("admin_email")
        == ADMIN_EMAIL
    )


def dashboard_endpoint() -> str:
    return (
        "admin_dashboard"
        if require_admin()
        else "dashboard"
    )


@app.route("/", methods=["GET", "POST"])
def role_select():
    if request.method == "POST":
        role = request.form.get("role", "")

        if role == "user":
            return redirect(url_for("identity"))

        if role == "admin":
            return redirect(url_for("admin_login"))

    return render_template("role_select.html")


@app.route("/user", methods=["GET", "POST"])
def identity():
    values = {
        "user_name": "",
        "email": "",
    }

    errors = {}

    if request.method == "GET":
        return field_error(
            "identity.html",
            errors,
            values,
        )

    values = {
        key: request.form.get(
            key,
            "",
        ).strip()
        for key in values
    }

    email = normalize_email(
        values["email"]
    )

    values["user_name"] = " ".join(
        values["user_name"].split()
    )

    if not values["user_name"]:
        errors["user_name"] = (
            "Name is required."
        )

    if not valid_email(email):
        errors["email"] = (
            "Enter a valid email address."
        )

    if errors:
        return field_error(
            "identity.html",
            errors,
            values,
        )

    identity_for_email = get_identity_by_email(email)
    identity_for_username = get_identity_by_username(
        values["user_name"]
    )

    email_has_another_username = (
        identity_for_email
        and identity_for_email["username_key"]
        and identity_for_email["username_key"]
        != normalize_username(values["user_name"])
    )

    username_taken = (
        identity_for_username
        and identity_for_username["email"] != email
    )

    if email_has_another_username:
        errors["email"] = (
            "This email is already registered with another username."
        )

    if username_taken:
        errors["user_name"] = (
            "This username is already taken. "
            "Please choose another one."
        )

    if errors:
        return field_error(
            "identity.html",
            errors,
            values,
        )

    session.clear()

    session["user"] = {
        "user_name": values["user_name"],
        "email": email,
    }
    session["role"] = "user"

    if identity_for_email:
        if not identity_for_email["username_key"]:
            try:
                save_verified_identity(
                    email,
                    values["user_name"],
                )
            except sqlite3.IntegrityError:
                errors["user_name"] = (
                    "This username is already taken. "
                    "Please choose another one."
                )

                return field_error(
                    "identity.html",
                    errors,
                    values,
                )

        session["verified"] = True
        return redirect(
            url_for("dashboard")
        )

    code = create_verification_code()

    try:
        send_verification_email(
            email,
            values["user_name"],
            code,
        )

    except (
        OSError,
        RuntimeError,
        smtplib.SMTPException,
    ) as error:
        errors["email"] = str(error)

        return field_error(
            "identity.html",
            errors,
            values,
        )

    session["verification"] = {
        "code_hash": hash_code(code),
        "expires_at": (
            time.time()
            + VERIFICATION_EXPIRY_SECONDS
        ),
        "attempts": 0,
    }

    return redirect(
        url_for("verify_email")
    )


@app.route(
    "/admin/login",
    methods=["GET", "POST"],
)
def admin_login():
    if require_admin():
        return redirect(url_for("admin_dashboard"))

    values = {"email": ADMIN_EMAIL}
    error = None

    if request.method == "POST":
        email = normalize_email(
            request.form.get("email", "")
        )
        password = request.form.get("password", "")
        values["email"] = email

        if email != ADMIN_EMAIL:
            error = "This email is not registered as the administrator."

        elif not admin_account_exists():
            session.clear()
            session["role"] = "admin"
            session["admin_email"] = ADMIN_EMAIL

            try:
                start_admin_verification("first_setup")
            except (
                OSError,
                RuntimeError,
                smtplib.SMTPException,
            ) as send_error:
                error = str(send_error)
            else:
                return redirect(
                    url_for("admin_verify")
                )

        elif not password:
            error = "Enter the administrator password."

        elif not admin_password_is_correct(password):
            error = "The administrator email or password is incorrect."

        else:
            session.clear()
            session["role"] = "admin"
            session["admin_email"] = ADMIN_EMAIL
            session["admin_authenticated"] = True
            session["user"] = {
                "user_name": "Administrator",
                "email": ADMIN_EMAIL,
            }

            return redirect(
                url_for("admin_dashboard")
            )

    return render_template(
        "admin_login.html",
        values=values,
        error=error,
        first_setup=not admin_account_exists(),
    )


@app.route(
    "/admin/forgot-password",
    methods=["GET", "POST"],
)
def admin_forgot_password():
    error = None
    values = {"email": ADMIN_EMAIL}

    if request.method == "POST":
        email = normalize_email(
            request.form.get("email", "")
        )
        values["email"] = email

        if email != ADMIN_EMAIL:
            error = "This email is not registered as the administrator."

        elif not admin_account_exists():
            error = "The administrator account has not been set up yet."

        else:
            session.clear()
            session["role"] = "admin"
            session["admin_email"] = ADMIN_EMAIL

            try:
                start_admin_verification("password_reset")
            except (
                OSError,
                RuntimeError,
                smtplib.SMTPException,
            ) as send_error:
                error = str(send_error)
            else:
                return redirect(
                    url_for("admin_verify")
                )

    return render_template(
        "admin_forgot_password.html",
        values=values,
        error=error,
    )


@app.route(
    "/admin/verify",
    methods=["GET", "POST"],
)
def admin_verify():
    verification = session.get(
        "admin_verification"
    )

    if (
        not verification
        or session.get("admin_email")
        != ADMIN_EMAIL
    ):
        return redirect(url_for("admin_login"))

    error = None

    if request.method == "POST":
        submitted_code = request.form.get(
            "verification_code",
            "",
        ).strip()

        if time.time() > verification["expires_at"]:
            session.pop("admin_verification", None)
            error = (
                "The code expired after 2 minutes. "
                "Return and request a new code."
            )

        elif hash_code(submitted_code) == verification["code_hash"]:
            purpose = verification["purpose"]
            session.pop("admin_verification", None)
            session["admin_password_setup_authorized"] = True
            session["admin_password_setup_purpose"] = purpose

            return redirect(
                url_for("admin_set_password")
            )

        else:
            verification["attempts"] += 1

            if verification["attempts"] >= MAX_VERIFICATION_ATTEMPTS:
                session.pop("admin_verification", None)
                error = (
                    "Five incorrect attempts were made. "
                    "Request a new code."
                )
            else:
                session["admin_verification"] = verification
                attempts_left = (
                    MAX_VERIFICATION_ATTEMPTS
                    - verification["attempts"]
                )
                error = (
                    "Incorrect code. "
                    f"{attempts_left} attempts remaining."
                )

    return render_template(
        "admin_verify.html",
        email=ADMIN_EMAIL,
        error=error,
    )


@app.route(
    "/admin/set-password",
    methods=["GET", "POST"],
)
def admin_set_password():
    if not session.get("admin_password_setup_authorized"):
        return redirect(url_for("admin_login"))

    error = None
    purpose = session.get(
        "admin_password_setup_purpose",
        "first_setup",
    )

    if request.method == "POST":
        password = request.form.get("password", "")
        confirmation = request.form.get(
            "confirm_password",
            "",
        )

        if len(password) < 8:
            error = "The password must contain at least 8 characters."
        elif password != confirmation:
            error = "The two passwords do not match."
        else:
            save_admin_password(password)

            session.pop(
                "admin_password_setup_authorized",
                None,
            )
            session.pop(
                "admin_password_setup_purpose",
                None,
            )
            session["role"] = "admin"
            session["admin_email"] = ADMIN_EMAIL
            session["admin_authenticated"] = True
            session["user"] = {
                "user_name": "Administrator",
                "email": ADMIN_EMAIL,
            }

            return redirect(
                url_for("admin_dashboard")
            )

    return render_template(
        "admin_set_password.html",
        error=error,
        purpose=purpose,
    )


@app.route(
    "/verify",
    methods=["GET", "POST"],
)
def verify_email():
    verification = session.get(
        "verification"
    )

    user = session.get("user")

    if not verification or not user:
        return redirect(
            url_for("identity")
        )

    error = None

    if request.method == "POST":
        if (
            time.time()
            > verification["expires_at"]
        ):
            session.pop(
                "verification",
                None,
            )

            error = (
                "The code expired after 2 minutes. "
                "Return and request a new code."
            )

        elif (
            hash_code(
                request.form.get(
                    "verification_code",
                    "",
                ).strip()
            )
            == verification["code_hash"]
        ):
            try:
                save_verified_identity(
                    user["email"],
                    user["user_name"],
                )
            except sqlite3.IntegrityError:
                session.pop(
                    "verification",
                    None,
                )

                return render_template(
                    "verify.html",
                    error=(
                        "This username is already taken. "
                        "Please choose another one."
                    ),
                )

            session.pop(
                "verification",
                None,
            )

            session["verified"] = True

            return redirect(
                url_for("dashboard")
            )

        else:
            verification["attempts"] += 1

            if (
                verification["attempts"]
                >= MAX_VERIFICATION_ATTEMPTS
            ):
                session.pop(
                    "verification",
                    None,
                )

                error = (
                    "Five incorrect attempts were "
                    "made. Request a new code."
                )

            else:
                session["verification"] = (
                    verification
                )

                attempts_left = (
                    MAX_VERIFICATION_ATTEMPTS
                    - verification["attempts"]
                )

                error = (
                    "Incorrect code. "
                    f"{attempts_left} attempts "
                    "remaining."
                )

    return render_template(
        "verify_email.html",
        email=user["email"],
        error=error,
    )


def require_verified():
    return (
        session.get("verified")
        and session.get("user")
        and session.get("role") == "user"
    )


@app.route(
    "/admin/dashboard",
    methods=["GET", "POST"],
)
def admin_dashboard():
    if not require_admin():
        return redirect(url_for("admin_login"))

    discovery_error = None
    notice = session.pop("admin_notice", None)
    error = session.pop("admin_error", None)

    if not session.get("admin_action_token"):
        session["admin_action_token"] = uuid.uuid4().hex

    admin_action_token = session["admin_action_token"]

    try:
        clusters = list_cluster_choices()
        applications = list_application_choices()
        pods = list_pod_choices()
    except RuntimeError as discovery_exception:
        clusters = []
        applications = []
        pods = []
        discovery_error = str(discovery_exception)

    cluster_owners = all_cluster_owners()
    application_owners = all_application_owners()

    clusters = [
        {
            **cluster,
            "owner": cluster_owners.get(
                cluster["name"],
                "Unassigned",
            ),
        }
        for cluster in clusters
    ]

    applications = [
        {
            **application,
            "owner": application_owners.get(
                application["key"],
                "Unassigned",
            ),
        }
        for application in applications
    ]

    if request.method == "POST":
        if request.form.get("token", "") != admin_action_token:
            session["admin_error"] = "The request is invalid. Please try again."
            return redirect(url_for("admin_dashboard"))

        admin_action = request.form.get(
            "admin_action",
            "inspect_application",
        )

        if admin_action == "delete_cluster":
            selected_cluster = request.form.get("cluster_name", "")
            cluster_match = next(
                (cluster for cluster in clusters if cluster["name"] == selected_cluster),
                None,
            )

            if cluster_match is None:
                session["admin_error"] = "The selected cluster was not found."
            else:
                worker = threading.Thread(
                    target=delete_cluster_after_response,
                    args=(selected_cluster,),
                    daemon=True,
                )
                worker.start()
                session["admin_notice"] = (
                    f"Cluster '{selected_cluster}' is being deleted. "
                    "Wait a few seconds, then refresh the dashboard."
                )

            return redirect(url_for("admin_dashboard"))

        if admin_action == "delete_pod":
            selected_pod = request.form.get("pod_key", "")
            pod_match = next(
                (pod for pod in pods if pod["key"] == selected_pod),
                None,
            )

            if pod_match is None:
                session["admin_error"] = "The selected Pod was not found."
            else:
                try:
                    delete_pod(
                        pod_match["cluster_name"],
                        pod_match["namespace"],
                        pod_match["pod_name"],
                    )
                    session["admin_notice"] = (
                        f"Pod '{pod_match['pod_name']}' was deleted. "
                        "Kubernetes may create a replacement if it belongs to a Deployment."
                    )
                except RuntimeError as delete_error:
                    session["admin_error"] = str(delete_error)

            return redirect(url_for("admin_dashboard"))

        selected = request.form.get("application_key", "")

        match = next(
            (
                application
                for application in applications
                if application["key"] == selected
            ),
            None,
        )

        if match is None:
            error = "Choose an application to inspect."
        else:
            session["action"] = "inspect"
            session["cluster"] = {
                "cluster_name": match["cluster_name"],
                "node_count": match["node_count"],
                "original_name": match["cluster_name"],
            }
            session["application"] = {
                key: match[key]
                for key in (
                    "app_name",
                    "namespace",
                    "image",
                    "replicas",
                    "port",
                    "cpu_request",
                    "memory_request",
                )
            }

            return redirect(url_for("confirmation"))

    return render_template(
        "admin_dashboard.html",
        admin_email=ADMIN_EMAIL,
        users=registered_users(),
        clusters=clusters,
        applications=applications,
        pods=pods,
        error=error,
        notice=notice,
        discovery_error=discovery_error,
        admin_action_token=admin_action_token,
    )


@app.route(
    "/dashboard",
    methods=["GET", "POST"],
)
def dashboard():
    if not require_verified():
        return redirect(
            url_for("identity")
        )

    if request.method == "POST":
        action = request.form.get(
            "action",
            "",
        )

        if action in ACTIONS:
            session["action"] = action

            session.pop("cluster", None)
            session.pop("application", None)

            if action == "create":
                return redirect(
                    url_for("cluster_step")
                )

            if action == "deploy":
                return redirect(
                    url_for("cluster_select")
                )

            return redirect(
                url_for("application_select")
            )

    return render_template(
        "dashboard.html",
        user=session["user"],
    )


@app.route(
    "/cluster",
    methods=["GET", "POST"],
)
def cluster_step():
    if (
        not require_verified()
        or session.get("action") != "create"
    ):
        return redirect(
            url_for("dashboard")
        )

    action = session["action"]

    values = session.get(
        "cluster",
        {
            "cluster_name": "",
            "node_count": "",
        },
    )

    errors = {}

    if request.method == "POST":
        raw_name = request.form.get(
            "cluster_name",
            "",
        )

        name = normalize_kubernetes_name(
            raw_name
        )

        values = {
            "cluster_name": raw_name,
            "normalized_name": name,
            "node_count": request.form.get(
                "node_count",
                "",
            ),
        }

        if not valid_kubernetes_name(name):
            errors["cluster_name"] = (
                "Enter a name containing "
                "letters or numbers."
            )

        try:
            nodes = parse_integer(
                values["node_count"],
                "Node count",
                1,
                MAX_NODE_COUNT,
            )

        except ValueError as error:
            errors["node_count"] = str(error)

        if not errors:
            owner = cluster_owner(name)

            if (
                owner
                and owner
                != session["user"]["email"]
            ):
                errors["cluster_name"] = (
                    "This cluster name is unavailable."
                )

            elif (
                cluster_exists(name)
                and owner is None
                and os.environ.get(
                    "ALLOW_LEGACY_RESOURCE_CLAIM"
                )
                != "1"
            ):
                errors["cluster_name"] = (
                    "A cluster with this name already exists. "
                    "Please choose another cluster name."
                )

        if not errors:
            session["cluster"] = {
                "cluster_name": name,
                "node_count": nodes,
                "original_name": (
                    raw_name.strip()
                ),
            }

            return redirect(
                url_for("confirmation")
            )

    return field_error(
        "cluster_form.html",
        errors,
        values,
        action=action,
    )


@app.route(
    "/select-cluster",
    methods=["GET", "POST"],
)
def cluster_select():
    if (
        not require_verified()
        or session.get("action") != "deploy"
    ):
        return redirect(
            url_for("dashboard")
        )

    try:
        clusters = list_cluster_choices(
            owned_cluster_names(
                session["user"]["email"]
            )
        )

    except RuntimeError as error:
        clusters = []
        discovery_error = str(error)

    else:
        discovery_error = None

    error = None

    if request.method == "POST":
        selected = request.form.get(
            "cluster_name",
            "",
        )

        match = next(
            (
                item
                for item in clusters
                if item["name"] == selected
            ),
            None,
        )

        if match is None:
            error = (
                "Choose an existing cluster."
            )

        elif match["node_count"] < 1:
            error = (
                "The cluster node count could not "
                "be determined. Start or resize it "
                "from Create cluster first."
            )

        else:
            session["cluster"] = {
                "cluster_name": match["name"],
                "node_count": match["node_count"],
                "original_name": match["name"],
            }

            return redirect(
                url_for("application_step")
            )

    return render_template(
        "cluster_select.html",
        clusters=clusters,
        error=error,
        discovery_error=discovery_error,
    )


@app.route(
    "/select-application",
    methods=["GET", "POST"],
)
def application_select():
    if (
        not require_verified()
        or session.get("action")
        not in {
            "update",
            "delete",
            "inspect",
        }
    ):
        return redirect(
            url_for("dashboard")
        )

    try:
        email = session["user"]["email"]

        applications = list_application_choices(
            allowed_cluster_names=(
                owned_cluster_names(email)
            ),
            allowed_application_keys=(
                owned_application_keys(email)
            ),
        )

    except RuntimeError as error:
        applications = []
        discovery_error = str(error)

    else:
        discovery_error = None

    error = None

    if request.method == "POST":
        selected = request.form.get(
            "application_key",
            "",
        )

        match = next(
            (
                item
                for item in applications
                if item["key"] == selected
            ),
            None,
        )

        if match is None:
            error = (
                "Choose an existing application."
            )

        else:
            session["cluster"] = {
                "cluster_name": (
                    match["cluster_name"]
                ),
                "node_count": (
                    match["node_count"]
                ),
                "original_name": (
                    match["cluster_name"]
                ),
            }

            session["application"] = {
                key: match[key]
                for key in (
                    "app_name",
                    "namespace",
                    "image",
                    "replicas",
                    "port",
                    "cpu_request",
                    "memory_request",
                )
            }

            if session["action"] == "update":
                return redirect(
                    url_for("application_step")
                )

            return redirect(
                url_for("confirmation")
            )

    return render_template(
        "application_select.html",
        applications=applications,
        error=error,
        discovery_error=discovery_error,
        action=session["action"],
    )


@app.route(
    "/application",
    methods=["GET", "POST"],
)
def application_step():
    if not require_verified():
        return redirect(
            url_for("identity")
        )

    action = session["action"]

    if not session.get("cluster"):
        endpoint = (
            "cluster_select"
            if action == "deploy"
            else "application_select"
        )

        return redirect(
            url_for(endpoint)
        )

    values = (
        session.get("application", {})
        if action == "update"
        else {}
    )

    errors = {}

    if request.method == "POST":
        keys = [
            "app_name",
            "replicas",
            "port",
            "cpu_request",
            "memory_request",
        ]

        values = {
            key: request.form.get(
                key,
                "",
            ).strip()
            for key in keys
        }

        if action == "update":
            values["app_name"] = (
                session["application"]["app_name"]
            )

        app_name = normalize_kubernetes_name(
            values["app_name"]
        )

        if not valid_kubernetes_name(app_name):
            errors["app_name"] = (
                "Enter an application name "
                "containing letters or numbers."
            )

        data = {
            "app_name": app_name,
            "original_name": values["app_name"],
        }

        if action == "update":
            data["namespace"] = (
                session["application"].get(
                    "namespace",
                    "default",
                )
            )

        if action in {"deploy", "update"}:
            source_archive = request.files.get("source_archive")
            if source_archive and source_archive.filename:
                try:
                    staged_path = save_source_upload(source_archive, UPLOAD_DIRECTORY)
                    data["source_archive"] = str(staged_path)
                except (OSError, ValueError) as error:
                    errors["source_archive"] = str(error)
            elif action == "deploy":
                errors["source_archive"] = "Upload your Docker-ready application as a ZIP file."
            else:
                data["image"] = session["application"].get("image", "")

            integer_fields = [
                (
                    "replicas",
                    "Replicas",
                    1,
                    100,
                ),
                (
                    "port",
                    "Port",
                    1,
                    65535,
                ),
            ]

            for (
                key,
                label,
                minimum,
                maximum,
            ) in integer_fields:
                try:
                    data[key] = parse_integer(
                        values[key],
                        label,
                        minimum,
                        maximum,
                    )

                except ValueError as error:
                    errors[key] = str(error)

            resource_fields = [
                (
                    "cpu_request",
                    parse_cpu_millicores,
                ),
                (
                    "memory_request",
                    parse_memory_mib,
                ),
            ]

            for key, parser in resource_fields:
                try:
                    data[key] = parser(
                        values[key]
                    )

                except ValueError as error:
                    errors[key] = str(error)

        if not errors:
            session["application"] = data

            return redirect(
                url_for("confirmation")
            )

    return field_error(
        "application_form.html",
        errors,
        values,
        action=action,
        max_cpu=MAX_CPU_MILLICORES,
        max_memory=MAX_MEMORY_MIB,
    )


@app.route("/apps/<token>/", defaults={"subpath": ""}, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
@app.route("/apps/<token>/<path:subpath>", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
def application_gateway(token: str, subpath: str):
    return proxy_application(app.secret_key, token, subpath)


def clean_expired_operation_jobs() -> None:
    cutoff = time.time() - OPERATION_JOB_TTL_SECONDS

    with OPERATION_JOBS_LOCK:
        expired_ids = [
            job_id
            for job_id, job in OPERATION_JOBS.items()
            if job["created_at"] < cutoff
        ]

        for job_id in expired_ids:
            OPERATION_JOBS.pop(job_id, None)


def operation_job_for_current_user(job_id: str):
    user = session.get("user")

    if not user:
        return None

    with OPERATION_JOBS_LOCK:
        job = OPERATION_JOBS.get(job_id)

        if not job or job["owner_email"] != user.get("email"):
            return None

        return job


def finish_operation_job(
    job_id: str,
    action: str,
    cluster: dict,
    application: dict | None,
    user: dict,
) -> None:
    # Give the progress page time to reach the browser before Minikube
    # changes Docker's virtual networking.
    time.sleep(1.5)

    with OPERATION_EXECUTION_LOCK:
        with OPERATION_JOBS_LOCK:
            job = OPERATION_JOBS.get(job_id)

            if not job:
                return

            job["status"] = "running"

        try:
            result = execute_web_action(
                action,
                cluster,
                application,
            )

            if result.get("success") and action in {"deploy", "update"} and application:
                gateway_token = create_application_token(
                    app.secret_key,
                    cluster["cluster_name"],
                    application.get("namespace", "default"),
                    f"{application['app_name']}-service",
                    application["port"],
                )
                base_url = os.environ.get(
                 "PUBLIC_BASE_URL","http://127.0.0.1:5055",).rstrip("/")

                result["application_url"] = ( f"{base_url}/apps/{gateway_token}/"
                )

            result = prepare_user_friendly_result(
                result,
                action,
                cluster,
                user,
            )

            if result.get("success"):
                if action == "create":
                    save_owned_cluster(
                        user["email"],
                        cluster["cluster_name"],
                    )

                elif action == "deploy" and application:
                    save_owned_cluster(
                        user["email"],
                        cluster["cluster_name"],
                    )
                    save_owned_application(
                        user["email"],
                        application,
                        cluster,
                    )

                elif action == "delete" and application:
                    remove_owned_application(
                        user["email"],
                        application,
                        cluster,
                    )

            elif (
                action == "deploy"
                and application
                and result.get("resources_applied")
            ):
                save_owned_cluster(
                    user["email"],
                    cluster["cluster_name"],
                )
                save_owned_application(
                    user["email"],
                    application,
                    cluster,
                )
                result["failed_application_saved"] = True

            try:
                send_operation_email(
                    user,
                    action,
                    cluster,
                    application,
                    result,
                )
                result["notification_sent"] = True
                result["notification_message"] = (
                    "An operation notification was sent to "
                    f"{user['email']}."
                )

            except (
                OSError,
                RuntimeError,
                smtplib.SMTPException,
            ) as email_error:
                result["notification_sent"] = False
                result["notification_message"] = (
                    "The operation finished, but its email "
                    "notification could not be sent: "
                    f"{email_error}"
                )

        except Exception as error:
            app.logger.exception(
                "Unexpected Kubernetes operation failure | action=%s | cluster=%s | owner=%s",
                action,
                cluster.get("cluster_name", "unknown"),
                user.get("email", "unknown"),
            )
            result = {
                "success": False,
                "error_title": "Operation could not be completed",
                "message": (
                    "The operation could not be completed because of an "
                    "unexpected system problem. Please try again later or "
                    "contact the administrator."
                ),
                "technical_message": str(error),
            }

        with OPERATION_JOBS_LOCK:
            job = OPERATION_JOBS.get(job_id)

            if job:
                job["status"] = "completed"
                job["result"] = result
                job["completed_at"] = time.time()


@app.route(
    "/confirmation",
    methods=["GET", "POST"],
)
def confirmation():
    if (
        not (
            require_verified()
            or require_admin()
        )
        or not session.get("cluster")
    ):
        return redirect(
            url_for(dashboard_endpoint())
        )

    if require_admin() and session.get("action") != "inspect":
        return redirect(url_for("admin_dashboard"))

    if (
        session["action"] != "create"
        and not session.get("application")
    ):
        return redirect(
            url_for("application_step")
        )

    if request.method == "GET":
        token = uuid.uuid4().hex

        session["confirmation_token"] = token

        return render_template(
            "confirmation.html",
            action=session["action"],
            cluster=session["cluster"],
            app_data=session.get("application"),
            token=token,
        )

    token = request.form.get("token")

    if (
        not token
        or token
        != session.pop(
            "confirmation_token",
            None,
        )
    ):
        return render_template(
            "result.html",
            result={
                "success": False,
                "message": (
                    "This confirmation was already "
                    "used or is invalid."
                ),
            },
        )

    action = session["action"]
    cluster = copy.deepcopy(session["cluster"])
    application = copy.deepcopy(session.get("application"))
    user = copy.deepcopy(session["user"])
    job_id = uuid.uuid4().hex

    clean_expired_operation_jobs()

    with OPERATION_JOBS_LOCK:
        OPERATION_JOBS[job_id] = {
            "status": "pending",
            "created_at": time.time(),
            "owner_email": user["email"],
            "action": action,
            "cluster": cluster,
            "application": application,
            "user": user,
            "result": None,
            "started": False,
        }

    return redirect(
        url_for(
            "operation_progress",
            job_id=job_id,
        )
    )


@app.get("/operation/<job_id>")
def operation_progress(job_id):
    job = operation_job_for_current_user(job_id)

    if not job:
        return redirect(url_for(dashboard_endpoint()))

    return render_template(
        "operation_progress.html",
        job_id=job_id,
        action=job["action"],
        cluster=job["cluster"],
    )


@app.post("/operation/<job_id>/start")
def start_operation_job(job_id):
    job = operation_job_for_current_user(job_id)

    if not job:
        return jsonify({"error": "Operation not found."}), 404

    with OPERATION_JOBS_LOCK:
        job = OPERATION_JOBS.get(job_id)

        if not job:
            return jsonify({"error": "Operation not found."}), 404

        if job["started"]:
            return jsonify({"status": job["status"]})

        job["started"] = True
        job["status"] = "queued"
        action = job["action"]
        cluster = copy.deepcopy(job["cluster"])
        application = copy.deepcopy(job["application"])
        user = copy.deepcopy(job["user"])

    worker = threading.Thread(
        target=finish_operation_job,
        args=(
            job_id,
            action,
            cluster,
            application,
            user,
        ),
        daemon=True,
    )
    worker.start()

    return jsonify({"status": "queued"}), 202


@app.get("/operation/<job_id>/status")
def operation_status(job_id):
    job = operation_job_for_current_user(job_id)

    if not job:
        return jsonify({"error": "Operation not found."}), 404

    response = {"status": job["status"]}

    if job["status"] == "completed":
        response["result_url"] = url_for(
            "operation_result",
            job_id=job_id,
        )

    return jsonify(response)


@app.get("/operation/<job_id>/result")
def operation_result(job_id):
    job = operation_job_for_current_user(job_id)

    if not job:
        return redirect(url_for(dashboard_endpoint()))

    if job["status"] != "completed":
        return redirect(
            url_for(
                "operation_progress",
                job_id=job_id,
            )
        )

    result = copy.deepcopy(job["result"])

    if result.get("failed_application_saved"):
        delete_token = uuid.uuid4().hex
        session["failed_delete_token"] = delete_token
        session["cluster"] = copy.deepcopy(job["cluster"])
        session["application"] = copy.deepcopy(job["application"])
        result["failed_delete_token"] = delete_token

    return render_template(
        "result.html",
        result=result,
    )


@app.post("/delete-failed-application")
def delete_failed_application():
    if not require_verified():
        return redirect(url_for("identity"))

    token = request.form.get("token", "")
    expected = session.pop("failed_delete_token", None)
    application = session.get("application")
    cluster = session.get("cluster")

    if not token or token != expected or not application or not cluster:
        return render_template(
            "result.html",
            result={
                "success": False,
                "message": "The delete request is invalid or has already been used.",
            },
        )

    session["action"] = "delete"
    return redirect(url_for("confirmation"))


@app.route("/logout")
def logout():
    session.clear()

    return redirect(
        url_for("role_select")
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5055)