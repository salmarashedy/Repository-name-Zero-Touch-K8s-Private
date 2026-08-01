import subprocess
import time
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from kubernetes import config

from cluster_manager import (
    check_docker_running,
    check_required_commands,
    ensure_cluster,
)

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

        # These values are fixed because they are not entered
        # separately in the web form.
        values["namespace"] = "default"
        values["service_type"] = "ClusterIP"

        generated_files = generate_yaml_files(values)

        if generated_files is None:
            raise RuntimeError(
                "The Kubernetes YAML files could not be generated."
            )

        deployment_file, service_file = generated_files

        print("\nApplying Kubernetes resources...")
        print("-" * 50)

        deployment_start_time = time.perf_counter()

        deployment_applied = apply_yaml(
            file_path=deployment_file,
            cluster_name=cluster_name,
        )

        if not deployment_applied:
            raise RuntimeError(
                "The Kubernetes Deployment could not be applied."
            )

        service_applied = apply_yaml(
            file_path=service_file,
            cluster_name=cluster_name,
        )

        if not service_applied:
            raise RuntimeError(
                "The Deployment was applied, but the Service failed."
            )

        print("\nResources applied successfully.")

        deployment_ready = wait_for_deployment(
            app_name=values["app_name"],
            cluster_name=cluster_name,
        )

        deployment_duration = (
            time.perf_counter() - deployment_start_time
        )

        if not deployment_ready:
            raise RuntimeError(
                "The resources were applied, but the Deployment "
                "did not become ready within 120 seconds."
            )

        show_application_status(
            app_name=values["app_name"],
            cluster_name=cluster_name,
        )

        print(
            f"\nWeb deployment completed in "
            f"{deployment_duration:.2f} seconds."
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
            "duration": round(
                deployment_duration,
                2,
            ),
        }

    except Exception as error:
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
        }


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