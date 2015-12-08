#
# =================================================================
# =================================================================

import copy
from nova.openstack.common import log as logging
from powervc_nova.network.powerkvm import agent
from powervc_nova.network.powerkvm.agent import commandlet
from powervc_nova.network.powerkvm.agent.common import exception
from powervc_nova.network.powerkvm.agent.common import micro_op
from powervc_nova.network.powerkvm.agent import move_ip_address

LOG = logging.getLogger(__name__)


class IfcfgFileCmpBuilder(micro_op.MicroOperation):
    """
    This class is invoked as a sub micro operation.  Its purpose is to inspect
    the ifcfg files of ports with OUT IP Addresses and ensure that they are
    properly wired to the Open vSwitch (or removed from being wired to the
    Open vSwitch).

    Note that this component builder is only meant for ports within a given
    OVS, not the OVS itself.
    """

    def __init__(self, device_name, ovs_name=None, is_bond=False):
        """
        Constructor.

        :param device: The name of the device to validate
        :param ovs_name: The name of the Open vSwitch that this device should
                         be paired to.  If set to None, indicates that this
                         device is no longer part of a vSwitch.  If it is set,
                         indicates that it is part of a vSwitch.
        :param is_bond: True if we're building an ifcfg file for an OVS bond,
                        False otherwise.
        """
        self.device_name = device_name
        self.ovs_name = ovs_name
        self.commandex = commandlet.CommandExecutor()
        self.source_dict_restore = None
        self.is_bond = is_bond

    def validate(self, current_dom):
        """
        Overrides parent method.

        Validate that the operation can be run.
        """
        # In order to determine the target type, we do need to investigate
        # the DOM to find the appropriate DOM object for this device.
        self.device_dom = current_dom.find_port_or_ovs(self.device_name)

        # The dom itself doesn't change here.
        return current_dom, []

    def execute(self):
        """
        Overrides parent method.

        Execute a command line call to create an OVS port.
        """
        LOG.debug("In the execute of IfcfgFileCmpBuilder")

        # Read the devices ifcfg file
        self.source_dict = self.commandex.get_ifcfg(self.device_name)[1]

        # If the source file is None, then we need to add some of the default
        # attributes to it.
        #
        # This should really only happen in the case where we are adding a port
        # to a vSwitch for the first time, and it doesn't have an IP Address.
        if len(self.source_dict) == 0:
            self.source_dict[agent.IFCFG_DEVICE] = self.device_name

        # the vswitch becomes unusable if any attached items have
        # ONBOOT set to no, so make sure ONBOOT is set to yes
        self.source_dict[agent.IFCFG_ONBOOT] = 'yes'

        # Create a duplicated copy, that will be the restore version.
        self.source_dict_restore = copy.copy(self.source_dict)

        # Make sure that this isn't marked as Network Manager controlled
        if agent.IFCFG_NM_CONTROLLED in self.source_dict and\
                'yes' in self.source_dict[agent.IFCFG_NM_CONTROLLED].lower():
            LOG.debug("Found NM Key as : %s", agent.IFCFG_NM_CONTROLLED)
            raise exception.IBMPowerKVMNMControlled()

        # If we are part of a vSwitch...
        if self.ovs_name is not None:
            # Then we need to set parameters in the device.
            if self.is_bond:
                # If we're part of a bond..its probably 'Ethernet', but could
                # be a bonded NIC.  So only add the type if not a bond
                dtype = move_ip_address.determine_object_type(self.device_dom)
                if dtype != move_ip_address.ETH_BOND:
                    self.source_dict[agent.IFCFG_TYPE] = dtype

                # Should delete these attributes if they exist, but we're
                # becoming a bonded adapter.
                if agent.IFCFG_DEV_TYPE in self.source_dict:
                    del self.source_dict[agent.IFCFG_DEV_TYPE]
                if agent.IFCFG_OVS_BRIDGE in self.source_dict:
                    del self.source_dict[agent.IFCFG_OVS_BRIDGE]
                if agent.IFCFG_TYPE in self.source_dict:
                    del self.source_dict[agent.IFCFG_TYPE]
            else:
                self.source_dict[agent.IFCFG_DEV_TYPE] = 'ovs'
                self.source_dict[agent.IFCFG_OVS_BRIDGE] = self.ovs_name
                self.source_dict[agent.IFCFG_TYPE] = 'OVSPort'
        else:
            # This means that we are removing (or just verifying that it was
            # already removed) a port from a vSwitch.  So we need to revert
            # some attributes.
            if agent.IFCFG_DEV_TYPE in self.source_dict:
                del self.source_dict[agent.IFCFG_DEV_TYPE]
            if agent.IFCFG_OVS_BRIDGE in self.source_dict:
                del self.source_dict[agent.IFCFG_OVS_BRIDGE]

            # Determining the type is a bit more of a pain.
            dev_type = move_ip_address.determine_object_type(self.device_dom)
            self.source_dict[agent.IFCFG_TYPE] = dev_type

            # If the type is a bond...need to remove it as these are not
            # represented in the dictionary
            if dev_type == move_ip_address.ETH_BOND:
                if agent.IFCFG_TYPE in self.source_dict:
                    del self.source_dict[agent.IFCFG_TYPE]

        # Write the updated device config back to disk
        LOG.debug("Going to send the following for the device: %s",
                  self.source_dict)
        self.commandex.send_ifcfg(self.device_name, self.source_dict)
        LOG.debug("Execute from IfcfgFileCmpBuilder was a success")

    def undo(self):
        """
        Overrides parent method.

        Execute a command line call to remove an OVS port.
        """
        LOG.debug("Roll back invoked for IfcfgFileCmpBuilder.  Rolling back "
                  "device %s", self.device_name)
        if self.source_dict_restore is not None:
            self.commandex.send_ifcfg(self.device_name,
                                      self.source_dict_restore)
        LOG.debug("Roll back completed for device %s", self.device_name)


