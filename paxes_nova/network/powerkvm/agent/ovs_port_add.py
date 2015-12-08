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
from powervc_nova.network.powerkvm.agent.ovs_validation_mixin \
    import OVSValidator
from powervc_nova.network.powerkvm.agent import move_ip_address
from powervc_nova.objects.network import dom_kvm
from oslo.config import cfg

CONF = cfg.CONF
CONF.import_opt('integration_bridge', 'powervc_nova.network.powerkvm.agent')

LOG = logging.getLogger(__name__)


class OvsPortAdd(micro_op.MicroOperation,
                 OVSValidator):
    """
    Add a port to an OpenVSwitch.
    """

    def __init__(self, ovs_name, ovs_port_name, cmp_names):
        """
        Constructor.

        :param ovs_name: The name of the Open vSwitch that is having a port
                         added.
        :param ovs_port_name: The name of the OVS Port to add
        :param cmp_names: The names of the components of the OVS port.
        """
        self.ovs_name = ovs_name
        self.ovs_port_name = ovs_port_name
        self.cmp_names = cmp_names
        self.commandex = commandlet.CommandExecutor()

        self.current_dom = None

        # Each operation can spawn child operations.
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

            # check if the ovs specified is the integration bridge
            self.validate_is_not_integration_bridge(self.ovs_name)

            # check for existence of specified ovs
            self.validate_ovs_exists(self.ovs_name)

            # check if OVS port is already in use in dom

            self.validate_port_unassigned(self.ovs_port_name)

            # check if adapters are available for use
            self.validate_adapters_available(self.cmp_names)

            self.validate_adapters_appear_once(self.cmp_names,
                                               self.ovs_port_name)

        except Exception as exp:
            LOG.error(exp)
            raise

        # update dom to put ovs port in proper ovs port list
        new_port_dom = dom_kvm.OVSPort(self.ovs_port_name)
        ovs_dom = None
        for ovs in curr_dom.ovs_list:
            if ovs.name == self.ovs_name:
                ovs_dom = ovs
                ovs_dom.ovs_port_list.append(new_port_dom)
                break

        # Find the ports to add to it
        to_remove_pts = []
        for unused_pt in self.current_dom.unused_component_list:
            if unused_pt.name in self.cmp_names:
                new_port_dom.port_list.append(unused_pt)
                to_remove_pts.append(unused_pt)
        for remove_pt in to_remove_pts:
            self.current_dom.unused_component_list.remove(remove_pt)

        # We will need to validate the OVS Name.
        ovs_name = self.ovs_name
        ovs_op = ifcfg_builder.IfcfgFileOVSBuilder(ovs_name)
        self.sub_ops.micro_ops.append(ovs_op)

        # If this OVSPort is a bond, create the ifcfg files.
        is_bond = False
        if len(new_port_dom.port_list) > 1:
            is_bond = True
            ifcfg_op = ifcfg_builder.IfCfgBondFileBuilder(
                ovs_name,
                new_port_dom.name,
                [port.name for port in new_port_dom.port_list])
            self.sub_ops.micro_ops.append(ifcfg_op)

        # If the port we are moving has an IP Address, then we should also
        # move the IP Address of the child object.  We need to throw a warning
        # though if the owning vSwitch will already have an IP Address.
        ip_adapter = None
        for added_adpt in new_port_dom.port_list:
            from_dev = added_adpt.name
            to_dev = ovs_dom.name

            # Check to see if this new port has an ip address
            if added_adpt.ip_addresses is not None and\
                    len(added_adpt.ip_addresses) > 0:
                # If another adapter already had an IP Addresses, throw an
                # error.
                # If the vSwitch already has an IP Address, throw an error
                if ovs_dom.ip_addresses is not None and\
                        len(ovs_dom.ip_addresses) > 0:
                    raise exception.IPAddressAdd(dev=added_adpt.name,
                                                 ovs_name=ovs_name)

                if ip_adapter is not None:
                    raise exception.IPAddressAddMultiPort(dev=added_adpt.name,
                                                          ovs_name=ovs_name)

                # At this point, we can safely set the move ip address
                op = move_ip_address.MoveIpAddress(from_dev, to_dev, is_bond)
                self.sub_ops.micro_ops.append(op)
                ip_adapter = added_adpt
            else:
                # We didn't add an IP Address, but we do need to modify the
                # ifcfg file to ensure that network restarts work properly
                op = ifcfg_builder.IfcfgFileCmpBuilder(from_dev,
                                                       to_dev,
                                                       is_bond)
                self.sub_ops.micro_ops.append(op)

        # currently no warnings (just errors) returned by this micro op.
        # However, the children may have them, so call the validate on the
        # subops and return that data.
        return self.sub_ops.validate(self.current_dom)

    def execute(self):
        """
        Overrides parent method.

        Execute a command line call to create an OVS port.
        """
        stderr = None
        stdout = None
        try:
            if len(self.cmp_names) == 1:
                # For just a single component, we use add-port.
                # The OVS port name should be the same as the component name.
                stdout, stderr = utils.execute('ovs-vsctl', 'add-port',
                                               self.ovs_name,
                                               self.cmp_names[0],
                                               run_as_root=True)
            elif len(self.cmp_names) > 1:
                # For multiple components, we use add-bond.
                # The OVS port name is arbitrarily selected as the first
                # component in the list.
                stdout, stderr = utils.execute('ovs-vsctl', 'add-bond',
                                               self.ovs_name,
                                               self.ovs_port_name,
                                               *self.cmp_names,
                                               run_as_root=True)

            if stderr and stderr != '':
                raise exception.IBMPowerKVMCommandExecError(cmd='ovs-vsctl',
                                                            exp=stderr)

            # Run the sub operations now
            self.sub_ops.execute()
        except Exception as e:
            LOG.error(e)
            raise
        LOG.debug('ovs-vsctl add-br output = %s' % stdout)

    def undo(self):
        """
        Overrides parent method.

        Execute a command line call to remove an OVS port.
        """
        stderr = None
        stdout = None
        try:
            # The OVS port name should be the same as the component name.
            # The 0 in the next line is very arbitrary but must match the
            # corresponding line in the execute() method.
            stdout, stderr = utils.execute('ovs-vsctl', 'del-port',
                                           self.ovs_name,
                                           self.ovs_port_name,
                                           run_as_root=True)

            # Undo the sub ops if needed
            self.sub_ops.undo()

            if stderr and stderr != '':
                raise exception.IBMPowerKVMCommandExecError(cmd='ovs-vsctl',
                                                            exp=stderr)
        except Exception as e:
            LOG.error(e)
            raise
        LOG.debug('ovs-vsctl del-br output = %s' % stdout)
