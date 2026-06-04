#!/usr/bin/env python3
"""cleanup-vcenter-aci.py — cleanup APIC-created vCenter DVS.

The script performs the following actions:
- Connects to APIC via REST API and lists VMware VMM domains
- Lets the operator select the VMM domain / vCenter DVS to cleanup
- Connects to vCenter via pyVmomi
- Disconnects VM vNICs from all portgroups on the selected DVS
- Removes VMkernel adapters using the selected DVS
- Removes ESXi hosts from the selected DVS
- Removes the VMM domain association from all APIC EPGs
- Deletes the APIC VMware VMM domain
- Confirms that vCenter no longer has the DVS

Usage:
    uv run cleanup-vcenter-aci.py [--check] [--log-level LEVEL]

Flags:
    --check              Test connectivity to APIC and vCenter
                         Print a report without executing any cleanups
    --log-level LEVEL    Verbosity: DEBUG, INFO, WARNING, ERROR (default: INFO)

Required environment variables:
    ACI_HOST — APIC management IP or hostname
    ACI_USER — APIC login username
    ACI_PASS — APIC login password
    VCENTER_HOST — vCenter management IP or hostname
    VCENTER_USER — vCenter login username
    VCENTER_PASS — vCenter login password
"""

from __future__ import annotations

import argparse
import logging
import os
import ssl
import sys
import time
from dataclasses import dataclass
from urllib.parse import quote

import prettytable
import requests
import urllib3
from pyVim.connect import Disconnect, SmartConnect
from pyVmomi import vim, vmodl

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

API_TIMEOUT: int = 30
API_LOGIN_PATH: str = "/api/aaaLogin.json"
API_VMWARE_DOMAINS_PATH: str = "/api/node/class/vmmDomP.json"
API_EPG_DOMAIN_RELS_PATH: str = "/api/node/class/fvRsDomAtt.json"

VCENTER_TASK_TIMEOUT: int = 600
VCENTER_DVS_REMOVAL_TIMEOUT: int = 300
VCENTER_POLL_INTERVAL: float = 2.0

LOG_FORMAT: str = "%(asctime)s [%(levelname)-8s] %(message)s"
LOG_DATE_FORMAT: str = "%Y-%m-%dT%H:%M:%S"
LOG_LEVELS: list[str] = ["DEBUG", "INFO", "WARNING", "ERROR"]

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

# (system, target, reachable, status_message)
CheckResult = tuple[str, str, bool, str]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    """Runtime configuration loaded from environment variables.

    :param apic_host: APIC management IP or hostname.
    :param apic_user: APIC API username.
    :param apic_password: APIC API password.
    :param vcenter_host: vCenter management IP or hostname.
    :param vcenter_user: vCenter username.
    :param vcenter_password: vCenter password.
    """

    apic_host: str
    apic_user: str
    apic_password: str
    vcenter_host: str
    vcenter_user: str
    vcenter_password: str


@dataclass(frozen=True)
class VmmDomain:
    """APIC VMware VMM domain selected for cleanup.

    :param name: VMM domain name. In vCenter this should be the DVS name.
    :param dn: APIC distinguished name for the domain.
    """

    name: str
    dn: str


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    datefmt=LOG_DATE_FORMAT,
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

# APIC and lab vCenter deployments commonly use self-signed TLS certificates.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ---------------------------------------------------------------------------
# APIC REST API
# ---------------------------------------------------------------------------


def _apic_login(
    session: requests.Session,
    host: str,
    user: str,
    password: str,
) -> None:
    """Authenticate against the APIC REST API.

    :param session: Requests session used to persist the auth cookie.
    :param host: APIC management IP or hostname.
    :param user: Username.
    :param password: Password.
    :raises requests.HTTPError: On authentication failure.
    """
    url = f"https://{host}{API_LOGIN_PATH}"
    payload = {"aaaUser": {"attributes": {"name": user, "pwd": password}}}
    response = session.post(url, json=payload, verify=False, timeout=API_TIMEOUT)
    response.raise_for_status()
    logger.debug("APIC REST API login successful: %s", host)


