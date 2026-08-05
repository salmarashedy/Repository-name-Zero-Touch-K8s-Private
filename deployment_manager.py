import json
import re
import subprocess
import time
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from kubernetes import config

from cluster_manager import (
    check_docker_running,
    check_required_commands,
    ensure_cluster,
    cluster_is_running,
    get_cluster_nodes,
)


def list_cluster_choices(allowed_names: set[str] | None = None) -> list[dict]:
    """Return Minikube profiles without changing their state."""
    result = subprocess.run(
        ["minikube", "profile", "list", "-o", "json"],
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Could not list Minikube clusters.")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("Minikube returned invalid profile information.") from error

    choices = []
    for profile in payload.get("valid", []) + payload.get("invalid", []):
        name = profile.get("Name")
        if not name:
            continue
        if allowed_names is not None and name not in allowed_names:
            continue
        running = cluster_is_running(name)
        config_nodes = profile.get("Config", {}).get("Nodes", [])
        if isinstance(config_nodes, list):
            configured_count = len(config_nodes)
        elif isinstance(config_nodes, int):
            configured_count = config_nodes
        elif isinstance(config_nodes, str) and config_nodes.isdigit():
            configured_count = int(config_nodes)
        else:
            configured_count = 0
        if running:
            try:
                node_count = len(get_cluster_nodes(name))
            except RuntimeError:
                node_count = configured_count
        else:
            node_count = configured_count
        choices.append({"name": name, "node_count": node_count, "running": running})
    return sorted(choices, key=lambda item: item["name"])


def _cpu_to_millicores(value: str) -> int:
    value = str(value or "").strip()
    if value.endswith("m") and value[:-1].isdigit():
        return int(value[:-1])
    if re.fullmatch(r"\d+", value):
        return int(value) * 1000
    return 0


def _memory_to_mib(value: str) -> int:
    value = str(value or "").strip()
    match = re.fullmatch(r"(\d+)(Ki|Mi|Gi)", value)
    if not match:
        return 0
    amount, unit = int(match.group(1)), match.group(2)
    return {"Ki": amount // 1024, "Mi": amount, "Gi": amount * 1024}[unit]


def list_application_choices(
    allowed_cluster_names: set[str] | None = None,
    allowed_application_keys: set[str] | None = None,
) -> list[dict]:
    """Discover Deployments from running Minikube clusters."""
    applications = []
    for cluster in list_cluster_choices(allowed_cluster_names):
        if not cluster["running"]:
            continue
        result = subprocess.run(
            ["kubectl", "--context", cluster["name"], "get", "deployments", "-A", "-o", "json"],
            check=False,
            text=True,
            capture_output=True,
        )
        if result.returncode != 0:
            continue
        try:
            items = json.loads(result.stdout).get("items", [])
        except json.JSONDecodeError:
            continue
        for item in items:
            metadata = item.get("metadata", {})
            namespace = metadata.get("namespace", "default")
            if namespace == "kube-system":
                continue
            deployment_name = metadata.get("name", "")
            labels = metadata.get("labels", {})
            app_name = labels.get("app") or deployment_name.removesuffix("-deployment")
            containers = item.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
            container = containers[0] if containers else {}
            resources = container.get("resources", {}).get("requests", {})
            ports = container.get("ports", [])
            application_key = f'{cluster["name"]}|{namespace}|{app_name}'
            if allowed_application_keys is not None and application_key not in allowed_application_keys:
                continue
            applications.append({
                "key": application_key,
                "cluster_name": cluster["name"],
                "node_count": cluster["node_count"],
                "namespace": namespace,
                "app_name": app_name,
                "deployment_name": deployment_name,
                "image": container.get("image", ""),
                "replicas": item.get("spec", {}).get("replicas", 1),
                "port": ports[0].get("containerPort", 80) if ports else 80,
                "cpu_request": _cpu_to_millicores(resources.get("cpu", "")),
                "memory_request": _memory_to_mib(resources.get("memory", "")),
            })
    return sorted(applications, key=lambda item: (item["cluster_name"], item["app_name"]))

from input_handler import (
    ask_app_name,
    ask_cluster_name,
    ask_non_empty,
    ask_port,
    ask_positive_integer,
    ask_yes_no,
)


# Render a Jinja2 template and save it as a YAML file
def render_template(
    environment: Environment,
    template_name: str,
    output_path: Path,
    values: dict,
) -> None:

    template = environment.get_template(template_name)
    rendered_content = template.render(**values)

    output_path.write_text(
        rendered_content,
        encoding="utf-8",
    )

    print(f"Generated: {output_path.name}")


# Apply one generated YAML file to a Kubernetes cluster
def apply_yaml(
    file_path: Path,
    cluster_name: str,
) -> bool:

    try:
        result = subprocess.run(
            [
                "kubectl",
                "--context",
                cluster_name,
                "apply",
                "-f",
                str(file_path),
            ],
            check=True,
            text=True,
            capture_output=True,
        )

        print(result.stdout.strip())
        return True

    except FileNotFoundError:
        print(
            "\nError: kubectl was not found. "
            "Make sure kubectl is installed and added to PATH."
        )
        return False

    except subprocess.CalledProcessError as error:
        print(f"\nFailed to apply {file_path.name}.")

        if error.stdout:
            print(error.stdout.strip())

        if error.stderr:
            print(error.stderr.strip())

        return False


# Wait until the Kubernetes Deployment becomes ready
def wait_for_deployment(
    app_name: str,
    cluster_name: str,
) -> bool:

    deployment_name = f"{app_name}-deployment"

    print("\nWaiting for the Deployment to become ready...")
    print("-" * 50)

    try:
        result = subprocess.run(
            [
                "kubectl",
                "--context",
                cluster_name,
                "rollout",
                "status",
                f"deployment/{deployment_name}",
                "--timeout=120s",
            ],
            check=True,
            text=True,
            capture_output=True,
        )

        print(result.stdout.strip())
        return True

    except FileNotFoundError:
        print("kubectl was not found.")
        return False

    except subprocess.CalledProcessError as error:
        print("\nThe Deployment did not become ready.")

        if error.stdout:
            print(error.stdout.strip())

        if error.stderr:
            print(error.stderr.strip())

        return False


# Display the Kubernetes resources created for an application
def show_application_status(
    app_name: str,
    cluster_name: str,
) -> None:

    print("\nKubernetes Resource Status")
    print("=" * 60)
    print(f"Cluster: {cluster_name}")

    commands = [
        (
            "Deployment",
            [
                "kubectl",
                "--context",
                cluster_name,
                "get",
                "deployment",
                f"{app_name}-deployment",
                "-o",
                "wide",
            ],
        ),
        (
            "Pods",
            [
                "kubectl",
                "--context",
                cluster_name,
                "get",
                "pods",
                "-l",
                f"app={app_name}",
                "-o",
                "wide",
            ],
        ),
        (
            "Service",
            [
                "kubectl",
                "--context",
                cluster_name,
                "get",
                "service",
                f"{app_name}-service",
                "-o",
                "wide",
            ],
        ),
    ]

    for title, command in commands:
        print(f"\n{title}")
        print("-" * 60)

        try:
            subprocess.run(
                command,
                check=False,
            )

        except FileNotFoundError:
            print("kubectl was not found.")
            return


# Check whether a Kubernetes context exists
def context_exists(cluster_name: str) -> bool:

    try:
        result = subprocess.run(
            [
                "kubectl",
                "config",
                "get-contexts",
                cluster_name,
                "-o",
                "name",
            ],
            check=False,
            text=True,
            capture_output=True,
        )

        return result.stdout.strip() == cluster_name

    except FileNotFoundError:
        print(
            "\nError: kubectl was not found. "
            "Make sure kubectl is installed and added to PATH."
        )
        return False


# Check whether an application Deployment already exists
def deployment_exists(
    app_name: str,
    cluster_name: str,
) -> bool:

    try:
        result = subprocess.run(
            [
                "kubectl",
                "--context",
                cluster_name,
                "get",
                "deployment",
                f"{app_name}-deployment",
            ],
            check=False,
            text=True,
            capture_output=True,
        )

        return result.returncode == 0

    except FileNotFoundError:
        return False


# Collect application information from the terminal user
def collect_application_information() -> dict:

    app_name = ask_app_name("Application name: ")
    image = ask_non_empty("Docker image: ")
    replicas = ask_positive_integer("Number of replicas: ")
    port = ask_port("Container port: ")

    cpu_request = ask_non_empty(
        "CPU request, for example 100m: "
    )

    cpu_limit = ask_non_empty(
        "CPU limit, for example 500m: "
    )

    memory_request = ask_non_empty(
        "Memory request, for example 128Mi: "
    )

    memory_limit = ask_non_empty(
        "Memory limit, for example 256Mi: "
    )

    return {
        "app_name": app_name,
        "image": image,
        "replicas": replicas,
        "port": port,
        "cpu_request": cpu_request,
        "cpu_limit": cpu_limit,
        "memory_request": memory_request,
        "memory_limit": memory_limit,
        "namespace": "default",
        "service_type": "ClusterIP",
    }


# Display application settings before deployment
def show_deployment_summary(
    cluster_name: str,
    node_count: int,
    values: dict,
) -> None:

    print("\nDeployment Summary")
    print("-" * 50)
    print(f"Target cluster   : {cluster_name}")
    print(f"Cluster nodes    : {node_count}")
    print(f"Application name : {values['app_name']}")
    print(f"Docker image     : {values['image']}")
    print(f"Replicas         : {values['replicas']}")
    print(f"Container port   : {values['port']}")
    print(f"CPU request      : {values['cpu_request']}")
    print(f"CPU limit        : {values['cpu_limit']}")
    print(f"Memory request   : {values['memory_request']}")
    print(f"Memory limit     : {values['memory_limit']}")
    print("Namespace        : default")
    print("Service type     : ClusterIP")


# Generate the Deployment and Service YAML files
def generate_yaml_files(
    values: dict,
) -> tuple[Path, Path] | None:

    project_directory = Path(__file__).resolve().parent
    templates_directory = project_directory / "templates"
    generated_directory = project_directory / "generated"

    deployment_template = (
        templates_directory / "deployment.yaml.j2"
    )

    service_template = (
        templates_directory / "service.yaml.j2"
    )

    if not deployment_template.exists():
        print(
            "\nTemplate not found: "
            "templates/deployment.yaml.j2"
        )
        return None

    if not service_template.exists():
        print(
            "\nTemplate not found: "
            "templates/service.yaml.j2"
        )
        return None

    generated_directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    environment = Environment(
        loader=FileSystemLoader(
            str(templates_directory)
        ),
        undefined=StrictUndefined,
        autoescape=False,
    )

    app_name = values["app_name"]

    deployment_output = (
        generated_directory
        / f"{app_name}-deployment.yaml"
    )

    service_output = (
        generated_directory
        / f"{app_name}-service.yaml"
    )

    print("\nGenerating Kubernetes YAML files...")
    print("-" * 50)

    try:
        render_template(
            environment=environment,
            template_name="deployment.yaml.j2",
            output_path=deployment_output,
            values=values,
        )

        render_template(
            environment=environment,
            template_name="service.yaml.j2",
            output_path=service_output,
            values=values,
        )

    except Exception as error:
        print("\nFailed to generate the YAML files.")
        print(f"Error: {error}")
        return None

    return deployment_output, service_output


# Deploy an application using data received from Flask
def deploy_application_from_web(
    cluster_name: str,
    node_count: int,
    values: dict,
) -> dict:

    application_start_time = None

    try:
        print("\nWeb deployment request received")
        print("=" * 60)

        check_required_commands()
        check_docker_running()

        print("All required commands are available.")
        print("Docker engine is running.")

        cluster_result = ensure_cluster(
            cluster_name=cluster_name,
            node_count=node_count,
        )

        print(f"Cluster result: {cluster_result}")

        if not context_exists(cluster_name):
            raise RuntimeError(
                f'Kubernetes context "{cluster_name}" '
                "was not found after preparing the cluster."
            )

        config.load_kube_config(
            context=cluster_name,
        )

        print(
            f'Connected successfully to "{cluster_name}".'
        )

        # Begin timing only the application deployment.
        application_start_time = time.perf_counter()

        values.setdefault("namespace", "default")
        values["service_type"] = "ClusterIP"

        generated_files = generate_yaml_files(values)

        if generated_files is None:
            raise RuntimeError(
                "The Kubernetes YAML files "
                "could not be generated."
            )

        deployment_file, service_file = generated_files

        print("\nApplying Kubernetes resources...")
        print("-" * 50)

        deployment_applied = apply_yaml(
            file_path=deployment_file,
            cluster_name=cluster_name,
        )

        if not deployment_applied:
            raise RuntimeError(
                "The Kubernetes Deployment "
                "could not be applied."
            )

        service_applied = apply_yaml(
            file_path=service_file,
            cluster_name=cluster_name,
        )

        if not service_applied:
            raise RuntimeError(
                "The Deployment was applied, "
                "but the Service failed."
            )

        print("\nResources applied successfully.")

        deployment_ready = wait_for_deployment(
            app_name=values["app_name"],
            cluster_name=cluster_name,
        )

        application_duration = (
            time.perf_counter()
            - application_start_time
        )

        if not deployment_ready:
            raise RuntimeError(
                "The resources were applied, but the "
                "Deployment did not become ready "
                "within 120 seconds."
            )

        show_application_status(
            app_name=values["app_name"],
            cluster_name=cluster_name,
        )

        print(
            "\nWeb deployment completed in "
            f"{application_duration:.2f} seconds."
        )

        return {
            "success": True,
            "message": "Application deployed successfully.",
            "cluster_result": cluster_result,
            "cluster_name": cluster_name,
            "node_count": node_count,
            "app_name": values["app_name"],
            "replicas": values["replicas"],
            "service_name": (
                f'{values["app_name"]}-service'
            ),
            "application_duration": round(
                application_duration,
                2,
            ),
        }

    except Exception as error:
        application_duration = (
            time.perf_counter()
            - application_start_time
            if application_start_time is not None
            else None
        )

        print("\nWeb deployment failed.")
        print(f"Error: {error}")

        return {
            "success": False,
            "message": str(error),
            "cluster_name": cluster_name,
            "app_name": values.get(
                "app_name",
                "Unknown",
            ),
            "application_duration": (
                round(application_duration, 2)
                if application_duration is not None
                else None
            ),
        }


def _run_checked(command: list[str]) -> str:
    result = subprocess.run(command, check=True, text=True, capture_output=True)
    return result.stdout.strip()


def execute_web_action(
    action: str,
    cluster: dict,
    app_data: dict | None,
) -> dict:
    
    #Execute an operation after the staged forms and one-use confirmation pass.


    cluster_name = cluster["cluster_name"]
    node_count = cluster["node_count"]
    cluster_start_time = None

    try:
        if action == "create":
            cluster_start_time = time.perf_counter()

            check_required_commands()
            check_docker_running()

            cluster_result = ensure_cluster(
                cluster_name,
                node_count,
            )

            cluster_duration = (
                time.perf_counter()
                - cluster_start_time
            )

            return {
                "success": True,
                "message": (
                    f"Cluster '{cluster_name}' is ready."
                ),
                "checks": [
                    cluster_result,
                    (
                        f"Verified {node_count} "
                        "Ready node(s)."
                    ),
                ],
                "cluster_duration": round(
                    cluster_duration,
                    2,
                ),
            }

        if app_data is None:
            raise ValueError(
                "Application information is missing."
            )

        app_name = app_data["app_name"]

        namespace = app_data.get(
            "namespace",
            "default",
        )

        if action in {"deploy", "update"}:
            values = dict(app_data)

            values["cpu_request"] = (
                f'{values["cpu_request"]}m'
            )

            values["cpu_limit"] = "1000m"

            values["memory_request"] = (
                f'{values["memory_request"]}Mi'
            )

            values["memory_limit"] = "1024Mi"

            result = deploy_application_from_web(
                cluster_name,
                node_count,
                values,
            )

            if result.get("success"):
                if action == "update":
                    result["message"] = (
                        "Application updated and verified."
                    )

                else:
                    result["message"] = (
                        "Application deployed and verified."
                    )

            return result

        if not context_exists(cluster_name):
            raise RuntimeError(
                f"Cluster '{cluster_name}' "
                "does not exist."
            )

        if action == "delete":
            deployment = (
                f"{app_name}-deployment"
            )

            service = (
                f"{app_name}-service"
            )

            _run_checked(
                [
                    "kubectl",
                    "--context",
                    cluster_name,
                    "-n",
                    namespace,
                    "delete",
                    "deployment",
                    deployment,
                ]
            )

            _run_checked(
                [
                    "kubectl",
                    "--context",
                    cluster_name,
                    "-n",
                    namespace,
                    "delete",
                    "service",
                    service,
                ]
            )

            return {
                "success": True,
                "message": (
                    f"Application '{app_name}' "
                    "was deleted."
                ),
                "checks": [
                    "Deployment deleted.",
                    "Service deleted.",
                ],
            }

        if action == "inspect":
            output = _run_checked(
                [
                    "kubectl",
                    "--context",
                    cluster_name,
                    "-n",
                    namespace,
                    "get",
                    "deployment,pods,service",
                    "-l",
                    f"app={app_name}",
                    "-o",
                    "wide",
                ]
            )

            return {
                "success": True,
                "message": (
                    "Current Kubernetes status "
                    "collected."
                ),
                "details": output,
            }

        raise ValueError(
            "Unsupported action."
        )

    except (
        FileNotFoundError,
        subprocess.CalledProcessError,
        RuntimeError,
        ValueError,
    ) as error:
        if (
            isinstance(
                error,
                subprocess.CalledProcessError,
            )
            and error.stderr
        ):
            detail = error.stderr.strip()

        else:
            detail = str(error)

        result = {
            "success": False,
            "message": detail,
        }

        if (
            action == "create"
            and cluster_start_time is not None
        ):
            result["cluster_duration"] = round(
                time.perf_counter()
                - cluster_start_time,
                2,
            )

        return result
# Complete terminal zero-touch deployment workflow
def deploy_application_workflow() -> None:

    print("\nDeploy Application to Kubernetes")
    print("=" * 60)

    cluster_name = ask_cluster_name(
        "Target cluster name: "
    )

    node_count = ask_positive_integer(
        "Number of cluster nodes: "
    )

    print("\nPreparing the Kubernetes cluster...")
    print("-" * 60)

    try:
        check_required_commands()
        check_docker_running()

        print("All required commands are available.")
        print("Docker engine is running.")

        cluster_result = ensure_cluster(
            cluster_name=cluster_name,
            node_count=node_count,
        )

        print(f"Cluster result: {cluster_result}")

    except (RuntimeError, ValueError) as error:
        print("\nCluster preparation failed.")
        print(f"Error: {error}")
        return

    if not context_exists(cluster_name):
        print(
            f'\nKubernetes context "{cluster_name}" '
            "was not found after preparing the cluster."
        )
        return

    print("\nLoading Kubernetes configuration...")

    try:
        config.load_kube_config(
            context=cluster_name,
        )

        print(
            f'Connected successfully to "{cluster_name}".'
        )

    except Exception as error:
        print(
            f'\nCould not connect to cluster '
            f'"{cluster_name}".'
        )
        print(f"Error: {error}")
        return

    values = collect_application_information()
    app_name = values["app_name"]

    show_deployment_summary(
        cluster_name=cluster_name,
        node_count=node_count,
        values=values,
    )

    if deployment_exists(
        app_name=app_name,
        cluster_name=cluster_name,
    ):
        print(
            f'\nA Deployment named '
            f'"{app_name}-deployment" already exists.'
        )

        print(
            "Continuing will update the existing "
            "Deployment and Service."
        )

        continue_update = ask_yes_no(
            "Do you want to update it? [y/n]: "
        )

        if not continue_update:
            print("\nOperation cancelled.")
            return

    else:
        confirmation = ask_yes_no(
            "\nGenerate and deploy these resources? [y/n]: "
        )

        if not confirmation:
            print("\nOperation cancelled.")
            return

    generated_files = generate_yaml_files(values)

    if generated_files is None:
        return

    deployment_output, service_output = generated_files

    print("\nApplying Kubernetes resources...")
    print("-" * 50)

    deployment_start_time = time.perf_counter()

    deployment_applied = apply_yaml(
        file_path=deployment_output,
        cluster_name=cluster_name,
    )

    if not deployment_applied:
        print("\nDeployment failed.")
        return

    service_applied = apply_yaml(
        file_path=service_output,
        cluster_name=cluster_name,
    )

    if not service_applied:
        print(
            "\nThe Deployment was applied, "
            "but the Service failed."
        )
        return

    print("\nResources applied successfully.")

    deployment_ready = wait_for_deployment(
        app_name=app_name,
        cluster_name=cluster_name,
    )

    deployment_duration = (
        time.perf_counter() - deployment_start_time
    )

    if not deployment_ready:
        print(
            "\nThe resources were applied, but the "
            "Deployment is not ready yet."
        )

        print(
            "Check the Pod status and logs "
            "for more information."
        )

        print(
            f"Elapsed time before failure: "
            f"{deployment_duration:.2f} seconds."
        )

    else:
        print(
            f"\nDeployment completed in "
            f"{deployment_duration:.2f} seconds."
        )

    show_application_status(
        app_name=app_name,
        cluster_name=cluster_name,
    )

    print("\n" + "=" * 60)
    print("Application deployment process completed.")
    print("=" * 60)
