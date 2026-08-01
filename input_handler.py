import re


def ask_non_empty(prompt: str) -> str:
    while True:
        value = input(prompt).strip()

        if value:
            return value

        print("This value cannot be empty.")


def ask_positive_integer(prompt: str) -> int:
    while True:
        value = input(prompt).strip()

        try:
            number = int(value)

            if number > 0:
                return number

            print("Please enter a number greater than 0.")

        except ValueError:
            print("Please enter a valid whole number.")


def ask_port(prompt: str) -> int:
    while True:
        value = input(prompt).strip()

        try:
            port = int(value)

            if 1 <= port <= 65535:
                return port

            print("Port must be between 1 and 65535.")

        except ValueError:
            print("Please enter a valid whole number.")


def ask_app_name(prompt: str) -> str:
    while True:
        value = input(prompt).strip().lower()

        valid_name = re.fullmatch(
            r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?",
            value,
        )

        if valid_name and len(value) <= 63:
            return value

        print(
            "Use lowercase letters, numbers, and hyphens only. "
            "The name cannot start or end with a hyphen "
            "and must be 63 characters or fewer."
        )


def ask_cluster_name(prompt: str) -> str:
    while True:
        value = input(prompt).strip().lower()

        valid_name = re.fullmatch(
            r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?",
            value,
        )

        if valid_name and len(value) <= 63:
            return value

        print(
            "Use lowercase letters, numbers, and hyphens only. "
            "The cluster name cannot start or end with a hyphen."
        )


def ask_yes_no(prompt: str) -> bool:
    while True:
        answer = input(prompt).strip().lower()

        if answer in {"y", "yes"}:
            return True

        if answer in {"n", "no"}:
            return False

        print("Please enter y or n.")