def _apic_get(
    session: requests.Session,
    host: str,
    path: str,
    params: dict[str, str] | None = None,
) -> dict:
    """Run an authenticated APIC GET request and return the decoded payload."""
    url = f"https://{host}{path}"
    response = session.get(url, params=params, verify=False, timeout=API_TIMEOUT)
    response.raise_for_status()
    return response.json()


def _apic_delete_mo(session: requests.Session, host: str, dn: str) -> None:
    """Delete an APIC managed object by distinguished name."""
    quoted_dn = quote(dn, safe="/[]:-_.")
    url = f"https://{host}/api/node/mo/{quoted_dn}.json"
    response = session.delete(url, verify=False, timeout=API_TIMEOUT)
    response.raise_for_status()


def fetch_vmware_vmm_domains(host: str, user: str, password: str) -> list[VmmDomain]:
    """Fetch VMware VMM domains from APIC.

    :param host: APIC management IP or hostname.
    :param user: API username.
    :param password: API password.
    :returns: Sorted list of VMware VMM domains.
    """
    domains: list[VmmDomain] = []
    with requests.Session() as session:
        _apic_login(session, host, user, password)
        payload = _apic_get(session, host, API_VMWARE_DOMAINS_PATH)

    for item in payload.get("imdata", []):
        attrs = item.get("vmmDomP", {}).get("attributes", {})
        dn = attrs.get("dn", "")
        name = attrs.get("name", "")
        if dn.startswith("uni/vmmp-VMware/dom-") and name:
            domains.append(VmmDomain(name=name, dn=dn))

    domains.sort(key=lambda domain: domain.name.lower())
    logger.info("Fetched %d VMware VMM domains from APIC", len(domains))
    return domains


def fetch_epg_domain_attachments(
    session: requests.Session,
    host: str,
    domain_dn: str,
) -> list[str]:
    """Fetch EPG-to-domain relation DNs for a selected VMM domain."""
    params = {"query-target-filter": f'eq(fvRsDomAtt.tDn,"{domain_dn}")'}
    payload = _apic_get(session, host, API_EPG_DOMAIN_RELS_PATH, params=params)
    attachments: list[str] = []

    for item in payload.get("imdata", []):
        attrs = item.get("fvRsDomAtt", {}).get("attributes", {})
        dn = attrs.get("dn", "")
        if dn:
            attachments.append(dn)

    attachments.sort()
    return attachments


def remove_domain_from_epgs(
    session: requests.Session,
    host: str,
    domain: VmmDomain,
) -> int:
    """Remove the selected VMM domain association from all EPGs.

    :param session: Authenticated APIC session.
    :param host: APIC management IP or hostname.
    :param domain: Selected VMware VMM domain.
    :returns: Number of deleted EPG relation objects.
    """
    attachments = fetch_epg_domain_attachments(session, host, domain.dn)
    if not attachments:
        logger.info("No EPG associations found for VMM domain %s", domain.name)
        return 0

    for attachment_dn in attachments:
        logger.info("Removing EPG VMM domain association: %s", attachment_dn)
        _apic_delete_mo(session, host, attachment_dn)

    logger.info(
        "Removed %d EPG association(s) for VMM domain %s",
        len(attachments),
        domain.name,
    )
    return len(attachments)


def delete_vmm_domain(
    session: requests.Session,
    host: str,
    domain: VmmDomain,
) -> None:
    """Delete the selected VMware VMM domain from APIC."""
    logger.info("Deleting APIC VMware VMM domain: %s", domain.dn)
    _apic_delete_mo(session, host, domain.dn)


# ---------------------------------------------------------------------------
# vCenter helpers
# ---------------------------------------------------------------------------


