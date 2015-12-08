#
# =================================================================
# =================================================================

from nova import utils
from nova.openstack.common import log as logging
from powervc_nova.network.powerkvm.agent import commandlet
from powervc_nova.network.powerkvm.agent import multi_op
from powervc_nova.network.powerkvm.agent import ifcfg_builder
from powervc_nova.network.powerkvm.agent.common import exception
from powervc_nova.network.powerkvm.agent.common import micro_op
from powervc_nova.network.powerkvm.agent import move_ip_address
from powervc_nova.network.powerkvm.agent.ovs_validation_mixin \
    import OVSValidator
from oslo.config import cfg

LOG = logging.getLogger(__name__)

CONF = cfg.CONF
CONF.import_opt('integration_bridge', 'powervc_nova.network.powerkvm.agent')


class OvsPortRemove(micro_op.MicroOperation,
                    OVSValidator):
    """
    Remove a port from an OpenVSwitch.
    """

    def __init__(self, ovs_name, ovs_port_name):
        """
        :param ovs_name: The name of the vswitch to remove from, a string.
        :param ovs_port_name: The name of the Open vSwitch port to remove
        """
        self.ovs_name = ovs_name
        self.ovs_port_name = ovs_port_name
        self.commandex = commandlet.CommandExecutor()

        self.current_dom = None

        # Create a container for the sub operations
        self.sub_ops = multi_op.MultiMicroOp()

    def validate(self, curr_dom):
        """
        Overrides parent method.

        Validate

        """
        try:
            # Ensure the mixin has the correct DOM
            self.current_dom = curr_dom

            if self.current_dom is None:
                raise exception.IBMPowerKVMOVSNoValidDomSpecified

            """check for errors"""
            # check if the ovs specified is the integration bridge
            self.validate_is_not_integration_bridge(self.ovs_name)

            # check for existence of specified ovs
            self.validate_ovs_exists(self.ovs_name)

            # check is OVS port is assigned to the ovs
            self.validate_port_assigned(self.ovs_name,
                                        self.ovs_port_name)

        except Exception as exp:
            LOG.error(exp)
            raise

        # update dom to remove ovs port from the specified ovs
        # port list
        orig_ovs_port = None
        ovs_dom = None
        for ovs in curr_dom.ovs_list:
            if ovs.name == self.ovs_name:
                ovs_dom = ovs
                for ovs_port in ovs.ovs_port_list:
                    if ovs_port.name == self.ovs_port_name:
                        # Remove it from the OVS
                        ovs.ovs_port_list.remove(ovs_port)
                        orig_ovs_port = ovs_port
                        break

        # Store off the original component names (at this point in time) in
        # case of a roll back
        #
        # At same time, add each back to the unused list
        self.orig_cmp_names = []
        for comp in orig_ovs_port.port_list:
            self.orig_cmp_names.append(comp.name)
            curr_dom.unused_component_list.append(comp)

        # We need to update the OVS to ensure that the ifcfg is proper.
        op = ifcfg_builder.IfcfgFileOVSBuilder(self.ovs_name)
        self.sub_ops.micro_ops.append(op)

        # We must also look for the opportunity for a move IP Address.  This
        # will occur if this is the last port on a vswitch, and that vswitch
        # has an IP Address.  We will embed that in this micro op.
        if ovs_dom and ovs_dom.ip_addresses is not None and\
                len(ovs_dom.ip_addresses) >= 1 and\
                len(ovs_dom.ovs_port_list) == 0:

            # If there was more than one port on here originally, we can't
            # move the IP Address as we won't know which adapter to assign
            # that IP Address to.
            if len(self.orig_cmp_names) > 1:
                raise exception.IBMPowerKVMIPAddressDelete(
                    ovs_name=self.ovs_name)

            op = move_ip_address.MoveIpAddress(self.ovs_name,
                                               self.orig_cmp_names[0])
            self.sub_ops.micro_ops.append(op)
        else:
            # If this was an OVS bond, remove the bond ifcfg file
            is_bond = False
            if len(orig_ovs_port.port_list) > 1:
                is_bond = True
                op = ifcfg_builder.IfCfgBondFileBuilder(
                    ovs_name=None,
                    ovs_port_name=orig_ovs_port.name,
                    bond_cmp_names=[])
                self.sub_ops.micro_ops.append(op)

            # Need to run the ifcfg check on each sub element
            for component in orig_ovs_port.port_list:
                op = ifcfg_builder.IfcfgFileCmpBuilder(component.name,
                                                       ovs_name=None,
                                                       is_bond=is_bond)
                self.sub_ops.micro_ops.append(op)

        # currently no warnings (just errors) returned by this micro op
        # However, the sub ops may have validation issues, so run those child
        # ops.
        return self.sub_ops.validate(self.current_dom)

    def execute(self):
        """
        Overrides parent method.

        Execute a command line call to remove an OVS port.
        """
        try:
            # The OVS port name should be the same as the component name.
            # The OVS port name is arbitrarily selected as the first
            # component in the list.
            stdout, stderr = utils.execute('ovs-vsctl', 'del-port',
                                           self.ovs_name, self.ovs_port_name,
                                           run_as_root=True)
            if stderr and stderr != '':
                raise exception.IBMPowerKVMCommandExecError(cmd='ovs-vsctl',
                                                            exp=stderr)

            # Run the sub operations
            self.sub_ops.execute()
        except Exception as e:
            LOG.error(e)
            raise
        LOG.debug('ovs-vsctl del-br output = %s' % stdout)

    def undo(self):
        """
        Overrides parent method.

        Execute a command line call to create an OVS port.
        """
        try:
            stderr = ""
            if len(self.orig_cmp_names) == 1:
                # For just a single component, we use add-port.
                # The OVS port name should be the same as the component name.
                stdout, stderr = utils.execute('ovs-vsctl', 'add-port',
                                               self.ovs_name,
                                               self.orig_cmp_names[0],
                                               run_as_root=True)
            elif len(self.orig_cmp_names) > 1:
                # For multiple components, we use add-bond.
                # The OVS port name is arbitrarily selected as the first
                # component in the list.
                stdout, stderr = utils.execute('ovs-vsctl', 'add-bond',
                                               self.ovs_name,
                                               self.ovs_port_name,
                                               *self.orig_cmp_names,
                                               run_as_root=True)

            # Undo any sub operations
            self.sub_ops.undo()

            if stderr and stderr != '':
                raise exception.IBMPowerKVMCommandExecError(cmd='ovs-vsctl',
                                                            exp=stderr)
        except Exception as e:
            LOG.error(e)
            raise
        LOG.debug('ovs-vsctl add-br output = %s' % stdout)
