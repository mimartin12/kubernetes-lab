import pulumi
import pulumiverse_talos as talos
import json
import copy
from pathlib import Path


def _get_repo_root() -> Path:
    """Get the repository root directory."""
    return Path(__file__).parent.parent


def _read_cilium_values() -> str:
    """Read Cilium values from the ArgoCD values file."""
    values_path = (
        _get_repo_root() / "argocd" / "applications" / "values" / "cilium.yaml"
    )
    with open(values_path, "r") as f:
        return f.read()


def _get_cilium_inline_manifests(cilium_version: str = "1.16.0") -> list:
    """
    Build inline manifests for Cilium bootstrap.
    Returns list of dicts with 'name' and 'contents' keys.
    """
    cilium_values = _read_cilium_values()

    # ConfigMap containing Cilium values
    cilium_values_manifest = {
        "name": "cilium-values",
        "contents": f"""---
apiVersion: v1
kind: ConfigMap
metadata:
  name: cilium-values
  namespace: kube-system
data:
  values.yaml: |
{_indent(cilium_values, 4)}
""",
    }

    # Job that installs Cilium using the values ConfigMap
    cilium_install_manifest = {
        "name": "cilium-bootstrap",
        "contents": f"""---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: cilium-install
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: cluster-admin
subjects:
- kind: ServiceAccount
  name: cilium-install
  namespace: kube-system
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: cilium-install
  namespace: kube-system
---
apiVersion: batch/v1
kind: Job
metadata:
  name: cilium-install
  namespace: kube-system
spec:
  backoffLimit: 10
  template:
    metadata:
      labels:
        app: cilium-install
    spec:
      restartPolicy: OnFailure
      tolerations:
        - operator: Exists
        - effect: NoSchedule
          operator: Exists
        - effect: NoExecute
          operator: Exists
        - effect: PreferNoSchedule
          operator: Exists
        - key: node-role.kubernetes.io/control-plane
          operator: Exists
          effect: NoSchedule
        - key: node-role.kubernetes.io/control-plane
          operator: Exists
          effect: NoExecute
        - key: node-role.kubernetes.io/control-plane
          operator: Exists
          effect: PreferNoSchedule
      affinity:
        nodeAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            nodeSelectorTerms:
              - matchExpressions:
                  - key: node-role.kubernetes.io/control-plane
                    operator: Exists
      serviceAccountName: cilium-install
      hostNetwork: true
      containers:
      - name: cilium-install
        image: quay.io/cilium/cilium-cli-ci:latest
        env:
        - name: KUBERNETES_SERVICE_HOST
          valueFrom:
            fieldRef:
              apiVersion: v1
              fieldPath: status.podIP
        - name: KUBERNETES_SERVICE_PORT
          value: "6443"
        volumeMounts:
          - name: values
            mountPath: /root/app/values.yaml
            subPath: values.yaml
        command:
          - cilium
          - install
          - --version=v{cilium_version}
          - --values
          - /root/app/values.yaml
      volumes:
        - name: values
          configMap:
            name: cilium-values
""",
    }

    return [cilium_values_manifest, cilium_install_manifest]


def _indent(text: str, spaces: int) -> str:
    """Indent each line of text by the specified number of spaces."""
    indent_str = " " * spaces
    return "\n".join(
        indent_str + line if line.strip() else line for line in text.split("\n")
    )


def create_talos_secrets(name: str, talos_version: str = None):
    # Secrets should remain stable across updates - do NOT include version in name
    # The talos_version parameter is used for the actual secret generation but not the resource name
    return talos.machine.Secrets(f"{name}-secrets", talos_version=talos_version)