def connect_vcenter(cfg: Config):
    """Connect to vCenter and return a pyVmomi service instance."""
    context = ssl._create_unverified_context()
    service_instance = SmartConnect(
        host=cfg.vcenter_host,
        user=cfg.vcenter_user,
        pwd=cfg.vcenter_password,
        sslContext=context,
    )
    logger.debug("vCenter login successful: %s", cfg.vcenter_host)
    return service_instance


def _get_objects(content, vim_type) -> list:
    """Return all inventory objects of a specific vCenter type."""
    view = content.viewManager.CreateContainerView(
        content.rootFolder,
        [vim_type],
        True,
    )
    try:
        return list(view.view)
    finally:
        view.Destroy()


def _dvs_uuid(dvs: vim.DistributedVirtualSwitch) -> str:
    """Return a stable DVS UUID from a distributed switch object."""
    return getattr(dvs, "uuid", "") or dvs.config.uuid


def find_dvs_by_name(
    service_instance, name: str
) -> vim.DistributedVirtualSwitch | None:
    """Find a vCenter distributed virtual switch by name."""
    content = service_instance.RetrieveContent()
    switches = _get_objects(content, vim.DistributedVirtualSwitch)
    matches = [dvs for dvs in switches if dvs.name == name]

    if not matches:
        return None
    if len(matches) > 1:
        logger.warning(
            "Found %d DVS objects named %s; using the first match",
            len(matches),
            name,
        )
    return matches[0]


def _dvs_portgroup_keys(dvs: vim.DistributedVirtualSwitch) -> set[str]:
    """Return the portgroup keys that belong to a DVS."""
    return {portgroup.key for portgroup in getattr(dvs, "portgroup", [])}


def _host_members(dvs: vim.DistributedVirtualSwitch) -> list[vim.HostSystem]:
    """Return ESXi hosts currently attached to a DVS."""
    hosts: list[vim.HostSystem] = []
    for member in getattr(dvs.config, "host", []):
        host = getattr(member.config, "host", None)
        if host is not None:
            hosts.append(host)
    return hosts


def _nic_uses_dvs(
    nic: vim.vm.device.VirtualEthernetCard,
    switch_uuid: str,
    portgroup_keys: set[str],
) -> bool:
    """Return True when a VM network adapter is backed by the selected DVS."""
    backing = getattr(nic, "backing", None)
    if not isinstance(
        backing,
        vim.vm.device.VirtualEthernetCard.DistributedVirtualPortBackingInfo,
    ):
        return False

    port = getattr(backing, "port", None)
    if port is None:
        return False

    return port.switchUuid == switch_uuid or port.portgroupKey in portgroup_keys


def _vm_name(vm: vim.VirtualMachine) -> str:
    """Return a printable VM name without raising on partial inventory objects."""
    return getattr(vm, "name", "<unknown-vm>")


def wait_for_task(
    task: vim.Task, action: str, timeout: int = VCENTER_TASK_TIMEOUT
) -> None:
    """Wait for a vCenter task to complete.

    :param task: pyVmomi task object.
    :param action: Human-readable action name used in errors.
    :param timeout: Maximum wait time in seconds.
    :raises TimeoutError: If the task does not finish in time.
    :raises RuntimeError: If vCenter reports task failure.
    """
    started_at = time.monotonic()

    while True:
        state = task.info.state
        if state == vim.TaskInfo.State.success:
            return
        if state == vim.TaskInfo.State.error:
            error = task.info.error
            message = error.msg if error is not None else "unknown vCenter error"
            raise RuntimeError(f"{action} failed: {message}")
        if time.monotonic() - started_at > timeout:
            raise TimeoutError(f"{action} did not finish within {timeout} seconds")

        time.sleep(VCENTER_POLL_INTERVAL)


