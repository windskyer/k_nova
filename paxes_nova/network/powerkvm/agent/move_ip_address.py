#
# =================================================================
# =================================================================

import copy
from nova.openstack.common import log as logging
from powervc_nova.network.powerkvm import agent
from powervc_nova.network.powerkvm.agent import commandlet
from powervc_nova.network.powerkvm.agent.common import exception
from powervc_nova.network.powerkvm.agent.common import warning
from powervc_nova.network.powerkvm.agent.common import micro_op
from powervc_nova.objects.network import dom_kvm

LOG = logging.getLogger(__name__)

ETHERNET = 'Ethernet'
BRIDGE = 'Bridge'
OVS_BR = 'OVSBridge'
ETH_BOND = 'EthernetBond'
OVS_PORT = 'OVSPort'
OVS_BOND = 'OVSBond'


def determine_object_type(obj):
    """
    This method determines the type of the source and target objects.
    The following could be the types: Ethernet, EthernetBond, LinuxBridge,
    OpenVSwitch. The OVSPort can be of either Ethernet, EthernetBond or
    LinuxBridge

    :param type_obj: The object who's type has to be determined.  Must be a
                     DOM object.
    :return type: The type string of the object.  This may be used for a
                  ifcfg file.
    """
    type_obj = "Ethernet"
    if isinstance(obj, dom_kvm.LinuxBridge):
        type_obj = BRIDGE
    elif isinstance(obj, dom_kvm.EthernetBond):
        type_obj = ETH_BOND
    elif isinstance(obj, dom_kvm.PhysicalPort):
        type_obj = ETHERNET
    elif isinstance(obj, dom_kvm.OpenVSwitch):
        type_obj = OVS_BR
    return type_obj


