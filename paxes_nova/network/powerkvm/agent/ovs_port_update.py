#
# =================================================================
# =================================================================

from nova import utils
from nova.openstack.common import log as logging
from powervc_nova.network.powerkvm.agent import commandlet
from powervc_nova.network.powerkvm.agent import move_ip_address
from powervc_nova.network.powerkvm.agent import multi_op
from powervc_nova.network.powerkvm.agent import ifcfg_builder
from powervc_nova.network.powerkvm.agent.common import micro_op
from powervc_nova.network.powerkvm.agent.common import exception
from powervc_nova.network.powerkvm.agent.common import warning
from powervc_nova.network.powerkvm.agent.ovs_validation_mixin \
    import OVSValidator
from oslo.config import cfg

CONF = cfg.CONF
CONF.import_opt('integration_bridge', 'powervc_nova.network.powerkvm.agent')

LOG = logging.getLogger(__name__)

# These are used to track how far we have gotten into
# the execution of the update and when will need to be
# run is an undo is called
OLD_PORT_DELETE = 1
NEW_PORT_ADD = 2
UNDO_ALL = 99


class OvsPortUpdate(micro_op.MicroOperation,
                    OVSValidator):
    """
    Update a port on an OpenVSwitch.
    """

    def __init__(self, ovs_name, old_ovs_port_name, new_ovs_port_name,
                 new_cmp_names):
        """
        Constructor.

        :param ovs_name: The OpenVSwitch name that contains the port to update.
        :param old_ovs_port_name: The old name of the OVS Port to update
        :param new_ovs_port_name: The new name of the OVS Port to update
        :param new_cmp_names: A list of the new component names for the ovs
                              port that is being updated
        """
        self.ovs_name = ovs_name

        # Put this one in two places so the validator mixin finds it correctly
        self.ovs_port_name = old_ovs_port_name
        self.old_ovs_port_name = old_ovs_port_name

        self.new_ovs_port_name = new_ovs_port_name
        self.new_cmp_names = new_cmp_names
        self.commandex = commandlet.CommandExecutor()
        self.current_dom = None

        # need a way to determine where execute failed for undo purposes
        self.fail_location = UNDO_ALL

        # There are going to be several ifcfg file changes needed, so we build
        # a sub operations handler.
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

            """
            NOTE: A failed check for any of the validation steps
                  will result in an exception being raised and
                  execution immediately returning to the caller.
                  It is the caller's responsibility to process
                  the exception appropriately
            """

            # check if the ovs specified is the integration bridge
            self.validate_is_not_integration_bridge(self.ovs_name)

            # check for existence of specified ovs
            self.validate_ovs_exists(self.ovs_name)

            # check if old OVS port is assigned to the ovs
            self.validate_port_assigned(self.ovs_name, self.old_ovs_port_name)

            # check if new OVS port is already in use somewhere
            #self.validate_port_unassigned(self.old_ovs_port_name)

            # confirm at least one adapter in new port
            self.validate_adapter_list(self.new_cmp_names,
                                       self.old_ovs_port_name)

            # check if adapters for new port are available for use
            self.validate_adapters_available_update(self.new_cmp_names,
                                                    self.old_ovs_port_name)

            self.validate_adapters_appear_once(self.new_cmp_names,
                                               self.ovs_port_name)

        except Exception as exp:
            LOG.error(exp)
            raise

        # Find the DOM's port object, to update
        ovs_dom = curr_dom.contains_vswitch_port_by_name(
            self.old_ovs_port_name)
        ovs_port = ovs_dom.get_port(self.old_ovs_port_name)
        ovs_port_names_to_add = []
        ovs_port_objs_to_remove = []
        ovs_port_objs_to_add = []
        for cmp_port in ovs_port.port_list:
            if cmp_port.name not in self.new_cmp_names:
                ovs_port_objs_to_remove.append(cmp_port)
        for cmp_port_name in self.new_cmp_names:
            has_port = False
            for cmp_port in ovs_port.port_list:
                if cmp_port.name == cmp_port_name:
                    has_port = True
                    break
            if not has_port:
                ovs_port_names_to_add.append(cmp_port_name)
        for port_name_to_add in ovs_port_names_to_add:
            obj = curr_dom.find_port_or_ovs(port_name_to_add)
            ovs_port_objs_to_add.append(obj)

        # Save off some 'original' data (or original by the time it hit here)
        self.orig_port_length = len(ovs_port.port_list)
        self.orig_cmp_names = []
        for comp in ovs_port.port_list:
            self.orig_cmp_names.append(comp.name)

        # Update the OVSPort to the new name
        ovs_port.name = self.new_ovs_port_name

        # If there will be multiple ports after this update, update ifcfg file
        # for an OVS port bond.
        is_bond = False
        if len(self.new_cmp_names) > 1:
            is_bond = True
            remove_on_undo = self.new_ovs_port_name != self.old_ovs_port_name
            op = ifcfg_builder.IfCfgBondFileBuilder(
                ovs_name=self.ovs_name,
                ovs_port_name=self.new_ovs_port_name,
                bond_cmp_names=self.new_cmp_names,
                remove_on_undo=remove_on_undo)
            self.sub_ops.micro_ops.append(op)

            # If we were coming from a single adapter, but are now multi
            # adapter, we need to update the original adapter to reflect this
            if len(self.orig_cmp_names) == 1:
                op = ifcfg_builder.IfcfgFileCmpBuilder(self.orig_cmp_names[0],
                                                       ovs_name=self.ovs_name,
                                                       is_bond=is_bond)
                self.sub_ops.micro_ops.append(op)

        # If there were originally multiple ports and there will still be
        # multiple ports, but the bond name is changing, delete the old
        # bond ifcfg file
        if len(self.orig_cmp_names) > 1 and len(self.new_cmp_names) > 1:
            if self.old_ovs_port_name != self.new_ovs_port_name:
                op = ifcfg_builder.IfCfgBondFileBuilder(
                    ovs_name=self.ovs_name,
                    ovs_port_name=self.old_ovs_port_name,
                    bond_cmp_names=[])  # <- This empty list deletes ifcfg file
                self.sub_ops.micro_ops.append(op)

        # If there was originally multiple ports and there will be a single
        # port after this update, remove the ifcfg file.
        if len(self.orig_cmp_names) > 1 and len(self.new_cmp_names) == 1:
            is_bond = False
            op = ifcfg_builder.IfCfgBondFileBuilder(
                ovs_name=self.ovs_name,
                ovs_port_name=self.old_ovs_port_name,
                bond_cmp_names=[])  # <- This empty list deletes ifcfg file
            self.sub_ops.micro_ops.append(op)

            # We must also update the final components ifcfg file, if we
            # are coming from a bond and are no longer going to be a bond
            op = ifcfg_builder.IfcfgFileCmpBuilder(self.new_cmp_names[0],
                                                   ovs_name=self.ovs_name,
                                                   is_bond=is_bond)
            self.sub_ops.micro_ops.append(op)

        # We have detected which ports to add/remove.  Now do the corresponding
        # updates to the DOM.  The update guarantees us that there will be
        # at least one remaining port on the OVS Port (or else ovs_port_remove
        # would have been called.)  Therefore, no IP Address movement is
        # needed for the removal scenario
        for port_remove in ovs_port_objs_to_remove:
            ovs_port.port_list.remove(port_remove)
            curr_dom.unused_component_list.append(port_remove)

            # We also need to check the ifcfg file of each removed port
            # to make sure we're not still referencing the parent bridge
            op = ifcfg_builder.IfcfgFileCmpBuilder(port_remove.name,
                                                   ovs_name=None,
                                                   is_bond=is_bond)
            self.sub_ops.micro_ops.append(op)

        # Now we loop to add in ports.  However, we must check to see if a port
        # that is being added required a move of the IP Address.
        ip_adapter = None
        for port_add in ovs_port_objs_to_add:

            # Move the port in the DOM
            ovs_port.port_list.append(port_add)
            if port_add in curr_dom.unused_component_list:
                curr_dom.unused_component_list.remove(port_add)

            # Check the IP Addresses for moving
            if port_add.ip_addresses is not None and\
                    len(port_add.ip_addresses) > 0:
                # First check to make sure we do not have an IP Address on
                # the vSwitch already.
                if ovs_dom.ip_addresses is not None and\
                        len(ovs_dom.ip_addresses) > 0:
                    raise exception.IPAddressAdd(dev=port_add.name,
                                                 ovs_name=self.ovs_name)
                if ip_adapter is not None:
                    ov = self.ovs_name
                    raise exception.IPAddressAddMultiPort(dev=port_add.name,
                                                          ovs_name=ov)

                # General validations complete...set the move of the IP Address
                op = move_ip_address.MoveIpAddress(port_add.name,
                                                   self.ovs_name, is_bond)
                self.sub_ops.micro_ops.append(op)
                ip_adapter = port_add
            else:
                # The port may not have an IP Address, but we still need
                # to work against its ifcfg file.
                op = ifcfg_builder.IfcfgFileCmpBuilder(port_add.name,
                                                       self.ovs_name,
                                                       is_bond)
                self.sub_ops.micro_ops.append(op)

        # Save off the 'final' data
        self.final_port_length = len(ovs_port.port_list)
        self.final_cmp_names = []
        for comp in ovs_port.port_list:
            self.final_cmp_names.append(comp.name)

        # currently no warnings (just errors) returned by this micro op
        # However, the sub ops may have them so we'll just return those.
        return self.sub_ops.validate(self.current_dom)

    def execute(self):
        """
        Overrides parent method.

        Execute
        """
        try:
            # Remove old port
            LOG.debug('running ovs-vsctl del-port to remove old port')

            stdout, stderr = utils.execute('ovs-vsctl', 'del-port',
                                           self.ovs_name,
                                           self.old_ovs_port_name,
                                           run_as_root=True)
            if stderr and stderr != '':
                self.fail_location = OLD_PORT_DELETE
                raise exception.IBMPowerKVMCommandExecError(cmd='ovs-vsctl',
                                                            exp=stderr)

            LOG.debug('ovs-vsctl del-port output = %s' % stdout)

            # Add new port
            if self.final_port_length == 1:
                LOG.debug('running ovs-vsctl add-port to add new port')
                # For just a single component, we use add-port.
                stdout, stderr = utils.execute('ovs-vsctl', 'add-port',
                                               self.ovs_name,
                                               self.final_cmp_names[0],
                                               run_as_root=True)
                LOG.debug('ovs-vsctl add-port output = %s' % stdout)

            elif self.final_port_length > 1:
                LOG.debug('running ovs-vsctl add-bond to add new port')
                # For multiple components, we use add-bond.
                stdout, stderr = utils.execute('ovs-vsctl', 'add-bond',
                                               self.ovs_name,
                                               self.new_ovs_port_name,
                                               *self.final_cmp_names,
                                               run_as_root=True)
                LOG.debug('ovs-vsctl add-bond output = %s' % stdout)

            if stderr and stderr != '':
                self.fail_location = NEW_PORT_ADD
                raise exception.IBMPowerKVMCommandExecError(cmd='ovs-vsctl',
                                                            exp=stderr)

            # Finally run all the micro ops
            self.sub_ops.execute()

            # we've completed the full execute, so we need to indicate that,
            # if an undo is needed, we need to undo everything
            self.fail_location = UNDO_ALL
        except Exception as e:
            LOG.error(e)
            raise

    def undo(self):
        LOG.debug("Running undo of ovs port update execute")

        try:
            # Start by undoing the micro ops
            self.sub_ops.undo()

            # Remove new port
            if self.fail_location >= NEW_PORT_ADD:
                LOG.debug('running ovs-vsctl del-port to remove new port')
                # The OVS port name should be the same as the component name.
                # The OVS port name is arbitrarily selected as the first
                # component in the list.
                stdout, stderr = utils.execute('ovs-vsctl', 'del-port',
                                               self.ovs_name,
                                               self.new_ovs_port_name,
                                               run_as_root=True)

                if stderr and stderr != '':
                    raise exception.IBMPowerKVMCommandExecError(
                        cmd='ovs-vsctl',
                        exp=stderr)

                LOG.debug('ovs-vsctl del-port output = %s' % stdout)

            # Add old port
            if self.fail_location >= OLD_PORT_DELETE:
                if self.orig_port_length == 1:
                    LOG.debug('running ovs-vsctl add-port to re-add old port')
                    # For just a single component, we use add-port.
                    stdout, stderr = utils.execute('ovs-vsctl', 'add-port',
                                                   self.ovs_name,
                                                   self.orig_cmp_names[0],
                                                   run_as_root=True)
                    LOG.debug('ovs-vsctl add-port output = %s' % stdout)

                elif self.orig_port_length > 1:
                    LOG.debug('running ovs-vsctl add-bond to add new port')
                    # For multiple components, we use add-bond.
                    stdout, stderr = utils.execute('ovs-vsctl', 'add-bond',
                                                   self.ovs_name,
                                                   self.old_ovs_port_name,
                                                   *self.orig_cmp_names,
                                                   run_as_root=True)
                    LOG.debug('ovs-vsctl add-bond output = %s' % stdout)

                if stderr and stderr != '':
                    raise exception.IBMPowerKVMCommandExecError(
                        cmd='ovs-vsctl',
                        exp=stderr)

        except Exception as e:
            LOG.error(e)
            raise