def disconnect_vms_from_dvs(service_instance, dvs: vim.DistributedVirtualSwitch) -> int:
    """Disconnect all VM network adapters using portgroups on a DVS.

    The adapter is kept on the VM, but its DVS backing is replaced with an empty
    standard-network backing so the selected DVS no longer owns the vNIC port.
    """
    content = service_instance.RetrieveContent()
    switch_uuid = _dvs_uuid(dvs)
    portgroup_keys = _dvs_portgroup_keys(dvs)
    changed_nics = 0

    for vm in _get_objects(content, vim.VirtualMachine):
        config = getattr(vm, "config", None)
        hardware = getattr(config, "hardware", None)
        devices = getattr(hardware, "device", [])

        device_changes = []
        for device in devices:
            if not isinstance(device, vim.vm.device.VirtualEthernetCard):
                continue
            if not _nic_uses_dvs(device, switch_uuid, portgroup_keys):
                continue

            label = getattr(device.deviceInfo, "label", "network adapter")
            logger.info(
                "Disconnecting VM %s %s from DVS %s", _vm_name(vm), label, dvs.name
            )

            device.backing = vim.vm.device.VirtualEthernetCard.NetworkBackingInfo()
            if device.connectable is None:
                device.connectable = vim.vm.device.VirtualDevice.ConnectInfo()
            device.connectable.connected = False
            device.connectable.startConnected = False
            device.connectable.allowGuestControl = False

            spec = vim.vm.device.VirtualDeviceSpec()
            spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.edit
            spec.device = device
            device_changes.append(spec)

        if not device_changes:
            continue

        vm_config = vim.vm.ConfigSpec()
        vm_config.deviceChange = device_changes
        task = vm.ReconfigVM_Task(vm_config)
        wait_for_task(task, f"disconnect VM {_vm_name(vm)} from DVS {dvs.name}")
        changed_nics += len(device_changes)

    logger.info(
        "Disconnected %d VM network adapter(s) from DVS %s", changed_nics, dvs.name
    )
    return changed_nics


def remove_vmkernel_adapters_from_dvs(dvs: vim.DistributedVirtualSwitch) -> int:
    """Remove VMkernel adapters attached to the selected DVS."""
    switch_uuid = _dvs_uuid(dvs)
    portgroup_keys = _dvs_portgroup_keys(dvs)
    removed = 0

    for host in _host_members(dvs):
        network_system = host.configManager.networkSystem
        vnics = getattr(host.config.network, "vnic", [])
        for vnic in vnics:
            spec = getattr(vnic, "spec", None)
            distributed_port = getattr(spec, "distributedVirtualPort", None)
            if distributed_port is None:
                continue
            if (
                distributed_port.switchUuid != switch_uuid
                and distributed_port.portgroupKey not in portgroup_keys
            ):
                continue

            logger.info(
                "Removing VMkernel adapter %s from host %s",
                vnic.device,
                host.name,
            )
            network_system.RemoveVirtualNic(vnic.device)
            removed += 1

    logger.info("Removed %d VMkernel adapter(s) from DVS %s", removed, dvs.name)
    return removed


def remove_hosts_from_dvs(dvs: vim.DistributedVirtualSwitch) -> int:
    """Disconnect all hosts from the selected DVS."""
    hosts = _host_members(dvs)
    if not hosts:
        logger.info("No hosts are attached to DVS %s", dvs.name)
        return 0

    host_specs = []
    for host in hosts:
        logger.info("Removing host %s from DVS %s", host.name, dvs.name)
        host_spec = vim.dvs.HostMember.ConfigSpec()
        host_spec.operation = vim.ConfigSpecOperation.remove
        host_spec.host = host
        host_specs.append(host_spec)

    config_spec = vim.DistributedVirtualSwitch.ConfigSpec()
    config_spec.configVersion = dvs.config.configVersion
    config_spec.host = host_specs

    task = dvs.ReconfigureDvs_Task(config_spec)
    wait_for_task(task, f"remove hosts from DVS {dvs.name}")
    logger.info("Removed %d host(s) from DVS %s", len(hosts), dvs.name)
    return len(hosts)


