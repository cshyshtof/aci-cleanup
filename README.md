# ACI Cleanup

> [!CAUTION]
> This repo is for LAB usage ONLY!

> [!CAUTION]
> SCRIPTS IN THIS REPO ARE DESTRUCTIVE TO ACI AND VCENTER

> [!CAUTION]
> Use on your own risk !!!

Automates factory reset operations for a Cisco ACI fabric. The main script connects to APIC through the REST API to discover switch OOB addresses, then resets APIC and each fabric switch.

The repository also includes a separate APIC/vCenter cleanup script for removing an APIC-created VMware VMM Domain and the matching vCenter DVS.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) package and environment manager
- SSH and REST API access to APIC
- SSH OOB access to each fabric switch
- vCenter API access for `cleanup-vcenter-aci.py`

## Environment Setup

Install `uv`:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Clone the repository:

```bash
git clone <repository-url>
cd aci-cleanup
```

Create the virtual environment and install dependencies:

```bash
make init
```

After `make init`, the project directory contains a `.venv` environment with `paramiko`, `requests`, `prettytable`, `pyvmomi`, and `ruff`.

## Configuration

### Environment Variables

The ACI factory reset script requires these variables:

```bash
export ACI_HOST="192.168.1.1"  # APIC IP address or hostname
export ACI_USER="admin"        # APIC username
export ACI_PASS="secret"       # APIC password
```

The APIC/vCenter DVS cleanup script also requires:

```bash
export VCENTER_HOST="vcenter.example.local"  # vCenter IP address or hostname
export VCENTER_USER="administrator@vsphere.local"
export VCENTER_PASS="secret"
```

You can place these variables in a local `.env` file and load it with:

```bash
source .env
```

### switches.csv

The `switches.csv` file maps switch names to OOB IP addresses. The format is `switch_name,switch_ip`, without a header row.

```
"leaf101","10.1.44.101"
"leaf102","10.1.44.102"
"spine121","10.1.44.121"
```

CSV addresses are used as a fallback. If APIC returns an OOB address for a switch, the APIC value takes priority.

## Running

```bash
make run
```

Or run the script directly with `uv`:

```bash
uv run aci-cleanup.py
```

### APIC and vCenter DVS Cleanup

The `cleanup-vcenter-aci.py` script removes a selected VMware VMM Domain from APIC and cleans up the matching DVS in vCenter.

> [!IMPORTANT]
> Run this script before full ACI cleanup, as dVS cannot be removed from vCenter directly, and you will get stuck with it

The script first fetches VMware VMM Domains from APIC and asks the operator to select one. It then disconnects VM vNICs from portgroups on that DVS, removes VMkernel adapters that use the DVS, disconnects ESXi hosts from the DVS, removes VMM Domain bindings from EPGs, deletes the VMM Domain in APIC, and confirms that the DVS is no longer present in vCenter.

Run a read-only connectivity and inventory check:

```bash
make run-vcenter-check
```

The check output includes:

- APIC and vCenter connectivity status
- VMware VMM Domains fetched from APIC
- EPGs assigned to each VMM Domain
- VMs attached to portgroups on each matching DVS
- ESXi hosts attached to each matching DVS

Run the cleanup:

```bash
make run-vcenter
```

Or run the script directly with `uv`:

```bash
uv run cleanup-vcenter-aci.py
```

### Example Output

```
2026-04-24T10:00:01 [WARNING ] DESTRUCTIVE OPERATION: all ACI devices will be reset to factory defaults.
2026-04-24T10:00:01 [INFO    ] === ACI Cleanup started (APIC: 192.168.1.1) ===
2026-04-24T10:00:01 [INFO    ] Loaded 6 switches from switches.csv
2026-04-24T10:00:02 [INFO    ] APIC REST API login successful: 192.168.1.1
2026-04-24T10:00:02 [INFO    ] Discovered leaf node: leaf101 at 10.10.244.101
2026-04-24T10:00:02 [INFO    ] Fetched 6 fabric nodes from APIC API
2026-04-24T10:00:02 [INFO    ] Starting APIC SSH cleanup: 192.168.1.1
2026-04-24T10:00:03 [INFO    ] SSH connecting to 192.168.1.1
2026-04-24T10:00:03 [INFO    ] SSH [192.168.1.1] >>> acidiag touch clean
2026-04-24T10:00:05 [INFO    ] SSH [192.168.1.1] >>> acidiag touch setup
2026-04-24T10:00:07 [INFO    ] SSH [192.168.1.1] >>> acidiag reboot
2026-04-24T10:00:09 [INFO    ] APIC cleanup commands dispatched successfully
2026-04-24T10:00:09 [INFO    ] Cleaning switch leaf101 at 10.10.244.101 (source: API)
...
2026-04-24T10:01:30 [INFO    ] === ACI Cleanup completed ===
```

## How the ACI Factory Reset Works

```
1. Validate environment variables
2. Load switches.csv -> switch name to IP address map
3. Log in to APIC REST API -> fetch switch OOB addresses
4. SSH to APIC:
   acidiag touch clean  ->  auto-confirm y
   acidiag touch setup  ->  auto-confirm y
   acidiag reboot       ->  auto-confirm y  (connection is dropped)
5. SSH to each switch through OOB:
   setup-clean-config.sh  ->  auto-confirm y
```

## Makefile Targets

```
make init               - create the virtual environment and install dependencies
make run                - run the ACI factory reset script
make run-check          - check APIC and switch connectivity without cleanup
make run-vcenter        - run APIC/vCenter DVS cleanup
make run-vcenter-check  - check APIC/vCenter connectivity and inventory
make format             - format Python code with ruff
make check              - verify linting and formatting without modifying files
make clean              - remove .venv and cache directories
```

## Troubleshooting

### Missing Environment Variables

```
[ERROR   ] Missing required environment variable(s): ACI_HOST, ACI_PASS
```

Set the missing variables, for example:
`export ACI_HOST="..."` and `export ACI_PASS="..."`.

### API or SSH Authentication Error

```
[ERROR   ] SSH connection failed for 192.168.1.1: Authentication failed.
```

Check `ACI_USER` and `ACI_PASS`. Make sure the account has the required APIC API and SSH permissions.

### Switch Unreachable Through SSH

```
[ERROR   ] Switch leaf101 (10.10.244.101) failed: [Errno 110] Connection timed out
```

The script continues with the remaining switches and prints a final error summary. Check the switch OOB management reachability.

### Missing OOB Address in APIC API

```
[WARNING ] Node leaf101 (leaf) has no OOB address - CSV fallback
```

This is diagnostic information. The script will use the address from `switches.csv`; it is not a fatal error.

### Ruff Errors

```bash
make check    # show lint and formatting errors without modifying files
make format   # apply ruff formatting automatically
```

## License

This project is licensed under the MIT License - see the LICENSE file for details. Support

## Final notes

> [!NOTE]
> Legal Notice: This repo is provided "as is", without warranty of any kind, express or implied. No guarantees are made regarding its functionality or suitability for any particular purpose. The author assumes no responsibility or liability for any damages, losses, or consequences resulting from the use, misuse, or inability to use this repo.

> [!NOTE]
> AI assited coding
