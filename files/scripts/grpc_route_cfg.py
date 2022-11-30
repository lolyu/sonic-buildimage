#!/usr/bin/env python3
import argparse
import ipaddress
import os
import subprocess

from sonic_py_common import logger as log
from swsscommon import swsscommon


logger = log.Logger("grpc_route_cfg")


def run_command(cmd, check=True):
    """Run a command."""
    logger.log_notice(f"COMMAND: {cmd}")
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=True,
    )
    result.stdout = result.stdout.decode()
    result.stderr = result.stderr.decode()
    logger.log_notice(f"COMMAND RETURNCODE: {result.returncode}")
    logger.log_debug(f"COMMAND STDOUT: {result.stdout}")
    logger.log_debug(f"COMMAND STDERR: {result.stderr}")
    return result


def remove_rt_table(table_name, table_number):
    """Remove route table from the system."""
    cmd = f'sed -i "/^{table_number}\s\+{table_name}\s*$/d" /etc/iproute2/rt_tables'
    run_command(cmd)


def add_rt_table(table_name, table_number):
    """Add route table to the system."""
    remove_rt_table(table_name, table_number)

    cmd = f'printf "{table_number}\t{table_name}\n" >> /etc/iproute2/rt_tables'
    run_command(cmd)


class IPCommand(object):
    """IP related commands."""

    IP_ROUTE_ADD_CMD = "ip route add {subnet} dev {device} table {table_name}"
    IP_ROUTE_DEL_CMD = "ip route del {subnet} dev {device} table {table_name}"
    IP_ROUTE_FLUSH_CMD = "ip route flush table {table_name}"
    IP_RULE_ADD_CMD = "ip rule add pref {pref_number} from {source} fwmark {fwmark} table {table_name}"
    IP_RULE_DEL_CMD = "ip rule del pref {pref_number} from {source} fwmark {fwmark} table {table_name}"

    @staticmethod
    def ip_route_add(subnet, device, table_name):
        return run_command(IPCommand.IP_ROUTE_ADD_CMD.format(subnet=subnet, device=device, table_name=table_name))

    @staticmethod
    def ip_route_del(subnet, device, table_name):
        return run_command(IPCommand.IP_ROUTE_DEL_CMD.format(subnet=subnet, device=device, table_name=table_name))

    @staticmethod
    def ip_route_flush(table_name):
        return run_command(IPCommand.IP_ROUTE_FLUSH_CMD.format(table_name=table_name))

    @staticmethod
    def ip_rule_add(pref_number, source, fwmark, table_name):
        return run_command(IPCommand.IP_RULE_ADD_CMD.format(pref_number=pref_number, source=source, fwmark=fwmark, table_name=table_name))

    @staticmethod
    def ip_rule_del(pref_number, source, fwmark, table_name):
        return run_command(IPCommand.IP_RULE_DEL_CMD.format(pref_number=pref_number, source=source, fwmark=fwmark, table_name=table_name))


class GRPCRouteMgr(object):
    GRPC_TRAFFIC_TRAFFIC_FWMARK = 50075
    GRPC_TRAFFIC_RULE_PREF = 32763
    RT_TABLE_NAME = "grpc"
    RT_TABLE_NUMBER = 11

    def __init__(self):
        self.config_db = swsscommon.ConfigDBConnector(use_unix_socket_path=True)
        self.config_db.connect()

        self.device_meta = self.config_db.get_table(swsscommon.CFG_DEVICE_METADATA_TABLE_NAME)
        self.mux_cables = self.config_db.get_table(swsscommon.CFG_MUX_CABLE_TABLE_NAME)

        self.vlan_interfaces_ipv4 = self._get_ipv4_vlan_interfaces()

    def _get_ipv4_vlan_interfaces(self):
        """Get vlan interfaces with ipv4 address."""
        vlan_interfaces_ipv4 = {}
        for vlan_intf_key in self.config_db.get_table(swsscommon.CFG_VLAN_INTF_TABLE_NAME).keys():
            if len(vlan_intf_key) == 2:
                vlan_intf, vlan_subnet = vlan_intf_key
                if ":" in vlan_subnet:
                    continue
                vlan_subnet = ipaddress.ip_interface(vlan_subnet).network.with_prefixlen
                vlan_interfaces_ipv4[vlan_intf] = vlan_subnet
        return vlan_interfaces_ipv4

    def _is_active_active_dualtor(self):
        """Check if the device is deployed as an active-active dualtor T0."""
        if "localhost" not in self.device_meta or self.device_meta["localhost"].get("subtype") != "DualToR":
            return False

        for mux_config in self.mux_cables.values():
            if mux_config.get("cable_type") == "active-active":
                return True
        return False

    def setup_route(self):
        """Setup the route"""
        if not self._is_active_active_dualtor():
            logger.log_warning("Skip applying gRPC route configs on non active-active dualtor")
            return

        # NOTE: Let the gRPC traffic that has fwmark as 50075 to use a dedicate route table
        # that has routes to forward gRPC traffic directly to the vlan interfaces.
        logger.log_warning("Apply gRPC route configs on active-active dualtor")
        add_rt_table(GRPCRouteMgr.RT_TABLE_NAME, GRPCRouteMgr.RT_TABLE_NUMBER)
        IPCommand.ip_rule_add(GRPCRouteMgr.GRPC_TRAFFIC_RULE_PREF, "all", GRPCRouteMgr.GRPC_TRAFFIC_TRAFFIC_FWMARK, GRPCRouteMgr.RT_TABLE_NAME)
        for vlan_intf, vlan_subnet in self.vlan_interfaces_ipv4.items():
            IPCommand.ip_route_add(vlan_subnet, vlan_intf, GRPCRouteMgr.RT_TABLE_NAME)

    def teardown_route(self):
        """Teardown the route setup."""
        if not self._is_active_active_dualtor():
            logger.log_warning("Skip removing gRPC route configs on non active-active dualtor")
            return

        logger.log_warning("Remove gRPC route configs on active-active dualtor")
        IPCommand.ip_route_flush(GRPCRouteMgr.RT_TABLE_NAME)
        IPCommand.ip_rule_del(GRPCRouteMgr.GRPC_TRAFFIC_RULE_PREF, "all", GRPCRouteMgr.GRPC_TRAFFIC_TRAFFIC_FWMARK, GRPCRouteMgr.RT_TABLE_NAME)
        remove_rt_table(GRPCRouteMgr.RT_TABLE_NAME, GRPCRouteMgr.RT_TABLE_NUMBER)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Config the route for gRPC traffic."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "-s",
        "--setup",
        action="store_true",
        help="setup the route environement for gRPC traffic"
    )
    group.add_argument(
        "-t",
        "--teardown",
        action="store_true",
        help="teardown the route environement for gRPC traffic"
    )
    return parser, parser.parse_args()


def check_permission():
    return os.geteuid() == 0


if __name__ == "__main__":
    parser, args = parse_args()

    if not check_permission():
        parser.exit("Operation not permitted")

    grpc_rt_mgr = GRPCRouteMgr()
    if args.setup:
        grpc_rt_mgr.setup_route()
    elif args.teardown:
        grpc_rt_mgr.teardown_route()
