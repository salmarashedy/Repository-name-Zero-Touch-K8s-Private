import json
import os
import shutil
import subprocess
import time
from pathlib import Path


def is_command_available(
    command_name: str,
) -> bool:
    return shutil.which(
        command_name
    ) is not None


def check_required_commands() -> None:
    required_commands = [
        "docker",
        "minikube",
        "kubectl",
    ]

    missing_commands = [
        command
        for command in required_commands
        if not is_command_available(command)
    ]

    if missing_commands:
        raise RuntimeError(
            "Missing required commands: "
            + ", ".join(missing_commands)
        )


def docker_is_running() -> bool:
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=15,
        )

        return result.returncode == 0

    except (
        FileNotFoundError,
        subprocess.TimeoutExpired,
    ):
        return False


def find_docker_desktop() -> Path | None:
    possible_paths = []

    program_files = os.environ.get(
        "ProgramFiles"
    )

    if program_files:
        possible_paths.append(
            Path(program_files)
            / "Docker"
            / "Docker"
            / "Docker Desktop.exe"
        )

    local_app_data = os.environ.get(
        "LOCALAPPDATA"
    )

    if local_app_data:
        possible_paths.append(
            Path(local_app_data)
            / "Docker"
            / "Docker Desktop.exe"
        )

    for path in possible_paths:
        if path.exists():
            return path

    return None


