import hashlib
import os
import random
import re
import smtplib
import sqlite3
import subprocess
import time
import uuid
from email.message import EmailMessage
from pathlib import Path

from flask import Flask, redirect, render_template, request, session, url_for

from deployment_manager import (
    execute_web_action,
    list_application_choices,
    list_cluster_choices,
)
from cluster_manager import cluster_exists


app = Flask(__name__)

secret = os.environ.get("FLASK_SECRET_KEY")

if not secret:
    secret = random.SystemRandom().randbytes(32).hex()

app.secret_key = secret


DATABASE_PATH = (
    Path(__file__).resolve().parent
    / "verified_emails.db"
)

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
            timeout=30,
        )
    except FileNotFoundError:
        return False, "Docker was not found. Start Docker Desktop and try again."
    except subprocess.TimeoutExpired:
        return False, "The image registry did not respond within 30 seconds."

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


def save_verified_email(email: str) -> None:
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO verified_emails (
                email
            )
            VALUES (?)
            """,
            (normalize_email(email),),
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


@app.route("/", methods=["GET", "POST"])
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

    session.clear()

    session["user"] = {
        "user_name": values["user_name"],
        "email": email,
    }

    if email_is_verified(email):
        session["verified"] = True
        return redirect(
            url_for("dashboard")
        )

    code = (
        f"{random.SystemRandom().randint(0, 999999):06d}"
    )

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
            save_verified_email(
                user["email"]
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
                    "This existing cluster is not "
                    "assigned to your account. Ask the "
                    "administrator to enable a "
                    "controlled legacy claim."
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
            "image",
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
            image = normalize_docker_image(
                values["image"]
            )

            if not image or not valid_docker_image_reference(image):
                errors["image"] = (
                    "Paste a Docker pull command "
                    "such as 'docker pull httpd:2.4' "
                    "or enter an image reference "
                    "such as 'httpd:2.4'."
                )

            else:
                available, availability_error = (
                    public_image_is_available(image)
                )

                if not available:
                    errors["image"] = availability_error

                data["image"] = image

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


@app.route(
    "/confirmation",
    methods=["GET", "POST"],
)
def confirmation():
    if (
        not require_verified()
        or not session.get("cluster")
    ):
        return redirect(
            url_for("dashboard")
        )

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
    cluster = session["cluster"]
    application = session.get(
        "application"
    )
    user = session["user"]

    result = execute_web_action(
        action,
        cluster,
        application,
    )

    if result.get("success"):
        if action == "create":
            save_owned_cluster(
                user["email"],
                cluster["cluster_name"],
            )

        elif (
            action == "deploy"
            and application
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

        elif (
            action == "delete"
            and application
        ):
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
        # Failed Pods must remain visible so the owner can inspect or
        # delete the Kubernetes resources from the web interface.
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

    if result.get("failed_application_saved"):
        delete_token = uuid.uuid4().hex
        session["failed_delete_token"] = delete_token
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
        url_for("identity")
    )


if __name__ == "__main__":
    initialize_database()

    app.run(
        debug=(
            os.environ.get("FLASK_DEBUG")
            == "1"
        )
    )