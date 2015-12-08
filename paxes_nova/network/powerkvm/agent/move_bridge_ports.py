#
# =================================================================
# =================================================================

import copy
from nova import utils
from nova.openstack.common import log as logging
from powervc_nova.network.powerkvm import agent
from powervc_nova.network.powerkvm.agent import commandlet
from powervc_nova.network.powerkvm.agent.common import exception
from powervc_nova.network.powerkvm.agent.common import warning
from powervc_nova.network.powerkvm.agent.common import micro_op
from powervc_nova.network.powerkvm.agent.ovs_validation_mixin \
    import OVSValidator
from powervc_nova.objects.network import dom_kvm

LOG = logging.getLogger(__name__)


class MoveBridgePorts(micro_op.MicroOperation,
                      OVSValidator):
    """
    If a bridge device is specified to be added to the
    virtual switch, it is necessary to move the bridge
    ports off of the bridge such that the individual
    ports can be picked up by the virtual switch.  The
    reason for this is that linux does not allow for a
    bridge to be added directly to a virtual switch.
    """

    def __init__(self, device_name):
        """
        :param device_name: Bridge that we are moving ports from.
        """
        self.bridge_dev = device_name
        self.bridge_port_restore = {}
        self.bridge_port_list = []
        self.commandex = commandlet.CommandExecutor()
        self.current_dom = None
        LOG.debug("MoveBridgePorts() class initialized ")

    def validate(self, curr_dom):
        """
        Overrides parent method.

        Determine if any errors are likely to occur when moving ports
        from linux bridge to ovs.
        :param curr_dom: The current dom object representation of the system.
        """
        LOG.debug("MoveBridgePorts validate start")

        warning_list = []

        try:
            # Ensure the mixin has the correct DOM
            self.current_dom = curr_dom

            if self.current_dom is None:
                raise exception.IBMPowerKVMOVSNoValidDomSpecified

            if not self.bridge_dev:
                return curr_dom, warning_list

            # Only process if bridge is currently unassigned
            self.validate_port_unassigned(self.bridge_dev)

            # Look up the objects in the DOM
            bridge_obj = curr_dom.find_port_or_ovs(self.bridge_dev)

            # verify that input parms are proper type, if not
            # return no changes
            if not isinstance(bridge_obj, dom_kvm.LinuxBridge):
                return curr_dom, warning_list

        except Exception as exp:
            LOG.error(exp)
            raise

        # there are no ports on the bridge, so there
        # is nothing to do
        if not len(bridge_obj.port_list) > 0:
            warning_list.append(
                warning.OVSNoPortsOnBridgeWarning(
                    bridge_name=self.bridge_dev))

            return curr_dom, warning_list

        # Move the bridge ports for the unused list
        # in the DOM
        for port in bridge_obj.port_list:
            curr_dom.unused_component_list.append(port)
            self.bridge_port_list.append(port)

        bridge_obj.port_list = []

        LOG.debug("Updated DOM: %s", curr_dom)

        LOG.debug("MoveBridgePorts validate complete")

        warning_list.append(
            warning.OVSMovingPortsFromBridgeToOVSWarning(
                bridge_name=self.bridge_dev,
                ports=[self.bridge_port_list[i].name
                       for i in range(0, len(self.bridge_port_list))]))

        return curr_dom, warning_list

    def execute(self):
        """
        Overrides parent method.

        Remove ports from bridge to make available for
        later micro op processing.
        """
        LOG.debug("MoveBridgePorts execute start")

        for port in self.bridge_port_list:
            # Read source device config
            port_dict = self.commandex.get_ifcfg(port.name)[1]

            if port_dict is None or len(port_dict) == 0:
                # This means the file wasn't found for the source dev.
                raise exception.IBMPowerKVMSourceDevFileDoesNotExit()

            # make copy of original port ifcfg file in case restore
            # is needed
            self.bridge_port_restore[port.name] = copy.copy(port_dict)

            LOG.debug("Original ifcfg for port %s: %s" %
                      (port.name, port_dict))

            # update port ifcfg by removing reference to the bridge
            if agent.IFCFG_BRIDGE in port_dict:
                del port_dict[agent.IFCFG_BRIDGE]

            # Write the updated device config back to disk
            LOG.debug("Updated ifcfg for port %s: %s" %
                      (port.name, port_dict))

            self.commandex.send_ifcfg(port.name, port_dict)

            try:
                stdout, stderr = utils.execute('brctl', 'delif',
                                               self.bridge_dev,
                                               port.name,
                                               run_as_root=True)
                if stderr and stderr != '':
                    raise exception.IBMPowerKVMCommandExecError(cmd='brctl',
                                                                exp=stderr)
            except Exception as e:
                LOG.error(e)
                raise

        LOG.debug("MoveBridgePorts execute complete")

    def undo(self):
        """
        Overrides parent method.

        Rollback the port update, add back to bridge.
        """
        LOG.debug("MoveBridgePorts undo start")

        # In scenario where no source IP Address...
        try:
            for k, v in self.bridge_port_restore.iteritems():
                self.commandex.send_ifcfg(k, v)

                stdout, stderr = utils.execute('brctl', 'addif',
                                               self.bridge_dev,
                                               k,
                                               run_as_root=True)
                if stderr and stderr != '':
                    raise exception.IBMPowerKVMCommandExecError(cmd='brctl',
                                                                exp=stderr)
        except Exception as e:
            LOG.error(e)
            raise

        LOG.debug("MoveBridgePorts undo complete")