class IfcfgFileOVSBuilder(micro_op.MicroOperation):
    """
    This class  is invoked as a sub micro operation.  Its purpose is to inspect
    the ifcfg files of Open VSwitch and ensure that their properties are
    correct.
    """

    def __init__(self, ovs_name):
        """
        Constructor.

        :param ovs_name: The name of the Open vSwitch that is being checked.
        """
        self.ovs_name = ovs_name
        self.commandex = commandlet.CommandExecutor()
        self.source_dict_restore = None

    def validate(self, current_dom):
        """
        Overrides parent method.

        Validate that the operation can be run.
        """
        # In order to determine the target type, we do need to investigate
        # the DOM to find the appropriate DOM object for this device.
        self.ovs_dom = current_dom.find_port_or_ovs(self.ovs_name)

        # The dom itself doesn't change here.
        return current_dom, []

    def execute(self):
        """
        Overrides parent method.

        Updates the Open vSwitch ifcfg file
        """
        LOG.debug("In the execute of IfcfgFileOVSBuilder")

        # Read the devices ifcfg file
        self.source_dict = self.commandex.get_ifcfg(self.ovs_name)[1]

        # If this is the first time this has been run, we may need to
        # initially configure the file
        if len(self.source_dict) == 0:
            self.source_dict[agent.IFCFG_DEVICE] = self.ovs_name
            self.source_dict[agent.IFCFG_TYPE] = move_ip_address.OVS_BR
            self.source_dict[agent.IFCFG_DEV_TYPE] = 'ovs'

        # Save the contents for undo
        self.source_dict_restore = self.source_dict

        # the vswitch becomes unusable if any attached items have
        # ONBOOT set to no, so make sure ONBOOT is set to yes
        self.source_dict[agent.IFCFG_ONBOOT] = 'yes'

        # Write the updated device config back to disk
        LOG.debug("Going to send the following for the device: %s",
                  self.source_dict)
        self.commandex.send_ifcfg(self.ovs_name, self.source_dict)
        LOG.debug("Execute from IfcfgFileOVSBuilder was a success")

    def undo(self):
        """
        Overrides parent method.

        Execute a command line call to remove an OVS port.
        """
        LOG.debug("Roll back invoked for IfcfgFileOVSBuilder.  Rolling back "
                  "device %s", self.ovs_name)
        if self.source_dict_restore is not None:
            self.commandex.send_ifcfg(self.ovs_name, self.source_dict_restore)
        LOG.debug("Roll back completed for device %s", self.ovs_name)


