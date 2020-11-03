
import atexit
from socket import gaierror
from ipaddress import ip_address, ip_network, ip_interface
import re

import pprint

from pyVim.connect import SmartConnectNoSSL, Disconnect
from pyVmomi import vim

from module.netbox.object_classes import *
from module.common.misc import grab, do_error_exit, dump, get_string_or_none
from module.common.support import normalize_mac_address, format_ip
from module import plural
from module.common.logging import get_logger, DEBUG3

log = get_logger()

class VMWareHandler():

    dependend_netbox_objects = [
        NBTags,
        NBManufacturers,
        NBDeviceTypes,
        NBPlatforms,
        NBClusterTypes,
        NBClusterGroups,
        NBDeviceRoles,
        NBSites,
        NBClusters,
        NBDevices,
        NBVMs,
        NBVMInterfaces,
        NBInterfaces,
        NBIPAddresses,
    ]

    settings = {
        "host_fqdn": None,
        "port": 443,
        "username": None,
        "password": None,
        "cluster_exclude_filter": None,
        "cluster_include_filter": None,
        "host_exclude_filter": None,
        "host_include_filter": None,
        "vm_exclude_filter": None,
        "vm_include_filter": None,
        "netbox_host_device_role": "Server",
        "netbox_vm_device_role": "Server",
        "permitted_subnets": None,
        "collect_hardware_asset_tag": True,
        "cluster_site_relation": None,
        "dns_name_lookup": False,
        "custom_dns_servers": None
    }

    init_successfull = False
    inventory = None
    name = None
    source_tag = None
    source_type = "vmware"

    # internal vars
    session = None

    site_name = None

    networks = dict()
    dvs_mtu = dict()
    standalone_hosts = list()

    processed_host_names = list()
    processed_vm_names = list()
    processed_vm_uuid = list()

    parsing_vms_the_first_time = True

    def __init__(self, name=None, settings=None, inventory=None):

        if name is None:
            raise ValueError("Invalid value for attribute 'name': '{name}'.")

        self.inventory = inventory
        self.name = name

        self.parse_config_settings(settings)

        self.create_session()

        self.source_tag = f"Source: {name}"
        self.site_name = f"vCenter: {name}"

        if self.session is not None:
            self.init_successfull = True

    def parse_config_settings(self, config_settings):

        validation_failed = False
        for setting in ["host_fqdn", "port", "username", "password" ]:
            if config_settings.get(setting) is None:
                log.error(f"Config option '{setting}' in 'source/{self.name}' can't be empty/undefined")
                validation_failed = True

        # check permitted ip subnets
        if config_settings.get("permitted_subnets") is None:
            log.info(f"Config option 'permitted_subnets' in 'source/{self.name}' is undefined. No IP addresses will be populated to Netbox!")
        else:
            config_settings["permitted_subnets"] = [x.strip() for x in config_settings.get("permitted_subnets").split(",") if x.strip() != ""]

            permitted_subnets = list()
            for permitted_subnet in config_settings["permitted_subnets"]:
                try:
                    permitted_subnets.append(ip_network(permitted_subnet))
                except Exception as e:
                    log.error(f"Problem parsing permitted subnet: {e}")
                    validation_failed = True

            config_settings["permitted_subnets"] = permitted_subnets

        # check include and exclude filter expressions
        for setting in [x for x in config_settings.keys() if "filter" in x]:
            if config_settings.get(setting) is None or config_settings.get(setting).strip() == "":
                continue

            re_compiled = None
            try:
                re_compiled = re.compile(config_settings.get(setting))
            except Exception as e:
                log.error(f"Problem parsing regular expression for '{setting}': {e}")
                validation_failed = True

            config_settings[setting] = re_compiled

        if config_settings.get("cluster_site_relation") is not None:

            relation_data = dict()
            for relation in config_settings.get("cluster_site_relation").split(","):

                cluster_name = relation.split("=")[0].strip()
                site_name = relation.split("=")[1].strip()

                if len(cluster_name) == 0 or len(site_name) == 0:
                    log.error(f"Config option 'cluster_site_relation' malformed got '{cluster_name}' for cluster_name and '{site_name}' for site name.")
                    validation_failed = True

                relation_data[cluster_name] = site_name

            config_settings["cluster_site_relation"] = relation_data

        if config_settings.get("dns_name_lookup") is True and config_settings.get("custom_dns_servers") is not None:

            custom_dns_servers = [x.strip() for x in config_settings.get("custom_dns_servers").split(",") if x.strip() != ""]

            tested_custom_dns_servers = list()
            for custom_dns_server in custom_dns_servers:
                try:
                    tested_custom_dns_servers.append(str(ip_address(custom_dns_server)))
                except ValueError:
                    log.error(f"Config option 'custom_dns_servers' value '{custom_dns_server}' does not appear to be an IP address.")
                    validation_failed = True

            config_settings["custom_dns_servers"] = tested_custom_dns_servers

        if validation_failed is True:
            log.error("Config validation failed. Exit!")
            exit(1)

        for setting in self.settings.keys():
            setattr(self, setting, config_settings.get(setting))

    def create_session(self):

        if self.session is not None:
            return True

        log.debug(f"Starting vCenter connection to '{self.host_fqdn}'")

        try:
            instance = SmartConnectNoSSL(
                host=self.host_fqdn,
                port=self.port,
                user=self.username,
                pwd=self.password
            )
            atexit.register(Disconnect, instance)
            self.session = instance.RetrieveContent()

        except (gaierror, vim.fault.InvalidLogin, OSError) as e:

            log.error(
                f"Unable to connect to vCenter instance '{self.host_fqdn}' on port {self.port}. "
                f"Reason: {e}"
            )

            return False

        log.info(f"Successfully connected to vCenter '{self.host_fqdn}'")

        return True

    def apply(self):

        self.inizialize_basic_data()

        log.info(f"Query data from vCenter: '{self.host_fqdn}'")

        """
        Mapping of object type keywords to view types and handlers

        iterate over all VMs twice.

        To handle VMs with the same name in a cluster we first
        iterate over all VMs and look only at the active ones
        and sync these first.
        Then we iterate a second time to catch the rest.

        This has been implemented to support migration scenarios
        where you create the same machines with a different setup
        like a new version or something. This way NetBox will be
        updated primarily with the actual active VM data.
        """
        object_mapping = {
            "datacenter": {
                "view_type": vim.Datacenter,
                "view_handler": self.add_datacenter
            },
            "cluster": {
                "view_type": vim.ClusterComputeResource,
                "view_handler": self.add_cluster
            },
            "network": {
                "view_type": vim.Network,
                "view_handler": self.add_network
            },
            "host": {
                "view_type": vim.HostSystem,
                "view_handler": self.add_host
            },
            "virtual machine": {
                "view_type": vim.VirtualMachine,
                "view_handler": self.add_virtual_machine
            },
            "offline virtual machine": {
                "view_type": vim.VirtualMachine,
                "view_handler": self.add_virtual_machine
            }
        }

        for view_name, view_details in object_mapping.items():

            if self.session is None:
                log.info("No existing vCenter session found.")
                self.create_session()

            view_data = {
                "container": self.session.rootFolder,
                "type": [view_details.get("view_type")],
                "recursive": True
            }

            try:
                container_view = self.session.viewManager.CreateContainerView(**view_data)
            except Exception as e:
                log.error(f"Problem creating vCenter view for '{view_name}s': {e}")
                continue

            view_objects = grab(container_view, "view")

            if view_objects is None:
                log.error(f"Creating vCenter view for '{view_name}s' failed!")
                continue

            if view_name != "offline virtual machine":
                log.debug("vCenter returned '%d' %s%s" % (len(view_objects), view_name, plural(len(view_objects))))
            else:
                self.parsing_vms_the_first_time = False
                log.debug("Iterating over all virtual machines a second time ")

            for obj in view_objects:

                if log.level == DEBUG3:
                    try:
                        dump(obj)
                    except Exception as e:
                        log.error(e)

                view_details.get("view_handler")(obj)

            container_view.Destroy()

    @staticmethod
    def passes_filter(name, include_filter, exclude_filter):

        # first includes
        if include_filter is not None and not include_filter.match(name):
            log.debug(f"Cluster '{name}' did not match include filter '{include_filter.pattern}'. Skipping")
            return False

        # second excludes
        if exclude_filter is not None and exclude_filter.match(name):
            log.debug(f"Cluster '{name}' matched exclude filter '{exclude_filter.pattern}'. Skipping")
            return False

        return True

    def add_datacenter(self, obj):

        name = get_string_or_none(grab(obj, "name"))

        if name is None:
            return

        log.debug2(f"Parsing vCenter datacenter: {name}")

        self.inventory.add_update_object(NBClusterGroups, data = { "name": name }, source=self)

    def add_cluster(self, obj):

        name = get_string_or_none(grab(obj, "name"))
        group = get_string_or_none(grab(obj, "parent.parent.name"))

        if name is None or group is None:
            return

        log.debug2(f"Parsing vCenter cluster: {name}")

        if self.passes_filter(name, self.cluster_include_filter, self.cluster_exclude_filter) is False:
            return

        # set default site name
        site_name = self.site_name

        # check if site was provided in config
        site_realtion = getattr(self, "cluster_site_relation", None)
        if site_realtion is not None and site_realtion.get(name) is not None:
            site_name = site_realtion.get(name)

        data = {
            "name": name,
            "type": { "name": "VMware ESXi" },
            "group": { "name": group },
            "site": { "name": site_name}
        }

        self.inventory.add_update_object(NBClusters, data = data, source=self)

    def add_network(self, obj):

        key = get_string_or_none(grab(obj, "key"))
        name = get_string_or_none(grab(obj, "name"))
        vlan_id = grab(obj, "config.defaultPortConfig.vlan.vlanId")

        if key is None or name is None:
            return

        log.debug2(f"Parsing vCenter network: {name}")

        self.networks[key] = {
            "name": name,
            "vlan_id": vlan_id
        }

    def add_host(self, obj):

        # ToDo:
        # * find Host based on device mac addresses

        name = get_string_or_none(grab(obj, "name"))

        # parse data
        log.debug2(f"Parsing vCenter host: {name}")

        if name in self.processed_host_names:
            log.warning(f"Host '{name}' already parsed. Make sure to use unique host names. Skipping")
            return

        self.processed_host_names.append(name)

        # filter hosts
        if self.passes_filter(name, self.host_include_filter, self.host_exclude_filter) is False:
            return

        manufacturer =  get_string_or_none(grab(obj, "summary.hardware.vendor"))
        model =  get_string_or_none(grab(obj, "summary.hardware.model"))
        product_name = get_string_or_none(grab(obj, "summary.config.product.name"))
        product_version =  get_string_or_none(grab(obj, "summary.config.product.version"))
        platform = f"{product_name} {product_version}"


        status = "offline"
        if get_string_or_none(grab(obj, "summary.runtime.connectionState")) == "connected":
            status = "active"

        # prepare identifiers
        identifiers = grab(obj, "summary.hardware.otherIdentifyingInfo")
        identifier_dict = dict()
        if identifiers is not None:
            for item in identifiers:
                value = grab(item, "identifierValue", fallback="")
                if len(str(value).strip()) > 0:
                    identifier_dict[grab(item, "identifierType.key")] = str(value).strip()

        # try to find serial
        serial = None

        for serial_num_key in [ "EnclosureSerialNumberTag", "SerialNumberTag", "ServiceTag"]:
            if serial_num_key in identifier_dict.keys():
                serial = get_string_or_none(identifier_dict.get(serial_num_key))
                break

        # add asset tag if desired and present
        asset_tag = None

        if bool(self.collect_hardware_asset_tag) is True and "AssetTag" in identifier_dict.keys():

            banned_tags = [ "Default string", "NA", "N/A", "None", "Null", "oem", "o.e.m",
                            "to be filled by o.e.m.", "Unknown" ]

            this_asset_tag = identifier_dict.get("AssetTag")

            if this_asset_tag.lower() not in [x.lower() for x in banned_tags]:
                asset_tag = this_asset_tag

        # manage site and cluster
        cluster = get_string_or_none(grab(obj, "parent.name"))

        # set default site name
        site_name = self.site_name

        # check if site was provided in config
        site_realtion = getattr(self, "cluster_site_relation", None)
        if site_realtion is not None and site_realtion.get(cluster) is not None:
            site_name = site_realtion.get(cluster)

        # handle standalone hosts
        if cluster == name:
            # Store the host so that we can check VMs against it
            self.standalone_hosts.append(cluster)
            cluster = "Standalone ESXi Host"

        data={
            "name": name,
            "device_role": {"name": self.netbox_host_device_role},
            "device_type": {
                "model": model,
                "manufacturer": {
                    "name": manufacturer
                }
            },
            "site": {"name": site_name},
            "cluster": {"name": cluster},
            "status": status
        }

        if serial is not None:
            data["serial"]: serial
        if asset_tag is not None:
            data["asset_tag"]: asset_tag
        if platform is not None:
            data["platform"]: {"name": platform}

        host_object = self.inventory.add_update_object(NBDevices, data=data, source=self)

        host_vswitches = dict()
        for vswitch in grab(obj, "config.network.vswitch", fallback=list()):

            vswitch_name = grab(vswitch, "name")

            vswitch_pnics = [str(x) for x in grab(vswitch, "pnic", fallback=list())]

            if vswitch_name is not None:

                log.debug2(f"Found vSwitch {vswitch_name}")

                host_vswitches[vswitch_name] = {
                    "mtu": grab(vswitch, "mtu"),
                    "pnics": vswitch_pnics
                }

        host_pswitches = dict()
        for pswitch in grab(obj, "config.network.proxySwitch", fallback=list()):

            pswitch_uuid = grab(pswitch, "dvsUuid")
            pswitch_name = grab(pswitch, "dvsName")
            pswitch_pnics = [str(x) for x in grab(pswitch, "pnic", fallback=list())]

            if pswitch_uuid is not None:

                log.debug2(f"Found proxySwitch {pswitch_name}")

                host_pswitches[pswitch_uuid] = {
                    "name": pswitch_name,
                    "mtu": grab(pswitch, "mtu"),
                    "pnics": pswitch_pnics
                }

                self.dvs_mtu[pswitch_uuid] = grab(pswitch, "mtu")

        host_portgroups = dict()
        for pgroup in grab(obj, "config.network.portgroup", fallback=list()):

            pgroup_name = grab(pgroup, "spec.name")

            if pgroup_name is not None:

                log.debug2(f"Found portGroup {pgroup_name}")

                host_portgroups[pgroup_name] = {
                    "vlan_id": grab(pgroup, "spec.vlanId"),
                    "vswitch": grab(pgroup, "spec.vswitchName")
                }

        for pnic in grab(obj, "config.network.pnic", fallback=list()):

            pnic_name = grab(pnic, "device")
            pnic_key = grab(pnic, "key")

            log.debug2("Parsing {}: {}".format(grab(pnic, "_wsdlName"), pnic_name))

            pnic_mtu = None

            pnic_link_speed = grab(pnic, "linkSpeed.speedMb")
            if pnic_link_speed is None:
                pnic_link_speed = grab(pnic, "spec.linkSpeed.speedMb")
            if pnic_link_speed is None:
                pnic_link_speed = grab(pnic, "validLinkSpecification.0.speedMb")

            # determine link speed text
            pnic_description = ""
            if pnic_link_speed is not None:
                if pnic_link_speed >= 1000:
                    pnic_description = "%iGb/s " % int(pnic_link_speed / 1000)
                else:
                    pnic_description = f"{pnic_link_speed}Mb/s "

            pnic_description = f"{pnic_description} pNIC"

            # check virtual switches for interface data
            for vs_name, vs_data in host_vswitches.items():

                if pnic_key in vs_data.get("pnics", list()):
                    pnic_description = f"{pnic_description} ({vs_name})"
                    pnic_mtu = vs_data.get("mtu")

            # check proxy switches for interface data
            for ps_uuid, ps_data in host_pswitches.items():

                if pnic_key in ps_data.get("pnics", list()):
                    ps_name = ps_data.get("name")
                    pnic_description = f"{pnic_description} ({ps_name})"
                    pnic_mtu = ps_data.get("mtu")

            pnic_speed_type_mapping = {
                100: "100base-tx",
                1000: "1000base-t",
                10000: "10gbase-t",
                25000: "25gbase-x-sfp28",
                40000: "40gbase-x-qsfpp"
            }

            pnic_data = {
                "name": pnic_name,
                "device": host_object,
                "mac_address": normalize_mac_address(grab(pnic, "mac")),
                "enabled": bool(grab(pnic, "linkSpeed")),
                "description": pnic_description,
                "type": pnic_speed_type_mapping.get(pnic_link_speed, "other")
            }

            if pnic_mtu is not None:
                pnic_data["mtu"] = pnic_mtu

            self.inventory.add_update_object(NBInterfaces, data=pnic_data, source=self)


        for vnic in grab(obj, "config.network.vnic", fallback=list()):

            vnic_name = grab(vnic, "device")

            log.debug2("Parsing {}: {}".format(grab(vnic, "_wsdlName"), vnic_name))

            vnic_portgroup = grab(vnic, "portgroup")
            vnic_portgroup_data = host_portgroups.get(vnic_portgroup)

            vnic_description = vnic_portgroup
            if vnic_portgroup_data is not None:
                vnic_vlan_id = vnic_portgroup_data.get("vlan_id")
                vnic_vswitch = vnic_portgroup_data.get("vswitch")
                vnic_description = f"{vnic_description} ({vnic_vswitch}, vlan ID: {vnic_vlan_id})"

            vnic_data = {
                "name": vnic_name,
                "device": host_object,
                "mac_address": normalize_mac_address(grab(vnic, "spec.mac")),
                "mtu": grab(vnic, "spec.mtu"),
                "description": vnic_description,
                "type": "virtual"
            }

            vnic_object = self.inventory.add_update_object(NBInterfaces, data=vnic_data, source=self)

            # check if interface has the default route
            if grab(vnic, "spec.ipRouteSpec") is not None:
                vnic_object.is_primary = True

            vnic_ips = list()

            vnic_ips.append("{}/{}".format(grab(vnic, "spec.ip.ipAddress"), grab(vnic, "spec.ip.subnetMask")))

            for ipv6_a in grab(vnic, "spec.ip.ipV6Config.ipV6Address", fallback=list()):

                vnic_ips.append("{}/{}".format(grab(ipv6_a, "ipAddress"), grab(ipv6_a, "prefixLength")))

            # add all interface IPs
            for vnic_ip in vnic_ips:

                if format_ip(vnic_ip) is None:
                    logging.error(f"IP address '{vnic_ip}' for {vnic_object.get_display_name()} invalid!")
                    continue

                ip_permitted = False

                ip_address_object = ip_interface(vnic_ip).ip
                for permitted_subnet in self.permitted_subnets:
                    if ip_address_object in permitted_subnet:
                        ip_permitted = True
                        break

                if ip_permitted is False:
                    log.debug(f"IP address {vnic_ip} not part of any permitted subnet. Skipping.")
                    continue

                vnic_ip_data = {
                    "address": format_ip(vnic_ip),
                    "assigned_object_id": vnic_object,
                }

                self.inventory.add_update_object(NBIPAddresses, data=vnic_ip_data, source=self)

    def add_virtual_machine(self, obj):

        name = get_string_or_none(grab(obj, "name"))

        # get VM UUID
        vm_uuid = grab(obj, "config.uuid")

        if vm_uuid is None or vm_uuid in self.processed_vm_uuid:
            return

        log.debug2(f"Parsing vCenter VM: {name}")

        # get VM power state
        status = "active" if get_string_or_none(grab(obj, "runtime.powerState")) == "poweredOn" else "offline"

        # ignore offline VMs during first run
        if self.parsing_vms_the_first_time == True and status == "offline":
            log.debug2(f"Ignoring {status} VM '{name}' on first run")
            return

        # filter VMs
        if self.passes_filter(name, self.vm_include_filter, self.vm_exclude_filter) is False:
            return

        cluster = get_string_or_none(grab(obj, "runtime.host.parent.name"))
        if cluster is None:
            log.error(f"Requesting cluster for Virtual Machine '{name}' failed. Skipping.")
            return

        if name in self.processed_vm_names:
            log.warning(f"Virtual machine '{name}' already parsed. Make sure to use unique VM names. Skipping")
            return

        # add to processed VMs
        self.processed_vm_uuid.append(vm_uuid)
        self.processed_vm_names.append(name)


        if cluster in self.standalone_hosts:
            cluster = "Standalone ESXi Host"

        platform = grab(obj, "config.guestFullName")
        platform = get_string_or_none(grab(obj, "guest.guestFullName", fallback=platform))


        hardware_devices = grab(obj, "config.hardware.device", fallback=list())

        disk = int(sum([ getattr(comp, "capacityInKB", 0) for comp in hardware_devices
                       if isinstance(comp, vim.vm.device.VirtualDisk)
                            ]) / 1024 / 1024)

        annotation = get_string_or_none(grab(obj, "config.annotation"))

        vm_data = {
            "name": name,
            "cluster": {"name": cluster},
            "role": {"name": self.settings.get("netbox_vm_device_role")},
            "status": status,
            "memory": grab(obj, "config.hardware.memoryMB"),
            "vcpus": grab(obj, "config.hardware.numCPU"),
            "disk": disk
        }

        if platform is not None:
            vm_data["platform"] = {"name": platform}

        if annotation is not None:
            vm_data["comments"] = annotation


        device_nic_data = list()
        device_ip_addresses = dict()

        default_route_4_int_mac = None
        default_route_6_int_mac = None

        for route in grab(obj, "guest.ipStack.0.ipRouteConfig.ipRoute", fallback=list()):

            # we found a default route
            if grab(route, "prefixLength") == 0:

                ip_a = None
                try:
                    ip_a = ip_address(grab(route, "network"))
                except ValueError:
                    pass

                if ip_a is None:
                    continue

                gateway_device = grab(route, "gateway.device", fallback="")

                nics = grab(obj, "guest.net", fallback=list())

                print(grab(nics, f"{gateway_device}"))
                gateway_device_mac = normalize_mac_address(grab(nics, f"{gateway_device}.macAddress"))

                if ip_a.version == 4 and gateway_device_mac is not None:
                    default_route_4_int_mac = gateway_device_mac
                elif ip_a.version == 6 and gateway_device_mac is not None:
                    default_route_6_int_mac = gateway_device_mac

        #print(default_route_4_int_mac)
        #print(default_route_6_int_mac)

        # get vm interfaces
        for vm_device in hardware_devices:

            int_mac = normalize_mac_address(grab(vm_device, "macAddress"))

            # not a network interface
            if int_mac is None:
                continue

            device_class = grab(vm_device, "_wsdlName")

            log.debug2(f"Parsing device {device_class}: {int_mac}")

            int_portgroup_data = self.networks.get(grab(vm_device, "backing.port.portgroupKey"))

            int_network_name = grab(int_portgroup_data, "name")
            int_network_vlan_id = grab(int_portgroup_data, "vlan_id")


            int_dvswitch_uuid = grab(vm_device, "backing.port.switchUuid")

            int_mtu = None
            if int_dvswitch_uuid is not None:
                int_mtu = self.dvs_mtu.get(int_dvswitch_uuid)

            int_connected = grab(vm_device, "connectable.connected")
            int_label = grab(vm_device, "deviceInfo.label", fallback="")

            int_name = "vNIC {}".format(int_label.split(" ")[-1])

            int_ip_addresses = list()

            for guest_nic in grab(obj, "guest.net", fallback=list()):

                if int_mac != normalize_mac_address(grab(guest_nic, "macAddress")):
                    continue

                int_connected = grab(guest_nic, "connected", fallback=int_connected)

                # grab all valid interface ip addresses
                for int_ip in grab(guest_nic, "ipConfig.ipAddress", fallback=list()):

                    int_ip_address = f"{int_ip.ipAddress}/{int_ip.prefixLength}"

                    if format_ip(int_ip_address) is None:
                        logging.error(f"IP address '{int_ip_address}' for {vm_nic_object.get_display_name()} invalid!")
                        continue

                    ip_permitted = False

                    ip_address_object = ip_address(int_ip_address.split("/")[0])
                    for permitted_subnet in self.permitted_subnets:
                        if ip_address_object in permitted_subnet:
                            ip_permitted = True
                            break

                    if ip_permitted is False:
                        log.debug(f"IP address {int_ip_address} not part of any permitted subnet. Skipping.")
                        continue

                    int_ip_addresses.append(int_ip_address)


            int_full_name = int_name
            if int_network_name is not None:
                int_full_name = f"{int_full_name} ({int_network_name})"

            int_description = f"{int_label} ({device_class})"
            if int_network_vlan_id is not None:
                int_description = f"{int_description} (vlan ID: {int_network_vlan_id})"

            vm_nic_data = {
                "name": int_full_name,
                "mac_address": int_mac,
                "description": int_description,
                "enabled": int_connected,
            }

            if int_mtu is not None:
                vm_nic_data["mtu"] = int_mtu

            device_nic_data.append(vm_nic_data)
            device_ip_addresses[int_full_name] = int_ip_addresses


        #####
        # add reported guest IP if
        #   * VM has one interface and
        #   * this one interface hast no IPv4
        """
        if len(device_nic_data) == 1 and guest_ip is not None:

            ip_a = None
            try:
                ip_a = ip_address(guest_ip)
            except ValueError:
                pass

            if ip_a is not None:

                prefix_len = 32 if ip_a.version == 4 else 128

                nic_name = grab(device_nic_data, "0.name")

                add_this_ip = True
                for nic_ip in device_ip_addresses[nic_name]:
                    if nic_ip.split("/")[0] == guest_ip:
                        add_this_ip = False
                        break

                if add_this_ip is True:
                    device_ip_addresses[int_full_name].append(f"{guest_ip}/{prefix_len}")
        """

        # now we collected all the device data
        # lets try to find a matching object on following order
        #   * try to match name
        #       * if name matches try to find the cluster matches
        #   * try to check if any interface MAC matches to an existing object
        #       * if mac matches try to match IP
        #   * try if any interface IP matches the primary IP of that device
        #       * if primary IP matches see if cluster matches
        #
        # if nothing of the above worked then it's probably a new VM

        vm_object = None
        vm_object_candidates = list()

        # check existing VMs for matches
        for vm in self.inventory.get_all_items(NBVMs):

            if vm.source is not None:
                continue

            if grab(vm, "data.name") != vm_data.get("name"):
                continue

            # name and cluster match exactly, we most likely found the correct VM
            if grab(vm, "data.cluster.data.name") == vm_data.get("cluster.name"):
                vm_object = vm
                break

            vm_object_candidates.append(vm)

        if vm_object is None:

            for vm_interface in self.inventory.get_all_items(NBVMInterfaces):
                # check all interfaces against all MACs and try to find candidates
                pass

        if vm_object is None:
            vm_object = self.inventory.add_update_object(NBVMs, data=vm_data, source=self)
        else:
            vm_object.update(data=vm_data, source=self)

        for vm_nic_data in device_nic_data:

            vm_nic_data["virtual_machine"] = vm_object

            # we are trying multiple strategies to find the correct interface
            # mac address will change if interface is moved to a different network
            # * interface name i.e. vNIC 1 and ignore network
            # * mac address

            vm_nic_object = None
            for interface in self.inventory.get_all_items(NBVMInterfaces):

                if grab(interface, "data.virtual_machine") != vm_object:
                    continue

                # found device based on name
                if grab(interface, "data.name").startswith(vm_nic_data.get("name").split("(")[0].strip()):
                    vm_nic_object = interface
                    break

                # found device based on mac address
                if grab(interface, "data.mac_address") == vm_nic_data.get("mac_address"):
                    vm_nic_object = interface
                    break

            if vm_nic_object is not None:
                vm_nic_object.update(data=vm_nic_data, source=self)
            else:
                vm_nic_object = self.inventory.add_update_object(NBVMInterfaces, data=vm_nic_data, source=self)

            for int_ip_address in device_ip_addresses.get(vm_nic_data.get("name"), list()):

                # apply ip filter
                vm_nic_ip_data = {
                    "address": format_ip(int_ip_address),
                    "assigned_object_id": vm_nic_object,
                }

                self.inventory.add_update_object(NBIPAddresses, data=vm_nic_ip_data, source=self)

    def inizialize_basic_data(self):

        # add source identification tag
        self.inventory.add_update_object(NBTags, data={
            "name": self.source_tag,
            "description": f"Marks sources synced from vCenter {self.name} "
                           f"({self.host_fqdn}) to this NetBox Instance."
        })

        self.inventory.add_update_object(NBSites, data={
            "name": self.site_name,
            "comments": f"A default virtual site created to house objects "
                        "that have been synced from this vCenter instance."
        })

        self.inventory.add_update_object(NBClusters, data={
            "name": "Standalone ESXi Host",
            "type": {"name": "VMware ESXi"},
            "comments": "A default cluster created to house standalone "
                        "ESXi hosts and VMs that have been synced from "
                        "vCenter."
        })

        self.inventory.add_update_object(NBDeviceRoles, data={
            "name": "Server",
            "color": "9e9e9e",
            "vm_role": True
        })


# EOF