def apply_talos_config(
    name: str,
    secrets: talos.machine.Secrets,
    cluster_name: str,
    cluster_endpoint: str,
    node_ip: str,
    role: str = "controlplane",
    install_disk: str = "/dev/sda",
    install_image: str = None,
    hostname: str = None,
    vm: pulumi.Resource = None,
    gateway: str = "192.168.1.1",
    nameservers: list = None,
    use_cilium: bool = False,
    cilium_version: str = "1.16.0",
    kubernetes_version: str = None,
    enable_gpu: bool = False,
    bootstrap: bool = False,
    node_labels: dict = None,
    node_taints: list = None,
    node_type: str = "proxmox",
    config_dependencies: list = None,
    grow_system_disk: bool = True,
):
    nameservers = nameservers or ["192.168.1.1"]
    config_dependencies = config_dependencies or []

    # Build machine config patch
    machine_patch = {
        "machine": {
            "install": {
                "disk": install_disk,
                "wipe": True,  # Force wipe disk on installation
            },
            "network": {
                "hostname": hostname or name,
                "nameservers": nameservers,
                "interfaces": [
                    {
                        "deviceSelector": {"busPath": "0*"},
                        "addresses": [f"{node_ip}/24"],
                        "routes": [{"network": "0.0.0.0/0", "gateway": gateway}],
                    }
                ],
            },
            # Enable kubelet certificate rotation for metrics-server
            # https://docs.siderolabs.com/kubernetes-guides/monitoring-and-observability/deploy-metrics-server
            "kubelet": {
                "extraArgs": {
                    "rotate-server-certificates": "true",
                },
            },
        }
    }

    # Add NVIDIA GPU kernel modules and runtime configuration if GPU is enabled
    if enable_gpu:
        # Add GPU node labels
        machine_patch["machine"].setdefault("nodeLabels", {})
        machine_patch["machine"]["nodeLabels"].update(
            {
                "nvidia.com/gpu.present": "true",
                "nvidia.com/mps.capable": "true",
                "feature.node.kubernetes.io/pci-10de.present": "true",
            }
        )

        machine_patch["machine"]["kernel"] = {
            "modules": [
                {"name": "nvidia"},
                {"name": "nvidia_uvm"},
                {"name": "nvidia_drm"},
                {"name": "nvidia_modeset"},
            ]
        }
        machine_patch["machine"]["sysctls"] = {"net.core.bpf_jit_harden": "1"}
        # Configure containerd runtime for NVIDIA
        machine_patch["machine"]["files"] = [
            {
                "content": '[plugins]\n  [plugins."io.containerd.grpc.v1.cri"]\n    enable_unprivileged_ports = true\n    enable_unprivileged_icmp = true\n  [plugins."io.containerd.grpc.v1.cri".containerd]\n    [plugins."io.containerd.grpc.v1.cri".containerd.runtimes]\n      [plugins."io.containerd.grpc.v1.cri".containerd.runtimes.nvidia]\n        privileged_without_host_devices = false\n        runtime_engine = ""\n        runtime_root = ""\n        runtime_type = "io.containerd.runc.v2"\n        [plugins."io.containerd.grpc.v1.cri".containerd.runtimes.nvidia.options]\n          BinaryName = "/usr/local/bin/nvidia-container-runtime"\n',
                "permissions": 0o644,
                "path": "/etc/cri/conf.d/20-customization.part",
                "op": "create",
            }
        ]

    # For control plane nodes with Cilium: disable default CNI, kube-proxy, and inject inline manifests
    if use_cilium and role == "controlplane":
        machine_patch["cluster"] = {
            "network": {"cni": {"name": "none"}},
            "proxy": {"disabled": True},
            "inlineManifests": _get_cilium_inline_manifests(cilium_version),
            # Install kubelet cert approver and metrics-server during bootstrap
            "extraManifests": [
                "https://raw.githubusercontent.com/alex1989hu/kubelet-serving-cert-approver/main/deploy/standalone-install.yaml",
                "https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml",
            ],
        }
    elif role == "controlplane":
        machine_patch["cluster"] = {
            "extraManifests": [
                "https://raw.githubusercontent.com/alex1989hu/kubelet-serving-cert-approver/main/deploy/standalone-install.yaml",
                "https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml",
            ],
        }

    def _render_machine_patch(image: str = None) -> str:
        patch = copy.deepcopy(machine_patch)
        # Merge user-supplied node labels and taints into the rendered patch
        if node_labels:
            patch["machine"].setdefault("nodeLabels", {})
            patch["machine"]["nodeLabels"].update(node_labels)
        if node_taints:
            patch["machine"]["nodeTaints"] = node_taints
        if image:
            patch["machine"]["install"]["image"] = image
        return json.dumps(patch)
    
    def _render_model_volume_patch() -> str:
        return json.dumps({
            "apiVersion": "v1alpha1",
            "kind": "UserVolumeConfig",
            "name": "model-store",
            "provisioning": {
                "diskSelector": {
                    "match": 'disk.dev_path == "/dev/sdb"'
                },
                "minSize": "100GB"
            },
            "filesystem": {
                "type": "xfs"
            }
        })

    def _render_volume_patch() -> str:
        # VolumeConfig is a separate top-level document kind, not a field inside
        # MachineConfig. It must be passed as its own patch entry.
        return json.dumps({
            "apiVersion": "v1alpha1",
            "kind": "VolumeConfig",
            "name": "EPHEMERAL",
            "provisioning": {
                "grow": True,
            },
        })

    # Build list of patches - VolumeConfig is a separate document from MachineConfig
    if install_image is None:
        patches = [_render_machine_patch()]
    else:
        patches = [
            pulumi.Output.from_input(install_image).apply(
                lambda image: _render_machine_patch(image)
            )
        ]

    patches.append(_render_volume_patch())
    patches.append(_render_model_volume_patch())

    # Convert secrets output to the format expected by get_configuration_output
    machine_secrets_dict = secrets.machine_secrets.apply(
        lambda ms: {
            "certs": {
                "etcd": {"cert": ms.certs.etcd.cert, "key": ms.certs.etcd.key},
                "k8s": {"cert": ms.certs.k8s.cert, "key": ms.certs.k8s.key},
                "k8sAggregator": {
                    "cert": ms.certs.k8s_aggregator.cert,
                    "key": ms.certs.k8s_aggregator.key,
                },
                "k8sServiceaccount": {"key": ms.certs.k8s_serviceaccount.key},
                "os": {"cert": ms.certs.os.cert, "key": ms.certs.os.key},
            },
            "cluster": {"id": ms.cluster.id, "secret": ms.cluster.secret},
            "secrets": {
                "bootstrapToken": ms.secrets.bootstrap_token,
                "secretboxEncryptionSecret": ms.secrets.secretbox_encryption_secret,
            },
            "trustdinfo": {"token": ms.trustdinfo.token},
        }
    )

    machine_config = talos.machine.get_configuration_output(
        cluster_name=cluster_name,
        machine_type=role,
        cluster_endpoint=cluster_endpoint,
        machine_secrets=machine_secrets_dict,
        config_patches=patches,
        kubernetes_version=kubernetes_version,
    )

    pulumi.log.info(f"Applying Talos configuration to {name} ({role}) at {node_ip}")

    # Build dependency list: VM (if exists) + any previous node configs
    depends_on_resources = (config_dependencies + [vm]) if vm else config_dependencies

    config_apply = talos.machine.ConfigurationApply(
        f"{name}-config-apply",
        client_configuration=secrets.client_configuration,
        machine_configuration_input=machine_config.machine_configuration,
        node=node_ip,
        endpoint=node_ip,
        apply_mode="auto",
        opts=pulumi.ResourceOptions(
            depends_on=depends_on_resources if depends_on_resources else None
        ),
    )

    result = {"config_apply": config_apply}

    if role == "controlplane" and bootstrap:
        pulumi.log.info(f"Bootstrapping Kubernetes cluster on {name}")
        result["bootstrap"] = talos.machine.Bootstrap(
            f"{name}-bootstrap",
            client_configuration=secrets.client_configuration,
            node=node_ip,
            endpoint=node_ip,
            opts=pulumi.ResourceOptions(depends_on=[config_apply]),
        )

        pulumi.log.info(f"Generating kubeconfig from {name}")
        result["kubeconfig"] = talos.cluster.Kubeconfig(
            f"{name}-kubeconfig",
            client_configuration=secrets.client_configuration,
            node=node_ip,
            endpoint=node_ip,
            opts=pulumi.ResourceOptions(depends_on=[result["bootstrap"]]),
        )

    return result