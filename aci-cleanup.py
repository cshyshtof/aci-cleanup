#!/usr/bin/env python3
"""aci-cleanup.py — Cisco ACI factory reset.

The script performs the following actions:
- Connects to APIC via REST API
- Discovers fabric nodes
- Resets APIC and all switches to factory defaults

Usage:
    uv run aci-cleanup.py [--check] [--log-level LEVEL]

Flags:
    --check              Test connectivity to APIC and switches
                         Print a report without executing any cleanups
    --log-level LEVEL    Verbosity: DEBUG, INFO, WARNING, ERROR (default: INFO)

Required environment variables:
    ACI_HOST — APIC management IP or hostname
    ACI_USER — login username
    ACI_PASS — login password
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import re
import sys
import time
from dataclasses import dataclass

import paramiko
import prettytable
import requests
import urllib3

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SWITCHES_CSV: str = "switches.csv"

SSH_TIMEOUT: int = 60  # per-command wait limit (seconds)
SSH_CONNECT_TIMEOUT: int = 10  # TCP connect timeout (seconds)
SSH_RECV_BUFFER: int = 4096
SSH_POLL_INTERVAL: float = 0.3
IDLE_TIMEOUT: float = 2.0  # seconds of silence = command finished

API_TIMEOUT: int = 30
API_LOGIN_PATH: str = "/api/aaaLogin.json"
API_NODES_PATH: str = "/api/node/class/topSystem.json"
API_NODES_FILTER: str = 'query-target-filter=ne(topSystem.role,"controller")'

APIC_COMMANDS: list[str] = [
    "acidiag touch clean",
    "acidiag touch setup",
    "acidiag reboot",
]
SWITCH_CLEANUP_CMD: str = "setup-clean-config.sh"
SWITCH_DONE_SENTINEL: str = "Done"
SWITCH_RELOAD_CMD: str = "reload"
SWITCH_CLEANUP_TIMEOUT: int = 300  # max wait for "Done" (seconds)

CONFIRM_RESPONSE: str = "y\n"
CONFIRM_PATTERNS: list[str] = [
    r"\[y/N\]",
    r"\[Y/n\]",
    r"\[yes/no\]",
    r"\(y/n\)",
    r"\(Y/N\)",
    r"Are you sure",
    r"Do you want to continue",
    r"confirm",
]

LOG_FORMAT: str = "%(asctime)s [%(levelname)-8s] %(message)s"
LOG_DATE_FORMAT: str = "%Y-%m-%dT%H:%M:%S"
LOG_LEVELS: list[str] = ["DEBUG", "INFO", "WARNING", "ERROR"]

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

SwitchMap = dict[str, str]  # switch_name -> ip_address

# (dev_type, name, ip, reachable, status_message)
CheckResult = tuple[str, str, str, bool, str]

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    """Runtime configuration loaded from environment variables.

    :param host: APIC management IP or hostname.
    :param user: SSH and API username.
    :param password: SSH and API password.
    :param csv_path: Path to the switches CSV file.
    """

    host: str
    user: str
    password: str
    csv_path: str = SWITCHES_CSV


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

# APIC uses a self-signed TLS certificate
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def load_switches(csv_path: str) -> SwitchMap:
    """Load switch name-to-IP mapping from a CSV file.

    :param csv_path: Path to CSV with columns ``switch_name,switch_ip``.
    :returns: Dictionary mapping switch name to IP address.
    :raises FileNotFoundError: If *csv_path* does not exist.
    :raises ValueError: If any row has an unexpected format.
    """
    if not os.path.isfile(csv_path):
        raise FileNotFoundError(f"Switches CSV not found: {csv_path}")

    result: SwitchMap = {}
    with open(csv_path, newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        for line_num, row in enumerate(reader, start=1):
            if not row or all(field.strip() == "" for field in row):
                continue
            if len(row) != 2:
                raise ValueError(
                    f"CSV line {line_num}: expected 2 columns, got {len(row)}"
                )
            name = row[0].strip().strip('"')
            ip = row[1].strip().strip('"')
            if not name or not ip:
                raise ValueError(f"CSV line {line_num}: empty name or IP address")
            result[name] = ip

    logger.info("Loaded %d switches from %s", len(result), csv_path)
    return result


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

    Stores the session cookie in *session* for subsequent requests.

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


def fetch_apic_nodes(host: str, user: str, password: str) -> SwitchMap:
    """Fetch leaf and spine OOB addresses from the APIC REST API.

    Queries ``topSystem`` for all non-controller nodes and returns
    their out-of-band management addresses.

    :param host: APIC management IP or hostname.
    :param user: API username.
    :param password: API password.
    :returns: Dictionary mapping node name to OOB management IP.
    :raises requests.HTTPError: On API request failure.
    :raises KeyError: If the response structure is unexpected.
    """
    result: SwitchMap = {}
    with requests.Session() as session:
        _apic_login(session, host, user, password)
        url = f"https://{host}{API_NODES_PATH}?{API_NODES_FILTER}"
        response = session.get(url, verify=False, timeout=API_TIMEOUT)
        response.raise_for_status()

        for item in response.json().get("imdata", []):
            attrs = item["topSystem"]["attributes"]
            name: str = attrs["name"]
            oob_ip: str = attrs.get("oobMgmtAddr", "")
            role: str = attrs.get("role", "unknown")

            if not oob_ip or oob_ip == "0.0.0.0":
                logger.warning(
                    "Node %s (%s) has no OOB address — CSV fallback",
                    name,
                    role,
                )
                continue

            result[name] = oob_ip
            logger.info("Discovered %s node: %s at %s", role, name, oob_ip)

    logger.info("Fetched %d fabric nodes from APIC API", len(result))
    return result


# ---------------------------------------------------------------------------
# SSH helpers
# ---------------------------------------------------------------------------


def _wait_for_output(
    channel: paramiko.Channel,
    timeout: int = SSH_TIMEOUT,
) -> str:
    """Read channel output and auto-confirm interactive prompts.

    Returns when the channel is idle for ``IDLE_TIMEOUT`` seconds,
    the total *timeout* expires, or the remote side closes the channel
    (expected behaviour after ``acidiag reboot``).

    :param channel: Paramiko interactive shell channel.
    :param timeout: Maximum seconds to wait for output.
    :returns: All received data decoded as UTF-8 string.
    """
    confirm_re = re.compile("|".join(CONFIRM_PATTERNS), re.IGNORECASE)
    output_parts: list[str] = []
    deadline = time.monotonic() + timeout
    last_recv = time.monotonic()

    while time.monotonic() < deadline:
        try:
            if channel.recv_ready():
                raw = channel.recv(SSH_RECV_BUFFER)
                if not raw:
                    break  # remote EOF
                text = raw.decode("utf-8", errors="replace")
                output_parts.append(text)
                last_recv = time.monotonic()

                if confirm_re.search(text):
                    logger.debug("Confirmation prompt — sending 'y'")
                    channel.send(CONFIRM_RESPONSE)
                    last_recv = time.monotonic()

            elif channel.exit_status_ready():
                break
            elif time.monotonic() - last_recv > IDLE_TIMEOUT:
                break
            else:
                time.sleep(SSH_POLL_INTERVAL)

        except (EOFError, OSError) as exc:
            # Expected when remote disconnects (e.g. after reboot)
            logger.debug("Channel closed during receive: %s", exc)
            break

    return "".join(output_parts)


def _wait_for_sentinel(
    channel: paramiko.Channel,
    sentinel: str,
    timeout: int = SSH_TIMEOUT,
    line_prefix: str = "",
) -> tuple[str, bool]:
    """Read channel output until a sentinel string appears or timeout expires.

    Unlike ``_wait_for_output``, this function does not exit on channel
    inactivity — it keeps reading until *sentinel* appears, the *timeout*
    expires, or the remote side closes the channel.  Confirmation prompts
    are handled automatically.

    When *line_prefix* is non-empty, each complete output line is logged
    at INFO level in real time (useful for long-running scripts).

    :param channel: Paramiko interactive shell channel.
    :param sentinel: String whose presence signals completion.
    :param timeout: Maximum seconds to wait.
    :param line_prefix: If set, prefix logged to each output line.
    :returns: Tuple of (accumulated output, sentinel_found).
    """
    confirm_re = re.compile("|".join(CONFIRM_PATTERNS), re.IGNORECASE)
    output_parts: list[str] = []
    line_buf: str = ""
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        try:
            if channel.recv_ready():
                raw = channel.recv(SSH_RECV_BUFFER)
                if not raw:
                    break  # remote EOF
                text = raw.decode("utf-8", errors="replace")
                output_parts.append(text)

                if line_prefix:
                    line_buf += text
                    # SSH uses \r\n; strip \r before splitting
                    *lines, line_buf = line_buf.replace("\r", "").split("\n")
                    for line in lines:
                        stripped = line.strip()
                        if stripped:
                            logger.info("%s%s", line_prefix, stripped)

                if confirm_re.search(text):
                    logger.debug("Confirmation prompt — sending 'y'")
                    channel.send(CONFIRM_RESPONSE)

                if sentinel in "".join(output_parts):
                    return "".join(output_parts), True

            elif channel.exit_status_ready():
                break
            else:
                time.sleep(SSH_POLL_INTERVAL)

        except (EOFError, OSError) as exc:
            logger.debug("Channel closed during receive: %s", exc)
            break

    accumulated = "".join(output_parts)
    return accumulated, sentinel in accumulated


def ssh_run_commands(
    ip: str,
    user: str,
    password: str,
    commands: list[str],
    timeout: int = SSH_TIMEOUT,
) -> str:
    """Connect via SSH and execute commands in an interactive shell.

    Handles confirmation prompts automatically via ``_wait_for_output``.
    Gracefully handles remote disconnects (e.g. after a reboot command).

    :param ip: Target host IP address.
    :param user: SSH username.
    :param password: SSH password.
    :param commands: Commands to execute sequentially.
    :param timeout: Per-command wait timeout in seconds.
    :returns: Accumulated decoded output from all commands.
    :raises paramiko.SSHException: On connection or authentication failure.
    :raises TimeoutError: If the TCP connection times out.
    :raises OSError: On network-level errors.
    """
    client = paramiko.SSHClient()
    # AutoAddPolicy is intentional: ACI devices have rotating SSH host keys
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        logger.info("SSH connecting to %s", ip)
        client.connect(
            hostname=ip,
            username=user,
            password=password,
            timeout=SSH_CONNECT_TIMEOUT,
            allow_agent=False,
            look_for_keys=False,
        )
        channel = client.invoke_shell()
        channel.settimeout(SSH_TIMEOUT)

        _wait_for_output(channel, timeout=15)  # consume login banner

        all_output: list[str] = []
        for cmd in commands:
            logger.info("SSH [%s] >>> %s", ip, cmd)
            try:
                channel.send(f"{cmd}\n")
            except (OSError, EOFError) as exc:
                logger.debug("Send interrupted for %r on %s: %s", cmd, ip, exc)
                break
            output = _wait_for_output(channel, timeout=timeout)
            all_output.append(output)

        return "\n".join(all_output)

    except (paramiko.SSHException, TimeoutError, OSError) as exc:
        logger.error("SSH connection failed for %s: %s", ip, exc)
        raise
    finally:
        client.close()


def _run_switch_cleanup(
    ip: str,
    user: str,
    password: str,
    name: str,
) -> None:
    """Execute the factory reset sequence on a single switch via SSH.

    Sends ``setup-clean-config.sh`` and waits up to
    ``SWITCH_CLEANUP_TIMEOUT`` seconds for ``SWITCH_DONE_SENTINEL`` to
    appear in the output.  Regardless of whether the sentinel was received,
    issues a ``reload`` command afterwards (confirmation handled
    automatically).

    :param ip: Switch management IP address.
    :param user: SSH username.
    :param password: SSH password.
    :param name: Switch hostname (used for logging only).
    :raises paramiko.SSHException: On SSH connection or auth failure.
    :raises TimeoutError: If the TCP connection times out.
    :raises OSError: On network-level errors.
    """
    client = paramiko.SSHClient()
    # AutoAddPolicy is intentional: ACI devices have rotating SSH host keys
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        logger.info("SSH connecting to %s (%s)", name, ip)
        client.connect(
            hostname=ip,
            username=user,
            password=password,
            timeout=SSH_CONNECT_TIMEOUT,
            allow_agent=False,
            look_for_keys=False,
        )
        channel = client.invoke_shell()
        channel.settimeout(SSH_TIMEOUT)

        _wait_for_output(channel, timeout=15)  # consume login banner

        logger.info("SSH [%s] >>> %s", ip, SWITCH_CLEANUP_CMD)
        channel.send(f"{SWITCH_CLEANUP_CMD}\n")

        _, done = _wait_for_sentinel(
            channel,
            SWITCH_DONE_SENTINEL,
            timeout=SWITCH_CLEANUP_TIMEOUT,
            line_prefix=f"[{name}] ",
        )
        if done:
            logger.info("Switch %s: cleanup script finished (Done)", name)
        else:
            logger.warning(
                "Switch %s: timed out waiting for %r after %ds — proceeding"
                " with reload anyway",
                name,
                SWITCH_DONE_SENTINEL,
                SWITCH_CLEANUP_TIMEOUT,
            )

        logger.info("SSH [%s] >>> %s", ip, SWITCH_RELOAD_CMD)
        try:
            channel.send(f"{SWITCH_RELOAD_CMD}\n")
        except (OSError, EOFError) as exc:
            logger.debug("Send interrupted on %s: %s", ip, exc)
            return

        # Wait for reload confirmation prompt and auto-confirm via
        # _wait_for_output (CONFIRM_PATTERNS already matches "(y/n)?")
        _wait_for_output(channel, timeout=30)

    except (paramiko.SSHException, TimeoutError, OSError) as exc:
        logger.error("SSH connection failed for %s: %s", ip, exc)
        raise
    finally:
        client.close()


# ---------------------------------------------------------------------------
# Cleanup operations
# ---------------------------------------------------------------------------


def apic_ssh_cleanup(cfg: Config) -> None:
    """Execute the APIC factory reset sequence via SSH.

    Sends ``acidiag touch clean``, ``acidiag touch setup``, and
    ``acidiag reboot`` in sequence. The connection drop after the
    reboot command is expected and handled transparently.

    :param cfg: Runtime configuration.
    :raises paramiko.SSHException: If the SSH connection cannot be established.
    """
    logger.info("Starting APIC SSH cleanup: %s", cfg.host)
    ssh_run_commands(cfg.host, cfg.user, cfg.password, APIC_COMMANDS)
    logger.info("APIC cleanup commands dispatched successfully")


def switch_ssh_cleanup(
    api_nodes: SwitchMap,
    csv_nodes: SwitchMap,
    cfg: Config,
) -> None:
    """Execute factory reset on each fabric switch via SSH.

    IP address priority: OOB address from APIC API takes precedence
    over the CSV address.  All switches are attempted regardless of
    individual failures; errors are collected and raised together.

    :param api_nodes: Node name → OOB IP from APIC REST API.
    :param csv_nodes: Node name → IP from switches.csv.
    :param cfg: Runtime configuration.
    :raises RuntimeError: If cleanup failed on one or more switches.
    """
    # CSV as base, API addresses override (API has higher priority)
    merged: SwitchMap = {**csv_nodes, **api_nodes}

    if not merged:
        logger.warning("Switch list is empty — nothing to clean")
        return

    errors: list[str] = []
    for name, ip in merged.items():
        source = "API" if name in api_nodes else "CSV"
        logger.info("Cleaning switch %s at %s (source: %s)", name, ip, source)
        try:
            _run_switch_cleanup(ip, cfg.user, cfg.password, name)
            logger.info("Switch %s reload initiated", name)
        except (paramiko.SSHException, TimeoutError, OSError) as exc:
            logger.error("Switch %s (%s) failed: %s", name, ip, exc)
            errors.append(f"{name} ({ip}): {exc}")

    if errors:
        failed = "\n  ".join(errors)
        raise RuntimeError(f"Cleanup failed for {len(errors)} switch(es):\n  {failed}")


# ---------------------------------------------------------------------------
# Connectivity check
# ---------------------------------------------------------------------------


def _check_apic_api(host: str, user: str, password: str) -> tuple[bool, str]:
    """Try to authenticate against the APIC REST API.

    :param host: APIC management IP or hostname.
    :param user: API username.
    :param password: API password.
    :returns: Tuple of (success, status_message).
    """
    try:
        with requests.Session() as session:
            _apic_login(session, host, user, password)
        return True, "OK"
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


def _check_ssh_reachable(
    ip: str,
    user: str,
    password: str,
) -> tuple[bool, str]:
    """Try to establish an SSH session without executing any commands.

    :param ip: Target host IP address.
    :param user: SSH username.
    :param password: SSH password.
    :returns: Tuple of (success, status_message).
    """
    client = paramiko.SSHClient()
    # AutoAddPolicy is intentional: ACI devices have rotating SSH host keys
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=ip,
            username=user,
            password=password,
            timeout=SSH_CONNECT_TIMEOUT,
            allow_agent=False,
            look_for_keys=False,
        )
        return True, "OK"
    except paramiko.AuthenticationException:
        return False, "Authentication failed"
    except (paramiko.SSHException, TimeoutError, OSError) as exc:
        return False, str(exc)
    finally:
        client.close()


def _print_check_report(results: list[CheckResult]) -> None:
    """Print a formatted connectivity check report table.

    :param results: List of (dev_type, name, ip, success, message) tuples.
    """
    table = prettytable.PrettyTable(
        field_names=["Type", "Name", "IP address", "Status"]
    )
    table.align = "l"

    for dev_type, name, ip, ok, msg in results:
        table.add_row([dev_type, name, ip, "OK" if ok else f"FAILED: {msg}"])

    print()
    print("=== Connectivity Check Report ===")
    print(table)

    ok_count = sum(1 for _, _, _, ok, _ in results if ok)
    print(f"Result: {ok_count}/{len(results)} devices reachable")
    print()


def run_check(cfg: Config) -> None:
    """Check connectivity to APIC and all switches; print a report.

    Tests APIC REST API login, APIC SSH, and SSH to each fabric switch.
    No cleanup commands are sent.

    :param cfg: Runtime configuration.
    """
    logger.info("=== ACI Connectivity Check (APIC: %s) ===", cfg.host)
    results: list[CheckResult] = []

    # Check APIC REST API
    logger.debug("Checking APIC REST API...")
    api_ok, api_msg = _check_apic_api(cfg.host, cfg.user, cfg.password)
    results.append(("APIC REST API", "apic", cfg.host, api_ok, api_msg))
    logger.info("APIC REST API: %s", api_msg)

    # Check APIC SSH
    logger.debug("Checking APIC SSH...")
    ssh_ok, ssh_msg = _check_ssh_reachable(cfg.host, cfg.user, cfg.password)
    results.append(("APIC SSH", "apic", cfg.host, ssh_ok, ssh_msg))
    logger.info("APIC SSH: %s", ssh_msg)

    # Load CSV switch list (best effort — may not exist)
    csv_nodes: SwitchMap = {}
    try:
        csv_nodes = load_switches(cfg.csv_path)
    except (FileNotFoundError, ValueError) as exc:
        logger.warning("Cannot load switch list: %s", exc)

    # Fetch fabric nodes from APIC API (only if API is reachable)
    api_nodes: SwitchMap = {}
    if api_ok:
        try:
            api_nodes = fetch_apic_nodes(cfg.host, cfg.user, cfg.password)
        except Exception as exc:
            logger.warning("Could not fetch fabric nodes from API: %s", exc)

    # API addresses take priority over CSV (same as cleanup mode)
    merged: SwitchMap = {**csv_nodes, **api_nodes}
    for name, ip in merged.items():
        source = "API" if name in api_nodes else "CSV"
        logger.debug("Checking switch %s (%s) at %s...", name, source, ip)
        sw_ok, sw_msg = _check_ssh_reachable(ip, cfg.user, cfg.password)
        results.append((f"Switch ({source})", name, ip, sw_ok, sw_msg))
        logger.info("Switch %s (%s): %s", name, source, sw_msg)

    _print_check_report(results)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    """Parse CLI arguments.

    :returns: Parsed argument namespace.
    """
    parser = argparse.ArgumentParser(
        description="Cisco ACI factory reset automation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "test connectivity to APIC and switches; print a report"
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
    host = os.environ.get("ACI_HOST", "")
    user = os.environ.get("ACI_USER", "")
    password = os.environ.get("ACI_PASS", "")

    missing = [
        name
        for name, val in [
            ("ACI_HOST", host),
            ("ACI_USER", user),
            ("ACI_PASS", password),
        ]
        if not val
    ]
    if missing:
        logger.error(
            "Missing required environment variable(s): %s",
            ", ".join(missing),
        )
        sys.exit(1)

    return Config(host=host, user=user, password=password)


def main() -> None:
    """Orchestrate ACI connectivity check or full factory reset."""
    args = _parse_args()
    logging.getLogger().setLevel(getattr(logging, args.log_level))

    cfg = _load_config()

    if args.check:
        run_check(cfg)
        return

    logger.warning(
        "DESTRUCTIVE OPERATION: all ACI devices will be reset to"
        " factory defaults. This cannot be undone."
    )
    try:
        confirmation = input('Type "ERASE" to confirm: ')
    except (KeyboardInterrupt, EOFError):
        print()
        logger.info("Aborted.")
        sys.exit(0)
    if confirmation != "ERASE":
        logger.info("Aborted.")
        sys.exit(0)
    logger.info("=== ACI Cleanup started (APIC: %s) ===", cfg.host)

    csv_nodes = load_switches(cfg.csv_path)
    api_nodes: SwitchMap = {}
    try:
        api_nodes = fetch_apic_nodes(cfg.host, cfg.user, cfg.password)
    except requests.RequestException as exc:
        logger.warning(
            "Cannot reach APIC REST API (%s) — falling back to CSV only", exc
        )

    try:
        apic_ssh_cleanup(cfg)
    except (paramiko.SSHException, TimeoutError, OSError):
        logger.error("APIC SSH cleanup failed, skipping")
    try:
        switch_ssh_cleanup(api_nodes, csv_nodes, cfg)
    except RuntimeError as exc:
        logger.error("%s", exc)
        sys.exit(1)

    logger.info("=== ACI Cleanup completed ===")


if __name__ == "__main__":
    main()
