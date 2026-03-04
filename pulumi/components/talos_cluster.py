"""TalosCluster Pulumi Component"""

import pulumi
import pulumi_kubernetes as kubernetes
import pulumi_proxmoxve as proxmoxve
import pulumiverse_talos as talos
import base64
import yaml
from pulumi_command import local as command
from talos_config import create_talos_secrets
from components.talos_node import TalosNode, TalosNodeArgs


class TalosClusterArgs:
    """Arguments for TalosCluster component"""

    def __init__(
        self,
        cluster_name: str,
        nodes: list[dict],
        gateway: str,
        image_factories: dict,  # {"default": factory, "gpu": factory, ...}
        talos_version: str,
        kubernetes_version: str = None,
        cluster_endpoint_ip: str = None,
        use_cilium: bool = False,
        cilium_version: str = "1.16.0",
        proxmox_provider: proxmoxve.Provider = None,
    ):
        self.cluster_name = cluster_name
        self.nodes = nodes
        self.gateway = gateway
        self.image_factories = image_factories
        self.talos_version = talos_version
        self.kubernetes_version = kubernetes_version
        self.cluster_endpoint_ip = cluster_endpoint_ip or nodes[0]["ip"]
        self.use_cilium = use_cilium
        self.cilium_version = cilium_version
        self.proxmox_provider = proxmox_provider


class TalosCluster(pulumi.ComponentResource):
    """
    A Pulumi ComponentResource that creates a complete Talos Kubernetes cluster:
    - Generates Talos secrets
    - Creates multiple TalosNode components
    - Bootstraps the cluster
    - Waits for cluster health
    - Generates kubeconfig and talosconfig
    - Creates Kubernetes provider
    """

    def __init__(
        self,
        name: str,
        args: TalosClusterArgs,
        opts: pulumi.ResourceOptions = None,
    ):
        super().__init__("custom:talos:Cluster", name, {}, opts)

        # Create Talos secrets
        self.talos_secrets = create_talos_secrets(
            args.cluster_name,
            talos_version=args.talos_version,
        )

        # Create nodes
        self.nodes = []
        self.kubeconfig_raw = None
        self.controlplane_ips = []
        bootstrap_resources = []
        previous_config_apply = None  # Track dependencies for sequential updates

        cluster_endpoint = f"https://{args.cluster_endpoint_ip}:6443"

        # Separate nodes: controlplane first, then workers
        controlplane_configs = [n for n in args.nodes if n["role"] == "controlplane"]
        worker_configs = [n for n in args.nodes if n["role"] == "worker"]
        ordered_node_configs = controlplane_configs + worker_configs

        for node_config in ordered_node_configs:
            is_bootstrap = (
                node_config["role"] == "controlplane"
                and node_config["ip"] == args.cluster_endpoint_ip
            )

            # Select image factory for this node
            image_profile = node_config.get("talosImage", "default")
            if image_profile not in args.image_factories:
                raise ValueError(
                    f"Image profile '{image_profile}' not found in image_factories"
                )

            image_factory = args.image_factories[image_profile]

            # Build dependencies: each node waits for previous node's config to be applied
            node_dependencies = [previous_config_apply] if previous_config_apply else []

            node = TalosNode(
                node_config["name"],
                TalosNodeArgs(
                    name=node_config["name"],
                    ip=node_config["ip"],
                    role=node_config["role"],
                    gateway=args.gateway,
                    talos_secrets=self.talos_secrets,
                    cluster_name=args.cluster_name,
                    cluster_endpoint=cluster_endpoint,
                    talos_installer_image=image_factory.installer_image,
                    talos_iso_file_id=image_factory.iso_file_id,
                    node_type=node_config.get("type", "proxmox"),
                    cpu=node_config.get("cpu", 2),
                    memory=node_config.get("memory", 2048),
                    install_disk=node_config.get("install_disk", "/dev/sda"),
                    disks=node_config.get("disks", []),
                    machine=node_config.get("machine", "q35"),
                    pcie_devices=node_config.get("pcie_devices", []),
                    node_labels=node_config.get("labels", {}),
                    node_taints=node_config.get("taints", []),
                    proxmox_provider=args.proxmox_provider,
                    use_cilium=args.use_cilium,
                    cilium_version=args.cilium_version,
                    kubernetes_version=args.kubernetes_version,
                    is_bootstrap=is_bootstrap,
                    config_dependencies=node_dependencies,
                ),
                opts=pulumi.ResourceOptions(parent=self),
            )

            self.nodes.append(node)
            previous_config_apply = node.config_apply  # Track for next node

            if is_bootstrap:
                self.kubeconfig_raw = node.kubeconfig.kubeconfig_raw
                self.controlplane_ips.append(node_config["ip"])
                bootstrap_resources.append(node.bootstrap)
            elif node_config["role"] == "controlplane":
                self.controlplane_ips.append(node_config["ip"])

        # Generate talosconfig
        self.talosconfig_yaml = self._create_talosconfig(
            args.cluster_name, args.cluster_endpoint_ip
        )

        # Wait for cluster health
        self.health_check = self._create_health_check(
            args.cluster_endpoint_ip, bootstrap_resources
        )

        # Create Kubernetes provider
        self.k8s_provider = kubernetes.Provider(
            f"{name}-k8s-provider",
            kubeconfig=self.kubeconfig_raw,
            enable_server_side_apply=True,
            opts=pulumi.ResourceOptions(
                parent=self,
                depends_on=[self.health_check],
            ),
        )

        self.register_outputs(
            {
                "kubeconfig": self.kubeconfig_raw,
                "talosconfig": self.talosconfig_yaml,
                "cluster_endpoint": cluster_endpoint,
                "controlplane_ips": self.controlplane_ips,
            }
        )

    def _create_talosconfig(
        self, cluster_name: str, cluster_endpoint_ip: str
    ) -> pulumi.Output[str]:
        """Generate talosconfig YAML"""

        def create_talosconfig_dict(client_config):
            return {
                "context": cluster_name,
                "contexts": {
                    cluster_name: {
                        "endpoints": [cluster_endpoint_ip],
                        "nodes": [cluster_endpoint_ip],
                        "ca": client_config["ca_certificate"],
                        "crt": client_config["client_certificate"],
                        "key": client_config["client_key"],
                    }
                },
            }

        talosconfig_dict = self.talos_secrets.client_configuration.apply(
            create_talosconfig_dict
        )
        return talosconfig_dict.apply(
            lambda cfg: yaml.dump(cfg, default_flow_style=False)
        )

    def _create_health_check(
        self, cluster_endpoint_ip: str, bootstrap_resources: list
    ) -> command.Command:
        """Wait for Talos cluster to report healthy"""
        talosconfig_b64 = self.talosconfig_yaml.apply(
            lambda cfg: base64.b64encode(cfg.encode("utf-8")).decode("utf-8")
        )

        return command.Command(
            "cluster-health-check",
            create=talosconfig_b64.apply(
                lambda b64: (
                    "set -euo pipefail; "
                    "printf %s '" + b64 + "' | base64 -d > /tmp/talosconfig.yaml; "
                    "for i in $(seq 1 60); do "
                    f"talosctl --talosconfig /tmp/talosconfig.yaml -n {cluster_endpoint_ip} health && exit 0; "
                    "sleep 10; "
                    "done; "
                    "exit 1"
                )
            ),
            delete="true",
            opts=pulumi.ResourceOptions(
                parent=self,
                depends_on=bootstrap_resources if bootstrap_resources else [],
            ),
        )