def wait_for_dvs_removed(service_instance, name: str) -> None:
    """Wait until vCenter inventory no longer contains the named DVS."""
    logger.info("Waiting for DVS %s to be removed from vCenter", name)
    started_at = time.monotonic()

    while time.monotonic() - started_at <= VCENTER_DVS_REMOVAL_TIMEOUT:
        if find_dvs_by_name(service_instance, name) is None:
            logger.info("Confirmed DVS %s is removed from vCenter", name)
            return
        time.sleep(VCENTER_POLL_INTERVAL)

    raise TimeoutError(
        f"DVS {name} is still present in vCenter after "
        f"{VCENTER_DVS_REMOVAL_TIMEOUT} seconds"
    )


def print_dvs_summary(dvs: vim.DistributedVirtualSwitch) -> None:
    """Print a human-readable DVS summary before destructive cleanup."""
    table = prettytable.PrettyTable(
        field_names=["DVS", "UUID", "Portgroups", "Hosts"],
    )
    table.align = "l"
    table.add_row(
        [
            dvs.name,
            _dvs_uuid(dvs),
            len(getattr(dvs, "portgroup", [])),
            len(_host_members(dvs)),
        ],
    )

    print()
    print("=== Selected vCenter DVS ===")
    print(table)
    print()


def _dvs_portgroup_names(dvs: vim.DistributedVirtualSwitch) -> dict[str, str]:
    """Return portgroup key-to-name mapping for a DVS."""
    return {
        portgroup.key: portgroup.name for portgroup in getattr(dvs, "portgroup", [])
    }


def collect_vm_dvs_attachments(
    service_instance,
    dvs: vim.DistributedVirtualSwitch,
) -> list[tuple[str, str, str]]:
    """Collect VM network adapters attached to portgroups on a DVS.

    :returns: Tuples of (vm_name, adapter_label, portgroup_name).
    """
    content = service_instance.RetrieveContent()
    switch_uuid = _dvs_uuid(dvs)
    portgroup_keys = _dvs_portgroup_keys(dvs)
    portgroup_names = _dvs_portgroup_names(dvs)
    attachments: list[tuple[str, str, str]] = []

    for vm in _get_objects(content, vim.VirtualMachine):
        config = getattr(vm, "config", None)
        hardware = getattr(config, "hardware", None)
        devices = getattr(hardware, "device", [])

        for device in devices:
            if not isinstance(device, vim.vm.device.VirtualEthernetCard):
                continue
            if not _nic_uses_dvs(device, switch_uuid, portgroup_keys):
                continue

            port = device.backing.port
            portgroup_name = portgroup_names.get(port.portgroupKey, port.portgroupKey)
            label = getattr(device.deviceInfo, "label", "network adapter")
            attachments.append((_vm_name(vm), label, portgroup_name))

    attachments.sort(key=lambda item: (item[0].lower(), item[1].lower()))
    return attachments


def print_check_inventory(service_instance, domains: list[VmmDomain]) -> None:
    """Print vCenter DVS inventory for APIC VMware VMM domains."""
    vm_table = prettytable.PrettyTable(
        field_names=["VMM Domain / DVS", "VM", "Adapter", "Portgroup"],
    )
    host_table = prettytable.PrettyTable(
        field_names=["VMM Domain / DVS", "Host", "Connection State"],
    )
    missing_table = prettytable.PrettyTable(field_names=["VMM Domain / DVS", "Status"])

    vm_table.align = "l"
    host_table.align = "l"
    missing_table.align = "l"

    vm_rows = 0
    host_rows = 0
    missing_rows = 0

    for domain in domains:
        dvs = find_dvs_by_name(service_instance, domain.name)
        if dvs is None:
            missing_table.add_row([domain.name, "DVS not found in vCenter"])
            missing_rows += 1
            continue

        for vm_name, adapter, portgroup in collect_vm_dvs_attachments(
            service_instance,
            dvs,
        ):
            vm_table.add_row([domain.name, vm_name, adapter, portgroup])
            vm_rows += 1

        for host in _host_members(dvs):
            state = getattr(
                getattr(host, "runtime", None), "connectionState", "unknown"
            )
            host_table.add_row([domain.name, host.name, state])
            host_rows += 1

    print()
    print("=== VMs Attached to APIC-created DVS Portgroups ===")
    print(
        vm_table if vm_rows else "No VM network adapters found on fetched DVS objects."
    )
    print()

    print("=== Hosts Attached to APIC-created DVS Objects ===")
    print(host_table if host_rows else "No hosts found on fetched DVS objects.")
    print()

    if missing_rows:
        print("=== Fetched Domains Missing in vCenter ===")
        print(missing_table)
        print()


