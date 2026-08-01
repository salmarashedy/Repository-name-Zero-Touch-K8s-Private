import json
import shutil
import subprocess
import time


# Check whether a command is installed and available
def is_command_available(command_name: str) -> bool:
    return shutil.which(command_name) is not None


# Check that Docker, Minikube, and kubectl are available
def check_required_commands() -> None:
    required_commands = ["docker", "minikube", "kubectl"]
    missing_commands = []

    for command in required_commands:
        if not is_command_available(command):
            missing_commands.append(command)

    if missing_commands:
        missing_text = ", ".join(missing_commands)

        raise RuntimeError(
            f"Missing required commands: {missing_text}"
        )


# Check whether the Docker engine is running
def check_docker_running() -> None:
    result = subprocess.run(
        ["docker", "info"],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(
            "Docker engine is not running. "
            "Please start Docker Desktop and try again."
        )


# Check whether a Minikube cluster profile exists
def cluster_exists(cluster_name: str) -> bool:
    result = subprocess.run(
        ["minikube", "profile", "list", "-o", "json"],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(
            "Could not read Minikube profiles: "
            f"{result.stderr.strip()}"
        )

    try:
        profile_data = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            "Minikube returned an invalid profile list."
        ) from error

    all_profiles = (
        profile_data.get("valid", [])
        + profile_data.get("invalid", [])
    )

    return any(
        profile.get("Name") == cluster_name
        for profile in all_profiles
    )


# Check whether an existing Minikube cluster is running
def cluster_is_running(cluster_name: str) -> bool:
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
    )

    if result.returncode != 0:
        return False

    cluster_status = result.stdout.strip().lower()

    return cluster_status == "running"


# Get the nodes from the selected Kubernetes cluster
def get_cluster_nodes(cluster_name: str) -> list:
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
    )

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
        node_data = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            "kubectl returned invalid node information."
        ) from error

    return node_data.get("items", [])


# Check whether one Kubernetes node is Ready
def node_is_ready(node: dict) -> bool:
    conditions = node.get("status", {}).get("conditions", [])

    for condition in conditions:
        if condition.get("type") == "Ready":
            return condition.get("status") == "True"

    return False


# Wait until the requested nodes are available and Ready
def verify_cluster_nodes(
    cluster_name: str,
    expected_node_count: int,
    timeout_seconds: int = 120,
) -> list:
    print(
        f"Checking that cluster '{cluster_name}' has "
        f"{expected_node_count} node(s)..."
    )

    end_time = time.time() + timeout_seconds

    while time.time() < end_time:
        nodes = get_cluster_nodes(cluster_name)
        ready_nodes = [
            node for node in nodes
            if node_is_ready(node)
        ]

        print(
            f"Found {len(nodes)} node(s); "
            f"{len(ready_nodes)} Ready."
        )

        if len(nodes) != expected_node_count:
            raise RuntimeError(
                f"Cluster '{cluster_name}' has {len(nodes)} "
                f"node(s), but {expected_node_count} were requested."
            )

        if len(ready_nodes) == expected_node_count:
            node_names = [
                node["metadata"]["name"]
                for node in ready_nodes
            ]

            print("All requested nodes are Ready.")
            return node_names

        time.sleep(5)

    raise RuntimeError(
        f"The nodes in cluster '{cluster_name}' did not "
        f"become Ready within {timeout_seconds} seconds."
    )


# Automatically create, start, or reuse a Minikube cluster
def ensure_cluster(
    cluster_name: str,
    node_count: int,
) -> str:
    if not cluster_name.strip():
        raise ValueError("The cluster name cannot be empty.")

    if node_count < 1:
        raise ValueError(
            "The number of nodes must be at least 1."
        )

    if not cluster_exists(cluster_name):
        print(
            f"Cluster '{cluster_name}' does not exist. "
            f"Creating it with {node_count} node(s)..."
        )

        command = [
            "minikube",
            "start",
            "-p",
            cluster_name,
            "--nodes",
            str(node_count),
            "--driver=docker",
        ]

        action = "created"

    elif not cluster_is_running(cluster_name):
        print(
            f"Cluster '{cluster_name}' is stopped. "
            "Starting it..."
        )

        command = [
            "minikube",
            "start",
            "-p",
            cluster_name,
        ]

        action = "started"

    else:
        print(
            f"Cluster '{cluster_name}' is already running."
        )

        verify_cluster_nodes(
            cluster_name,
            node_count,
        )

        return "already running"

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        error_message = (
            result.stderr.strip()
            or result.stdout.strip()
        )

        raise RuntimeError(
            f"Cluster operation failed for "
            f"'{cluster_name}': {error_message}"
        )

    print(
        f"Cluster '{cluster_name}' was "
        f"{action} successfully."
    )

    verify_cluster_nodes(
        cluster_name,
        node_count,
    )

    return action


# Test the cluster manager directly
if __name__ == "__main__":
    try:
        cluster_name = "multi-node"
        node_count = 2

        check_required_commands()
        check_docker_running()
        

        print("All required commands are available.")
        print("Docker engine is running.")

        cluster_result = ensure_cluster(
            cluster_name,
            node_count,
        )

        print(f"Cluster result: {cluster_result}")

    except (RuntimeError, ValueError) as error:
        print(f"Error: {error}")