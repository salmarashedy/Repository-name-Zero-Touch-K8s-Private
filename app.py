import hashlib
import os
import random
import re
import smtplib
import sqlite3
import time
from email.message import EmailMessage
from pathlib import Path

from flask import (
    Flask,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from deployment_manager import deploy_application_from_web


app = Flask(__name__)

app.secret_key = os.environ.get(
    "FLASK_SECRET_KEY",
    "zero-touch-local-development-key",
)

DATABASE_PATH = (
    Path(__file__).resolve().parent
    / "verified_emails.db"
)

VERIFICATION_EXPIRY_SECONDS = 600
MAX_VERIFICATION_ATTEMPTS = 5


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

        connection.commit()


def normalize_email(email: str) -> str:
    return email.strip().lower()


def valid_kubernetes_name(value: str) -> bool:
    pattern = r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"

    return (
        len(value) <= 63
        and re.fullmatch(pattern, value) is not None
    )


def valid_email(value: str) -> bool:
    pattern = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"

    return re.fullmatch(pattern, value) is not None


def email_is_verified(email: str) -> bool:
    with sqlite3.connect(DATABASE_PATH) as connection:
        result = connection.execute(
            """
            SELECT email
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
            INSERT OR IGNORE INTO verified_emails (email)
            VALUES (?)
            """,
            (normalize_email(email),),
        )

        connection.commit()


def hash_verification_code(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def create_verification_code() -> str:
    return f"{random.SystemRandom().randint(0, 999999):06d}"


def get_gmail_settings() -> tuple[str, str]:
    sender_email = os.environ.get("SENDER_EMAIL")
    app_password = os.environ.get("GMAIL_APP_PASSWORD")

    if not sender_email:
        raise RuntimeError(
            "SENDER_EMAIL is not set in PowerShell."
        )

    if not app_password:
        raise RuntimeError(
            "GMAIL_APP_PASSWORD is not set in PowerShell."
        )

    return sender_email, app_password


def send_email(message: EmailMessage) -> None:
    sender_email, app_password = get_gmail_settings()

    message["From"] = sender_email

    with smtplib.SMTP_SSL(
        host="smtp.gmail.com",
        port=465,
        timeout=30,
    ) as smtp:
        smtp.login(sender_email, app_password)
        smtp.send_message(message)


def send_verification_email(
    recipient_email: str,
    user_name: str,
    verification_code: str,
) -> None:
    message = EmailMessage()

    message["Subject"] = (
        "Verify your email - Zero Touch Kubernetes"
    )

    message["To"] = recipient_email

    message.set_content(
        f"""
Hello {user_name},

Your email verification code is:

{verification_code}

This code expires in 10 minutes.

If you did not request a Kubernetes deployment,
you can ignore this email.

Zero Touch Kubernetes Deployment System
        """.strip()
    )

    send_email(message)


def send_deployment_email(
    recipient_email: str,
    user_name: str,
    result: dict,
    data: dict,
) -> None:
    message = EmailMessage()

    message["Subject"] = (
        f'Deployment successful: {result["app_name"]}'
    )

    message["To"] = recipient_email

    message.set_content(
        f"""
Hello {user_name},

Your Kubernetes application was deployed successfully.

Deployment information
----------------------
Cluster name: {result["cluster_name"]}
Number of nodes: {result["node_count"]}
Application name: {result["app_name"]}
Docker image: {data["image"]}
Replicas: {result["replicas"]}
Container port: {data["port"]}
Service name: {result["service_name"]}
Deployment time: {result["duration"]} seconds

CPU request: {data["cpu_request"]}
CPU limit: {data["cpu_limit"]}
Memory request: {data["memory_request"]}
Memory limit: {data["memory_limit"]}

The deployment is running successfully inside Kubernetes.

Zero Touch Kubernetes Deployment System
        """.strip()
    )

    send_email(message)


def collect_form_information() -> tuple:
    cluster_name = (
        request.form["cluster_name"]
        .strip()
        .lower()
    )

    app_name = (
        request.form["app_name"]
        .strip()
        .lower()
    )

    node_count = int(request.form["node_count"])
    replicas = int(request.form["replicas"])
    port = int(request.form["port"])

    if not valid_kubernetes_name(cluster_name):
        raise ValueError(
            "The cluster name is not valid."
        )

    if not valid_kubernetes_name(app_name):
        raise ValueError(
            "The application name is not valid."
        )

    if node_count < 1:
        raise ValueError(
            "The number of nodes must be greater than zero."
        )

    if replicas < 1:
        raise ValueError(
            "The number of replicas must be greater than zero."
        )

    if not 1 <= port <= 65535:
        raise ValueError(
            "The container port must be between 1 and 65535."
        )

    user_information = {
        "user_name": (
            request.form["user_name"].strip()
        ),
        "email": normalize_email(
            request.form["email"]
        ),
    }

    application_values = {
        "app_name": app_name,
        "image": request.form["image"].strip(),
        "replicas": replicas,
        "port": port,
        "cpu_request": (
            request.form["cpu_request"].strip()
        ),
        "cpu_limit": (
            request.form["cpu_limit"].strip()
        ),
        "memory_request": (
            request.form["memory_request"].strip()
        ),
        "memory_limit": (
            request.form["memory_limit"].strip()
        ),
    }

    if not user_information["user_name"]:
        raise ValueError(
            "The user name cannot be empty."
        )

    if not valid_email(user_information["email"]):
        raise ValueError(
            "Please enter a valid email address."
        )

    if not application_values["image"]:
        raise ValueError(
            "The Docker image cannot be empty."
        )

    return (
        cluster_name,
        node_count,
        user_information,
        application_values,
    )


def perform_deployment(
    cluster_name: str,
    node_count: int,
    user_information: dict,
    application_values: dict,
):
    result = deploy_application_from_web(
        cluster_name=cluster_name,
        node_count=node_count,
        values=application_values,
    )

    if result["success"]:
        try:
            send_deployment_email(
                recipient_email=(
                    user_information["email"]
                ),
                user_name=(
                    user_information["user_name"]
                ),
                result=result,
                data=application_values,
            )

            result["email_sent"] = True
            result["email_message"] = (
                "A deployment notification was sent to "
                f'{user_information["email"]}.'
            )

        except (
            OSError,
            RuntimeError,
            smtplib.SMTPException,
        ) as email_error:
            result["email_sent"] = False
            result["email_message"] = (
                "The application was deployed, but the "
                f"notification failed: {email_error}"
            )

            print("\nDeployment email failed.")
            print(f"Error: {email_error}")

    return render_template(
        "result.html",
        result=result,
        user=user_information,
        data=application_values,
    )


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/deploy", methods=["POST"])
def deploy():
    try:
        (
            cluster_name,
            node_count,
            user_information,
            application_values,
        ) = collect_form_information()

        email = user_information["email"]

        if email_is_verified(email):
            print(
                f"\nEmail already verified: {email}"
            )

            return perform_deployment(
                cluster_name=cluster_name,
                node_count=node_count,
                user_information=user_information,
                application_values=application_values,
            )

        verification_code = create_verification_code()

        session["pending_deployment"] = {
            "cluster_name": cluster_name,
            "node_count": node_count,
            "user_information": user_information,
            "application_values": application_values,
        }

        session["verification"] = {
            "email": email,
            "code_hash": hash_verification_code(
                verification_code
            ),
            "expires_at": (
                time.time()
                + VERIFICATION_EXPIRY_SECONDS
            ),
            "attempts": 0,
        }

        send_verification_email(
            recipient_email=email,
            user_name=user_information["user_name"],
            verification_code=verification_code,
        )

        return redirect(url_for("verify_email"))

    except (
        KeyError,
        TypeError,
        ValueError,
        OSError,
        RuntimeError,
        smtplib.SMTPException,
    ) as error:
        result = {
            "success": False,
            "message": str(error),
            "email_sent": False,
        }

        return render_template(
            "result.html",
            result=result,
            user=None,
            data=None,
        )


@app.route("/verify-email", methods=["GET", "POST"])
def verify_email():
    verification = session.get("verification")
    pending_deployment = session.get(
        "pending_deployment"
    )

    if not verification or not pending_deployment:
        return redirect(url_for("home"))

    if request.method == "GET":
        return render_template(
            "verify_email.html",
            email=verification["email"],
            error=None,
        )

    entered_code = (
        request.form.get("verification_code", "")
        .strip()
    )

    if time.time() > verification["expires_at"]:
        session.pop("verification", None)
        session.pop("pending_deployment", None)

        return render_template(
            "verify_email.html",
            email=verification["email"],
            error=(
                "The verification code has expired. "
                "Return to the form and try again."
            ),
        )

    verification["attempts"] += 1
    session["verification"] = verification

    if (
        verification["attempts"]
        > MAX_VERIFICATION_ATTEMPTS
    ):
        session.pop("verification", None)
        session.pop("pending_deployment", None)

        return render_template(
            "verify_email.html",
            email=verification["email"],
            error=(
                "Too many incorrect attempts. "
                "Return to the form and try again."
            ),
        )

    entered_code_hash = hash_verification_code(
        entered_code
    )

    if entered_code_hash != verification["code_hash"]:
        attempts_left = (
            MAX_VERIFICATION_ATTEMPTS
            - verification["attempts"]
        )

        return render_template(
            "verify_email.html",
            email=verification["email"],
            error=(
                "The verification code is incorrect. "
                f"{attempts_left} attempts remaining."
            ),
        )

    save_verified_email(verification["email"])

    session.pop("verification", None)
    session.pop("pending_deployment", None)

    return perform_deployment(
        cluster_name=(
            pending_deployment["cluster_name"]
        ),
        node_count=(
            pending_deployment["node_count"]
        ),
        user_information=(
            pending_deployment["user_information"]
        ),
        application_values=(
            pending_deployment["application_values"]
        ),
    )


if __name__ == "__main__":
    initialize_database()
    app.run(debug=True)