# ---------------------------------------------------------------------------
# Operator interaction
# ---------------------------------------------------------------------------


def print_domain_table(domains: list[VmmDomain]) -> None:
    """Print APIC VMware VMM domains in a numbered table."""
    table = prettytable.PrettyTable(field_names=["#", "VMM Domain / DVS", "APIC DN"])
    table.align = "l"

    for index, domain in enumerate(domains, start=1):
        table.add_row([index, domain.name, domain.dn])

    print()
    print("=== APIC VMware VMM Domains ===")
    print(table)
    print()


def _dn_segment_value(dn: str, prefix: str) -> str:
    """Return a named APIC DN segment value without its class prefix."""
    for segment in dn.split("/"):
        if segment.startswith(prefix):
            return segment.removeprefix(prefix)
    return ""


def print_epg_domain_assignments(
    host: str,
    user: str,
    password: str,
    domains: list[VmmDomain],
) -> None:
    """Print EPGs associated with fetched VMware VMM domains."""
    table = prettytable.PrettyTable(
        field_names=[
            "VMM Domain / DVS",
            "Tenant",
            "App Profile",
            "EPG",
            "Relation DN",
        ],
    )
    table.align = "l"
    rows = 0

    with requests.Session() as session:
        _apic_login(session, host, user, password)
        for domain in domains:
            for relation_dn in fetch_epg_domain_attachments(session, host, domain.dn):
                table.add_row(
                    [
                        domain.name,
                        _dn_segment_value(relation_dn, "tn-"),
                        _dn_segment_value(relation_dn, "ap-"),
                        _dn_segment_value(relation_dn, "epg-"),
                        relation_dn,
                    ],
                )
                rows += 1

    print()
    print("=== EPGs Assigned to APIC VMware VMM Domains ===")
    print(table if rows else "No EPG domain associations found.")
    print()


def select_domain(domains: list[VmmDomain]) -> VmmDomain:
    """Prompt the operator to select a VMM domain."""
    print_domain_table(domains)

    while True:
        try:
            choice = input("Select VMM Domain / DVS number to cleanup: ").strip()
        except (KeyboardInterrupt, EOFError):
            print()
            logger.info("Aborted.")
            sys.exit(0)

        if not choice:
            continue
        if not choice.isdigit():
            print("Enter a number from the table.")
            continue

        index = int(choice)
        if 1 <= index <= len(domains):
            return domains[index - 1]
        print(f"Enter a number between 1 and {len(domains)}.")


def confirm_cleanup(domain: VmmDomain) -> None:
    """Require explicit confirmation before destructive cleanup."""
    logger.warning(
        "DESTRUCTIVE OPERATION: VMs and hosts will be disconnected from DVS %s, "
        "and APIC VMM domain %s will be deleted. This cannot be undone.",
        domain.name,
        domain.dn,
    )

    try:
        confirmation = input(f'Type "CLEANUP {domain.name}" to confirm: ')
    except (KeyboardInterrupt, EOFError):
        print()
        logger.info("Aborted.")
        sys.exit(0)

    if confirmation != f"CLEANUP {domain.name}":
        logger.info("Aborted.")
        sys.exit(0)


# ---------------------------------------------------------------------------
# Connectivity check
# ---------------------------------------------------------------------------


