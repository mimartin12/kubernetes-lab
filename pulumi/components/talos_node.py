"""TalosNode Pulumi Component"""

import pulumi
import pulumi_proxmoxve as proxmoxve
import pulumiverse_talos as talos
from talos_config import apply_talos_config


class TalosNodeArgs:
    """Arguments for TalosNode component"""

    def __init__(
        self,
        name: str,
        ip: str,
        role: str,
        gateway: str,
        talos_secrets: talos.machine.Secrets,
        cluster_name: str,
        cluster_endpoint: str,
        talos_installer_image: pulumi.Output[str],
        talos_iso_file_id: pulumi.Output[str],
        node_type: str = "proxmox",
        cpu: int = 2,
        memory: int = 2048,
        install_disk: str = "/dev/sda",
        disks: list = None,
        machine: str = "q35",
        pcie_devices: list = None,
        node_labels: dict = None,
        node_taints: list = None,
        proxmox_provider: proxmoxve.Provider = None,
        use_cilium: bool = False,
        cilium_version: str = "1.16.0",
        kubernetes_version: str = None,
        is_bootstrap: bool = False,
        config_dependencies: list = None,
    ):
        self.name = name
        self.ip = ip
        self.role = role
        self.gateway = gateway
        self.talos_secrets = talos_secrets
        self.cluster_name = cluster_name
        self.cluster_endpoint = cluster_endpoint
        self.talos_installer_image = talos_installer_image
        self.talos_iso_file_id = talos_iso_file_id
        self.node_type = node_type
        self.cpu = cpu
        self.memory = memory
        self.machine = machine
        self.disks = disks or []
        self.pcie_devices = pcie_devices or []
        self.node_labels = node_labels or {}
        self.node_taints = node_taints or []
        self.install_disk = install_disk
        self.proxmox_provider = proxmox_provider
        self.use_cilium = use_cilium
        self.cilium_version = cilium_version
        self.kubernetes_version = kubernetes_version
        self.is_bootstrap = is_bootstrap
        self.config_dependencies = config_dependencies or []


class TalosNode(pulumi.ComponentResource):
    """
    A Pulumi ComponentResource that creates and configures a Talos node:
    - Optionally creates a Proxmox VM
    - Applies Talos machine configuration
    - Bootstraps control plane (if bootstrap node)
    - Generates kubeconfig (if bootstrap node)
    """

    def __init__(
        self,
        name: str,
        args: TalosNodeArgs,
        opts: pulumi.ResourceOptions = None,
    ):
        super().__init__("custom:talos:Node", name, {}, opts)

        self.ip = args.ip
        self.role = args.role
        self.vm = None

        # Create VM if not external node
        if args.node_type == "external":
            pulumi.log.info(
                f"Skipping VM creation for {args.name} (external node at {args.ip})"
            )
        else:
            self.vm = self._create_vm(args)

        # Apply Talos configuration
        config_result = apply_talos_config(
            name=args.name,
            secrets=args.talos_secrets,
            cluster_name=args.cluster_name,
            cluster_endpoint=args.cluster_endpoint,
            node_ip=args.ip,
            role=args.role,
            vm=self.vm,
            gateway=args.gateway,
            node_type=args.node_type,
            use_cilium=args.use_cilium,
            cilium_version=args.cilium_version,
            kubernetes_version=args.kubernetes_version,
            install_disk=args.install_disk,
            install_image=args.talos_installer_image,
            enable_gpu=len(args.pcie_devices) > 0,
            node_labels=args.node_labels,
            node_taints=args.node_taints,
            bootstrap=args.is_bootstrap,
            config_dependencies=args.config_dependencies,
        )

        self.config_apply = config_result["config_apply"]
        self.bootstrap = config_result.get("bootstrap")
        self.kubeconfig = config_result.get("kubeconfig")

        # Prepare outputs
        outputs = {
            "ip": self.ip,
            "role": self.role,
            "config_apply": self.config_apply,
        }

        if self.vm:
            outputs["vm_id"] = self.vm.id

        if self.bootstrap:
            outputs["bootstrap"] = self.bootstrap

        if self.kubeconfig:
            outputs["kubeconfig"] = self.kubeconfig.kubeconfig_raw

        self.register_outputs(outputs)

    def _create_vm(self, args: TalosNodeArgs) -> proxmoxve.vm.VirtualMachine:
        """Create a Proxmox VM for the Talos node"""
        # Prepare PCIe devices if provided
        hostpcis = None
        if args.pcie_devices:
            hostpcis = [
                proxmoxve.vm.VirtualMachineHostpciArgs(
                    device=f"hostpci{idx}",
                    mapping=device_mapping,
                    pcie=True,
                    rombar=True,  # Enable ROM BAR for GPU initialization
                    xvga=False,  # Don't use as primary VGA to avoid conflicts
                )
                for idx, device_mapping in enumerate(args.pcie_devices)
            ]

        return proxmoxve.vm.VirtualMachine(
            f"{args.name}-vm",
            node_name="pve01",
            agent=proxmoxve.vm.VirtualMachineAgentArgs(
                enabled=True,
                type="virtio",
            ),
            bios="ovmf",
            efi_disk=proxmoxve.vm.VirtualMachineEfiDiskArgs(
                datastore_id="local-lvm",
                file_format="raw",
                type="4m",
            ),
            machine=args.machine,
            cpu=proxmoxve.vm.VirtualMachineCpuArgs(
                cores=args.cpu,
                sockets=1,
                type="host",
            ),
            disks=[
                # proxmoxve.vm.VirtualMachineDiskArgs(
                #     interface="scsi0",
                #     size=20,
                #     datastore_id="local-lvm",
                #     file_format="raw",
                # ),
                proxmoxve.vm.VirtualMachineDiskArgs(
                    interface=f"scsi{disk_idx}",
                    size=int(disk.get("size", 20)),
                    datastore_id=disk.get("datastore_id", "local-lvm"),
                    file_format=disk.get("file_format", "raw"),
                ) for disk_idx, disk in enumerate(args.disks)
            ],
            memory=proxmoxve.vm.VirtualMachineMemoryArgs(dedicated=args.memory),
            network_devices=[
                proxmoxve.vm.VirtualMachineNetworkDeviceArgs(
                    model="virtio", bridge="vmbr0"
                )
            ],
            initialization=proxmoxve.vm.VirtualMachineInitializationArgs(
                datastore_id="local-lvm",
                type="nocloud",
                interface="ide0",
                ip_configs=[
                    proxmoxve.vm.VirtualMachineInitializationIpConfigArgs(
                        ipv4=proxmoxve.vm.VirtualMachineInitializationIpConfigIpv4Args(
                            address=f"{args.ip}/24",
                            gateway=args.gateway,
                        )
                    )
                ],
                dns=proxmoxve.vm.VirtualMachineInitializationDnsArgs(
                    servers=[args.gateway],
                ),
            ),
            cdrom=proxmoxve.vm.VirtualMachineCdromArgs(
                file_id=args.talos_iso_file_id,
                interface="ide2",
            ),
            boot_orders=["scsi0"],
            hostpcis=hostpcis,
            opts=pulumi.ResourceOptions(
                parent=self,
                provider=args.proxmox_provider,
            ),
        )