def start_docker_desktop() -> None:
    docker_desktop = find_docker_desktop()

    if docker_desktop is None:
        raise RuntimeError(
            "Docker Desktop is not running and "
            "its application could not be found. "
            "Make sure Docker Desktop is installed."
        )

    try:
        subprocess.Popen(
            [str(docker_desktop)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=(
                subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NEW_PROCESS_GROUP
            ),
        )

    except OSError as error:
        raise RuntimeError(
            "Docker Desktop could not be "
            "started automatically."
        ) from error


def check_docker_running() -> None:
    if docker_is_running():
        print(
            "Docker engine is already running."
        )

        return

    print(
        "Docker is stopped. "
        "Starting Docker Desktop..."
    )

    start_docker_desktop()

    timeout_seconds = 120
    interval_seconds = 3

    deadline = (
        time.monotonic()
        + timeout_seconds
    )

    while time.monotonic() < deadline:
        if docker_is_running():
            print(
                "Docker Desktop started successfully."
            )

            return

        time.sleep(
            interval_seconds
        )

    raise RuntimeError(
        "Docker Desktop was opened, but the "
        "Docker engine did not become ready within "
        "2 minutes. Check Docker Desktop for a WSL, "
        "update, login, or startup message."
    )


def cluster_exists(
    cluster_name: str,
) -> bool:
    try:
        result = subprocess.run(
            [
                "minikube",
                "profile",
                "list",
                "-o",
                "json",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

    except FileNotFoundError as error:
        raise RuntimeError(
            "Minikube was not found."
        ) from error

    except subprocess.TimeoutExpired as error:
        raise RuntimeError(
            "Minikube profile discovery timed out."
        ) from error

    if result.returncode != 0:
        error_message = (
            result.stderr.strip()
            or result.stdout.strip()
        )

        raise RuntimeError(
            "Could not read Minikube profiles: "
            f"{error_message}"
        )

    try:
        profile_data = json.loads(
            result.stdout
        )

    except json.JSONDecodeError as error:
        raise RuntimeError(
            "Minikube returned an invalid "
            "profile list."
        ) from error

    profiles = (
        profile_data.get("valid", [])
        + profile_data.get("invalid", [])
    )

    return any(
        profile.get("Name") == cluster_name
        for profile in profiles
    )


def cluster_is_running(
    cluster_name: str,
) -> bool:
    try:
        result = subprocess.run(
            [
                "minikube",
                "status",
                "-p",
                cluster_name,
                "--format={{.Host}}",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

    except (
        FileNotFoundError,
        subprocess.TimeoutExpired,
    ):
        return False

    if result.returncode != 0:
        return False

    status = (
        result.stdout
        .strip()
        .lower()
    )

    return status == "running"


def get_cluster_nodes(
    cluster_name: str,
) -> list:
    try:
        result = subprocess.run(
            [
                "kubectl",
                "--context",
                cluster_name,
                "get",
                "nodes",
                "-o",
                "json",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

    except FileNotFoundError as error:
        raise RuntimeError(
            "kubectl was not found."
        ) from error

    except subprocess.TimeoutExpired as error:
        raise RuntimeError(
            "Reading Kubernetes nodes timed out."
        ) from error

    if result.returncode != 0:
        error_message = (
            result.stderr.strip()
            or result.stdout.strip()
        )

        raise RuntimeError(
            f"Could not get nodes from cluster "
            f"'{cluster_name}': {error_message}"
        )

    try:
        node_data = json.loads(
            result.stdout
        )

    except json.JSONDecodeError as error:
        raise RuntimeError(
            "kubectl returned invalid "
            "node information."
        ) from error

    return node_data.get(
        "items",
        [],
    )


def node_is_ready(
    node: dict,
) -> bool:
    conditions = (
        node.get("status", {})
        .get("conditions", [])
    )

    for condition in conditions:
        if condition.get("type") == "Ready":
            return (
                condition.get("status")
                == "True"
            )

    return False


def node_is_control_plane(
    node: dict,
) -> bool:
    labels = (
        node.get("metadata", {})
        .get("labels", {})
    )

    return (
        "node-role.kubernetes.io/control-plane"
        in labels
        or "node-role.kubernetes.io/master"
        in labels
    )


def verify_cluster_nodes(
    cluster_name: str,
    expected_node_count: int,
    timeout_seconds: int = 1200,
) -> list:
    print(
        f"Checking that cluster '{cluster_name}' "
        f"has {expected_node_count} node(s)..."
    )

    deadline = (
        time.monotonic()
        + timeout_seconds
    )

    last_node_count = 0
    last_ready_count = 0

    while time.monotonic() < deadline:
        nodes = get_cluster_nodes(
            cluster_name
        )

        ready_nodes = [
            node
            for node in nodes
            if node_is_ready(node)
        ]

        last_node_count = len(nodes)
        last_ready_count = len(
            ready_nodes
        )

        print(
            f"Found {last_node_count} node(s); "
            f"{last_ready_count} Ready."
        )

        if (
            last_node_count
            == expected_node_count
            and last_ready_count
            == expected_node_count
        ):
            node_names = [
                node.get(
                    "metadata",
                    {},
                ).get(
                    "name",
                    "",
                )
                for node in ready_nodes
            ]

            node_names = [
                name
                for name in node_names
                if name
            ]

            print(
                "All requested nodes are Ready."
            )

            return node_names

        time.sleep(5)

    raise RuntimeError(
        f"Cluster '{cluster_name}' did not reach "
        f"{expected_node_count} Ready node(s) within "
        f"{timeout_seconds} seconds. It currently has "
        f"{last_node_count} node(s), with "
        f"{last_ready_count} Ready."
    )


def run_cluster_command(
    command: list[str],
    operation_description: str,
    timeout_seconds: int = 300,
) -> str:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )

    except FileNotFoundError as error:
        raise RuntimeError(
            f"{operation_description} failed because "
            "a required command was not found."
        ) from error

    except subprocess.TimeoutExpired as error:
        raise RuntimeError(
            f"{operation_description} timed out."
        ) from error

    if result.returncode != 0:
        error_message = (
            result.stderr.strip()
            or result.stdout.strip()
            or "No command output was returned."
        )

        raise RuntimeError(
            f"{operation_description} failed: "
            f"{error_message}"
        )

    return result.stdout.strip()


def add_worker_nodes(
    cluster_name: str,
    number_to_add: int,
) -> None:
    for number in range(
        1,
        number_to_add + 1,
    ):
        print(
            f"Adding worker node "
            f"{number} of {number_to_add}..."
        )

        output = run_cluster_command(
            command=[
                "minikube",
                "node",
                "add",
                "--worker",
                "-p",
                cluster_name,
            ],
            operation_description=(
                f"Adding worker node {number}"
            ),
            timeout_seconds=300,
        )

        if output:
            print(output)


def drain_worker_node(
    cluster_name: str,
    node_name: str,
) -> None:
    print(
        f"Draining worker node "
        f"'{node_name}'..."
    )

    output = run_cluster_command(
        command=[
            "kubectl",
            "--context",
            cluster_name,
            "drain",
            node_name,
            "--ignore-daemonsets",
            "--delete-emptydir-data",
            "--force",
            "--timeout=120s",
        ],
        operation_description=(
            f"Draining worker node '{node_name}'"
        ),
        timeout_seconds=150,
    )

    if output:
        print(output)


def uncordon_worker_node(
    cluster_name: str,
    node_name: str,
) -> None:
    try:
        result = subprocess.run(
            [
                "kubectl",
                "--context",
                cluster_name,
                "uncordon",
                node_name,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode != 0:
            print(
                f"Warning: worker node '{node_name}' "
                "could not be uncordoned."
            )

    except (
        FileNotFoundError,
        subprocess.TimeoutExpired,
    ):
        print(
            f"Warning: worker node '{node_name}' "
            "could not be uncordoned."
        )


def delete_worker_nodes(
    cluster_name: str,
    number_to_delete: int,
) -> None:
    nodes = get_cluster_nodes(
        cluster_name
    )

    worker_names = sorted(
        [
            node.get(
                "metadata",
                {},
            ).get(
                "name",
                "",
            )
            for node in nodes
            if not node_is_control_plane(node)
        ],
        reverse=True,
    )

    worker_names = [
        name
        for name in worker_names
        if name
    ]

    if (
        len(worker_names)
        < number_to_delete
    ):
        raise RuntimeError(
            f"Cluster '{cluster_name}' does not "
            "have enough removable worker nodes. "
            "The control-plane node will never be "
            "removed automatically."
        )

    selected_workers = worker_names[
        :number_to_delete
    ]

    for number, node_name in enumerate(
        selected_workers,
        start=1,
    ):
        print(
            f"Removing worker node "
            f"{number} of {number_to_delete}: "
            f"{node_name}"
        )

        drain_worker_node(
            cluster_name=cluster_name,
            node_name=node_name,
        )

        try:
            output = run_cluster_command(
                command=[
                    "minikube",
                    "node",
                    "delete",
                    node_name,
                    "-p",
                    cluster_name,
                ],
                operation_description=(
                    f"Deleting worker node "
                    f"'{node_name}'"
                ),
                timeout_seconds=300,
            )

            if output:
                print(output)

        except RuntimeError:
            uncordon_worker_node(
                cluster_name=cluster_name,
                node_name=node_name,
            )

            raise


def resize_cluster_node_count(
    cluster_name: str,
    requested_node_count: int,
) -> str:
    if requested_node_count < 1:
        raise ValueError(
            "A cluster must have at least "
            "one node."
        )

    current_nodes = get_cluster_nodes(
        cluster_name
    )

    current_node_count = len(
        current_nodes
    )

    if (
        current_node_count
        == requested_node_count
    ):
        return (
            f"cluster already has "
            f"{requested_node_count} node(s)"
        )

    if (
        requested_node_count
        > current_node_count
    ):
        number_to_add = (
            requested_node_count
            - current_node_count
        )

        print(
            f"Increasing cluster '{cluster_name}' "
            f"from {current_node_count} to "
            f"{requested_node_count} nodes."
        )

        add_worker_nodes(
            cluster_name=cluster_name,
            number_to_add=number_to_add,
        )

        verify_cluster_nodes(
            cluster_name=cluster_name,
            expected_node_count=(
                requested_node_count
            ),
        )

        return (
            f"added {number_to_add} "
            "worker node(s)"
        )

    number_to_delete = (
        current_node_count
        - requested_node_count
    )

    print(
        f"Reducing cluster '{cluster_name}' "
        f"from {current_node_count} to "
        f"{requested_node_count} nodes."
    )

    delete_worker_nodes(
        cluster_name=cluster_name,
        number_to_delete=number_to_delete,
    )

    verify_cluster_nodes(
        cluster_name=cluster_name,
        expected_node_count=(
            requested_node_count
        ),
    )

    return (
        f"removed {number_to_delete} "
        "worker node(s)"
    )


def create_new_cluster(
    cluster_name: str,
    node_count: int,
) -> str:
    print(
        f"Cluster '{cluster_name}' does not exist. "
        f"Creating it with {node_count} node(s)..."
    )

    output = run_cluster_command(
        command=[
            "minikube",
            "start",
            "-p",
            cluster_name,
            "--nodes",
            str(node_count),
            "--driver=docker",
        ],
        operation_description=(
            f"Creating cluster '{cluster_name}'"
        ),
        timeout_seconds=600,
    )

    if output:
        print(output)

    verify_cluster_nodes(
        cluster_name=cluster_name,
        expected_node_count=node_count,
    )

    return "created"


def start_existing_cluster(
    cluster_name: str,
) -> None:
    print(
        f"Cluster '{cluster_name}' is stopped. "
        "Starting it..."
    )

    output = run_cluster_command(
        command=[
            "minikube",
            "start",
            "-p",
            cluster_name,
        ],
        operation_description=(
            f"Starting cluster '{cluster_name}'"
        ),
        timeout_seconds=600,
    )

    if output:
        print(output)


def ensure_cluster(
    cluster_name: str,
    node_count: int,
) -> str:
    cluster_name = cluster_name.strip()

    if not cluster_name:
        raise ValueError(
            "The cluster name cannot be empty."
        )

    if node_count < 1:
        raise ValueError(
            "The number of nodes must be "
            "at least 1."
        )

    if not cluster_exists(
        cluster_name
    ):
        return create_new_cluster(
            cluster_name=cluster_name,
            node_count=node_count,
        )

    if not cluster_is_running(
        cluster_name
    ):
        start_existing_cluster(
            cluster_name
        )

        cluster_action = "started"

    else:
        print(
            f"Cluster '{cluster_name}' "
            "is already running."
        )

        cluster_action = "reused"

    resize_result = (
        resize_cluster_node_count(
            cluster_name=cluster_name,
            requested_node_count=node_count,
        )
    )

    verify_cluster_nodes(
        cluster_name=cluster_name,
        expected_node_count=node_count,
    )

    return (
        f"{cluster_action}; "
        f"{resize_result}"
    )


if __name__ == "__main__":
    try:
        test_cluster_name = "multi-node"
        test_node_count = 2

        check_required_commands()
        check_docker_running()

        print(
            "All required commands are available."
        )

        print(
            "Docker engine is running."
        )

        result = ensure_cluster(
            cluster_name=test_cluster_name,
            node_count=test_node_count,
        )

        print(
            f"Cluster result: {result}"
        )

    except (
        RuntimeError,
        ValueError,
    ) as error:
        print(
            f"Error: {error}"
        )