def _check_apic(cfg: Config) -> tuple[bool, str]:
    """Try to authenticate to APIC and list VMware VMM domains."""
    try:
        domains = fetch_vmware_vmm_domains(
            cfg.apic_host,
            cfg.apic_user,
            cfg.apic_password,
        )
        return True, f"OK ({len(domains)} VMware VMM domain(s))"
    except requests.HTTPError as exc:
        return False, f"HTTP {exc.response.status_code}"
    except requests.exceptions.ConnectTimeout:
        return False, "connection timed out"
    except requests.exceptions.ConnectionError:
        return False, "connection error"
    except requests.exceptions.Timeout:
        return False, "timed out"
    except requests.RequestException as exc:
        return False, str(exc)


def _check_vcenter(cfg: Config) -> tuple[bool, str]:
    """Try to authenticate to vCenter and list distributed switches."""
    service_instance = None
    try:
        service_instance = connect_vcenter(cfg)
        content = service_instance.RetrieveContent()
        switches = _get_objects(content, vim.DistributedVirtualSwitch)
        return True, f"OK ({len(switches)} DVS object(s))"
    except vmodl.MethodFault as exc:
        return False, exc.msg
    except OSError as exc:
        return False, str(exc)
    finally:
        if service_instance is not None:
            Disconnect(service_instance)


def _print_check_report(results: list[CheckResult]) -> None:
    """Print a formatted connectivity check report table."""
    table = prettytable.PrettyTable(field_names=["System", "Target", "Status"])
    table.align = "l"

    for system, target, ok, msg in results:
        table.add_row([system, target, "OK" if ok else f"FAILED: {msg}"])

    print()
    print("=== Connectivity Check Report ===")
    print(table)
    ok_count = sum(1 for _, _, ok, _ in results if ok)
    print(f"Result: {ok_count}/{len(results)} systems reachable")
    print()


def run_check(cfg: Config) -> None:
    """Check connectivity to APIC and vCenter without executing cleanup."""
    logger.info(
        "=== VMM DVS Cleanup Connectivity Check (APIC: %s, vCenter: %s) ===",
        cfg.apic_host,
        cfg.vcenter_host,
    )

    domains: list[VmmDomain] = []
    apic_ok = False
    try:
        domains = fetch_vmware_vmm_domains(
            cfg.apic_host,
            cfg.apic_user,
            cfg.apic_password,
        )
        apic_ok = True
        apic_msg = f"OK ({len(domains)} VMware VMM domain(s))"
    except requests.HTTPError as exc:
        apic_msg = f"HTTP {exc.response.status_code}"
    except requests.exceptions.ConnectTimeout:
        apic_msg = "connection timed out"
    except requests.exceptions.ConnectionError:
        apic_msg = "connection error"
    except requests.exceptions.Timeout:
        apic_msg = "timed out"
    except requests.RequestException as exc:
        apic_msg = str(exc)

    service_instance = None
    vcenter_ok = False
    try:
        service_instance = connect_vcenter(cfg)
        content = service_instance.RetrieveContent()
        switches = _get_objects(content, vim.DistributedVirtualSwitch)
        vcenter_ok = True
        vcenter_msg = f"OK ({len(switches)} DVS object(s))"
    except vmodl.MethodFault as exc:
        vcenter_msg = exc.msg
    except OSError as exc:
        vcenter_msg = str(exc)

    _print_check_report(
        [
            ("APIC REST API", cfg.apic_host, apic_ok, apic_msg),
            ("vCenter API", cfg.vcenter_host, vcenter_ok, vcenter_msg),
        ],
    )

    try:
        if domains:
            print_domain_table(domains)
            print_epg_domain_assignments(
                cfg.apic_host,
                cfg.apic_user,
                cfg.apic_password,
                domains,
            )
        else:
            logger.info("No VMware VMM domains fetched from APIC")

        if service_instance is not None and domains:
            print_check_inventory(service_instance, domains)
    finally:
        if service_instance is not None:
            Disconnect(service_instance)