class MoveIpAddress(micro_op.MicroOperation):
    """
    When a device with an IP address is added to an OpenVSwitch port, the IP
    address needs to be moved to the vswitch.  This class handles reconfiguring
    the host networking to support that.
    """

    def __init__(self, source_device, target_device, is_bond=False):
        """
        :param source_device: The source device that may have IP config.
        :param target_device: The target device to move IP config to.
        """
        self.source_dev = source_device
        self.target_dev = target_device
        self.source_dict = None
        self.target_dict = None
        self.source_dict_restore = None
        self.target_dict_restore = None
        self.device_type_source = None
        self.device_type_target = None
        self.source_type = None
        self.target_type = None
        self.commandex = commandlet.CommandExecutor()
        self.no_op = False
        self.is_bond = is_bond
        LOG.debug("The MoveIpAddress() class has  been initialized ")

    def validate(self, curr_dom):
        """
        Overrides parent method.

        Determine if any errors are likely to occur when moving the IP address
        from source_device to target_device.  The source device not having IP
        config is not considered an error.
        :param curr_dom: The current dom object representation of the system.
        """
        LOG.debug("In the MoveIpAddress() validate()")

        if not self.source_dev or not self.target_dev:
            return

        # Look up the objects in the DOM
        source_obj = curr_dom.find_port_or_ovs(self.source_dev)
        target_obj = curr_dom.find_port_or_ovs(self.target_dev)

        # Verify that an IP address exists on the source device
        if len(source_obj.ip_addresses) == 0:
            # Source has no IP, this is not an error but there's nothing to do
            self.no_op = True
            return curr_dom, []

        # Verify that an IP address does not exist on the target device
        if len(target_obj.ip_addresses) != 0:
            LOG.debug('Target already has IP config, raising exception.')
            raise exception.IBMPowerKVMTargetIpConfig(src=self.source_dev,
                                                      tgt=self.target_dev)

        # Verify that we're not moving from one vswitch to another
        self.source_type = determine_object_type(source_obj)
        self.target_type = determine_object_type(target_obj)
        if self.source_type == OVS_BR and self.target_type == OVS_BR:
            raise exception.IBMPowerKVMSourceTargetOVS(src=self.source_dev,
                                                       tgt=self.target_dev)

        # Move the IP on the validation DOM
        target_obj.ip_addresses = [source_obj.ip_addresses[0]]
        source_obj.ip_addresses = []

        warning_list = []

        # Read source device config
        source_dict = self.commandex.get_ifcfg(self.source_dev)[1]

        if source_dict and \
                len(source_dict) != 0 and \
                source_dict.get(agent.IFCFG_BOOTPROTO, '').upper() == 'DHCP':
            warning_list.append(
                warning.OVSMovingDHCPAddressWarning(
                    adapter_name=self.source_dev,
                    target_dev=self.target_dev))

        LOG.debug("The modified DOM returned from the MoveIpAddress() is: %s",
                  curr_dom)
        return curr_dom, warning_list

    def execute(self):
        """
        Overrides parent method.

        Move IP from source_device to target_device.
        """
        LOG.debug("In the execute of MoveIPAddress")

        # validate detected no ip address to move, so
        # exit
        if self.no_op:
            return

        if not self.source_dev or not self.target_dev:
            return

        # Read source device config
        source_dict = self.commandex.get_ifcfg(self.source_dev)[1]
        self.source_dict_restore = copy.copy(source_dict)
        self.source_dict = source_dict

        if source_dict is None or len(source_dict) == 0:
            # This means the file wasn't found for the source dev.
            src_eth = self.source_dev
            raise exception.IBMPowerKVMSourceDevFileDoesNotExit(eth=src_eth)

        nm_key = agent.IFCFG_NM_CONTROLLED
        LOG.debug("Found NM Key as : %s", nm_key)
        if nm_key in source_dict and 'yes' in source_dict[nm_key].lower():
            raise exception.IBMPowerKVMNMControlled()

        # Read the target device config. We want to be prepared for the undo
        target_dict = self.commandex.get_ifcfg(self.target_dev)[1]
        self.target_dict_restore = copy.copy(target_dict)
        self.target_dict = target_dict
        # Move the IP config from source device to target device
        self._move_ip_attributes(self.source_dict, self.target_dict)

        # Adjust target device properties
        self.target_dict[agent.IFCFG_DEVICE] = self.target_dev

        # the vswitch becomes unusable if any attached items have
        # ONBOOT set to no, so make sure ONBOOT is set to yes
        self.source_dict[agent.IFCFG_ONBOOT] = 'yes'
        self.target_dict[agent.IFCFG_ONBOOT] = 'yes'

        # If the target device doesn't have a "TYPE" key, that means it's
        # a OVSBridge, otherwise, it always should have a TYPE of Ethernet
        # or Bridge - which we retain.
        if self.target_type == OVS_BR:
            self.target_dict[agent.IFCFG_TYPE] = OVS_BR
            self.target_dict[agent.IFCFG_DEV_TYPE] = 'ovs'
            self.target_dict[agent.IFCFG_HOTPLUG] = 'no'

            # Now populate the source, but we only need these if we're not
            # coming from a bridge or are part of a bond.
            if self.source_type is not BRIDGE and not self.is_bond:
                self.source_dict[agent.IFCFG_TYPE] = OVS_PORT
                self.source_dict[agent.IFCFG_DEV_TYPE] = 'ovs'
                self.source_dict[agent.IFCFG_OVS_BRIDGE] =\
                    self.target_dict[agent.IFCFG_DEVICE]
        elif self.source_type == OVS_BR:
            if agent.IFCFG_OVS_BRIDGE in self.target_dict:
                del self.target_dict[agent.IFCFG_OVS_BRIDGE]
            if agent.IFCFG_DEV_TYPE in self.target_dict:
                del self.target_dict[agent.IFCFG_DEV_TYPE]
            if self.target_type == BRIDGE:
                self.target_dict[agent.IFCFG_TYPE] = BRIDGE
            elif self.target_type == ETH_BOND:
                # Ethernet bonds do not specify an ifcfg type
                if agent.IFCFG_TYPE in self.target_dict:
                    del self.target_dict[agent.IFCFG_TYPE]
            elif self.target_type == ETHERNET:
                self.target_dict[agent.IFCFG_TYPE] = ETHERNET
        else:
            raise exception.IBMPowerKVMUnsupportedRequest()

        # Write the updated device config back to disk
        LOG.debug("Going to send the following for the source device: %s",
                  self.source_dict)
        LOG.debug("Going to send the following target dict: %s",
                  self.target_dict)
        self.commandex.send_ifcfg(self.source_dev, self.source_dict)
        self.commandex.send_ifcfg(self.target_dev, self.target_dict)
        LOG.debug("Execute from MoveIPAddress was a succes")

    def undo(self):
        """
        Overrides parent method.

        Rollback the IP move.
        """
        LOG.debug("In the undo method, will attempt to restore")

        # validate detected nothing to do for this, nothing was done
        # for execute, so simply return
        if self.no_op:
            return

        if not self.source_dev or not self.target_dev:
            return
        LOG.debug("The source dictionary is: %s", self.source_dict_restore)
        LOG.debug("The target dictionary is: %s", self.target_dict_restore)

        # In scenario where no source IP Address...
        if self.source_dict_restore:
            self.commandex.send_ifcfg(self.source_dev,
                                      self.source_dict_restore)

        # May have failed because the ifcfg didn't even exist, nothing
        # to roll back then
        if self.target_dict_restore:
            self.commandex.send_ifcfg(self.target_dev,
                                      self.target_dict_restore)

    def _has_ip_config(self, device_dict):
        """
        Check to see if the specified dictionary has IP config.

        :param device_dict: The dictionary to inspect.
        :returns: True if ip config is present, False otherwise.
        """
        keys_that_indicate_ip_config = [agent.IFCFG_IPADDR,
                                        agent.IFCFG_IPV6ADDR,
                                        agent.IFCFG_DHCP_HOSTNAME,
                                        agent.IFCFG_DHCPV6C,
                                        agent.IFCFG_DHCPV6C_OPTIONS,
                                        agent.IFCFG_DHCP_HOSTNAME,
                                        ]
        for key in keys_that_indicate_ip_config:
            if key in device_dict and device_dict[key]:
                return True
        return False

    def _move_ip_attributes(self, source_dict, target_dict):
        """
        Move the IP specific attributes from the source dict to the target
        dict.

        :param source_dict: The source device dictionary config.
        :param target_dict: The target device dictionary config.
        """
        # Make a list of keys that comprise the Level 3 config and need to move
        keys_to_move = [agent.IFCFG_BOOTPROTO,
                        agent.IFCFG_DHCP_HOSTNAME,
                        agent.IFCFG_DHCPV6C,
                        agent.IFCFG_DHCPV6C_OPTIONS,
                        agent.IFCFG_DNS1,
                        agent.IFCFG_DNS2,
                        agent.IFCFG_ETHTOOL_OPTIONS,
                        agent.IFCFG_GATEWAY,
                        agent.IFCFG_IPADDR,
                        agent.IFCFG_IPV6ADDR,
                        agent.IFCFG_IPV6ADDR_SECONDARIES,
                        agent.IFCFG_IPV6INIT,
                        agent.IFCFG_IPV6_AUTOCONF,
                        agent.IFCFG_IPV6_MTU,
                        agent.IFCFG_IPV6_PRIVACY,
                        agent.IFCFG_MTU,
                        agent.IFCFG_NETMASK,
                        agent.IFCFG_NETWORK,
                        agent.IFCFG_ONBOOT,
                        agent.IFCFG_PEERDNS,
                        agent.IFCFG_REMOTE_IPADDR,
                        agent.IFCFG_SRCADDR]

        # Move each key
        for key in keys_to_move:
            if key in source_dict:
                target_dict[key] = source_dict[key]
                # Special case -- ONBOOT should be left in both files
                if key != agent.IFCFG_ONBOOT:
                    del source_dict[key]