class IfCfgBondFileBuilder(micro_op.MicroOperation):
    """
    This class is invoked as a sub micro operation.  Its purpose is to generate
    ifcfg files for OVS bonds and account for the type of bonding being used.
    """

    def __init__(self, ovs_name, ovs_port_name, bond_cmp_names,
                 remove_on_undo=False):
        """
        :param ovs_name: The name of the Open vSwitch that is being checked.
        """
        self.ovs_name = ovs_name
        self.ovs_port_name = ovs_port_name
        self.bond_cmp_names = bond_cmp_names
        self.remove_on_undo = remove_on_undo
        self.commandex = commandlet.CommandExecutor()

    def validate(self, current_dom):
        """
        Overrides parent method.

        Validate that the operation can be run.
        """
        # No validation for this operation
        return current_dom, []

    def execute(self):
        """
        Overrides parent method.

        Updates the OVSPort bond ifcfg files.
        """
        LOG.debug("In the execute of IfCfgBondFileBuilder")

        # Read the OVSPort ifcfg file and save the contents for undo
        self.fnamepath, self.source_ovs_port_restore = \
            self.commandex.get_ifcfg(self.ovs_port_name)

        # Create new configuration for OVSPort bond, if the bond has children
        ovs_port_dict = {}
        if len(self.bond_cmp_names):
            ovs_port_dict[agent.IFCFG_DEVICE] = self.ovs_port_name
            ovs_port_dict[agent.IFCFG_ONBOOT] = 'yes'
            ovs_port_dict[agent.IFCFG_DEV_TYPE] = 'ovs'
            ovs_port_dict[agent.IFCFG_TYPE] = move_ip_address.OVS_BOND
            ovs_port_dict[agent.IFCFG_OVS_BRIDGE] = self.ovs_name
            ovs_port_dict[agent.IFCFG_BOOTPROTO] = 'none'
            ovs_port_dict[agent.IFCFG_BOND_IFACES] = '"%s"' % (
                ' '.join(self.bond_cmp_names))
            ovs_port_dict[agent.IFCFG_OVS_OPTIONS] = '"bond_mode=balance-slb"'
            ovs_port_dict[agent.IFCFG_HOTPLUG] = 'no'

            # Send OVSPort ifcfg file back to disk
            LOG.debug('Sending ifcfg for device: %s', ovs_port_dict)
            name = self.commandex.send_ifcfg(self.ovs_port_name, ovs_port_dict)

            # Store the pathname that was used in case we need to undo
            self.fnamepath = name

        elif self.fnamepath and len(self.fnamepath):
            # The bond has no children, so the bond is being removed
            LOG.debug('Removing ifcfg: %s', self.fnamepath)
            commandlet.remove_file_as_root(self.fnamepath)

        LOG.debug("Execute from IfCfgBondFileBuilder was a success")

    def undo(self):
        """
        Overrides parent method.

        Roll back a failed transaction.
        """
        LOG.debug('Roll back invoked for IfcfgFileOVSBuilder.  Rolling back '
                  'device %s', self.ovs_port_name)

        # Roll back OVS port
        if self.remove_on_undo:
            # Prevent empty file from hanging around on disk
            LOG.debug('Removing ifcfg: %s', self.fnamepath)
            commandlet.remove_file_as_root(self.fnamepath)

        elif(self.source_ovs_port_restore is not None and
             len(self.source_ovs_port_restore)):
            self.commandex.send_ifcfg(self.ovs_port_name,
                                      self.source_ovs_port_restore)

        LOG.debug('Roll back completed for device %s', self.ovs_port_name)