# ---------------------------------------------------------------------------
# Cleanup orchestration
# ---------------------------------------------------------------------------


def run_cleanup(cfg: Config) -> None:
    """Orchestrate APIC-created vCenter DVS cleanup."""
    logger.info("=== VMM DVS Cleanup started (APIC: %s) ===", cfg.apic_host)

    domains = fetch_vmware_vmm_domains(cfg.apic_host, cfg.apic_user, cfg.apic_password)
    if not domains:
        logger.error("No VMware VMM domains found on APIC")
        sys.exit(1)

    domain = select_domain(domains)
    service_instance = None

    try:
        service_instance = connect_vcenter(cfg)
        dvs = find_dvs_by_name(service_instance, domain.name)
        if dvs is None:
            logger.error("DVS %s was not found in vCenter", domain.name)
            sys.exit(1)

        print_dvs_summary(dvs)
        confirm_cleanup(domain)

        disconnect_vms_from_dvs(service_instance, dvs)
        remove_vmkernel_adapters_from_dvs(dvs)
        remove_hosts_from_dvs(dvs)

        with requests.Session() as session:
            _apic_login(session, cfg.apic_host, cfg.apic_user, cfg.apic_password)
            remove_domain_from_epgs(session, cfg.apic_host, domain)
            delete_vmm_domain(session, cfg.apic_host, domain)

        wait_for_dvs_removed(service_instance, domain.name)
    except (
        requests.RequestException,
        vmodl.MethodFault,
        RuntimeError,
        TimeoutError,
    ) as exc:
        logger.error("Cleanup failed: %s", exc)
        sys.exit(1)
    finally:
        if service_instance is not None:
            Disconnect(service_instance)

    logger.info("=== VMM DVS Cleanup completed ===")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    """Parse CLI arguments.

    :returns: Parsed argument namespace.
    """
    parser = argparse.ArgumentParser(
        description="Cleanup APIC-created vCenter DVS",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "test connectivity to APIC and vCenter; print a report"
            " without executing any cleanup commands"
        ),
    )
    parser.add_argument(
        "--log-level",
        choices=LOG_LEVELS,
        default="INFO",
        metavar="LEVEL",
        help=("logging verbosity: DEBUG, INFO, WARNING, ERROR (default: INFO)"),
    )
    return parser.parse_args()


def _load_config() -> Config:
    """Load and validate configuration from environment variables.

    :returns: Populated :class:`Config` instance.
    :raises SystemExit: If any required variable is missing or empty.
    """
    apic_host = os.environ.get("ACI_HOST", "")
    apic_user = os.environ.get("ACI_USER", "")
    apic_password = os.environ.get("ACI_PASS", "")
    vcenter_host = os.environ.get("VCENTER_HOST", "")
    vcenter_user = os.environ.get("VCENTER_USER", "")
    vcenter_password = os.environ.get("VCENTER_PASS", "")

    missing = [
        name
        for name, val in [
            ("ACI_HOST", apic_host),
            ("ACI_USER", apic_user),
            ("ACI_PASS", apic_password),
            ("VCENTER_HOST", vcenter_host),
            ("VCENTER_USER", vcenter_user),
            ("VCENTER_PASS", vcenter_password),
        ]
        if not val
    ]
    if missing:
        logger.error(
            "Missing required environment variable(s): %s",
            ", ".join(missing),
        )
        sys.exit(1)

    return Config(
        apic_host=apic_host,
        apic_user=apic_user,
        apic_password=apic_password,
        vcenter_host=vcenter_host,
        vcenter_user=vcenter_user,
        vcenter_password=vcenter_password,
    )


def main() -> None:
    """Run connectivity check or full VMM DVS cleanup."""
    args = _parse_args()
    logging.getLogger().setLevel(getattr(logging, args.log_level))

    cfg = _load_config()
    if args.check:
        run_check(cfg)
        return

    run_cleanup(cfg)


if __name__ == "__main__":
    main()
