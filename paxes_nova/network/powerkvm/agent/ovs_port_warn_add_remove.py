#
# =================================================================
# =================================================================

from nova import utils
from nova.openstack.common import log as logging
from powervc_nova.network.powerkvm.agent import commandlet
from powervc_nova.network.powerkvm.agent.common import exception
from powervc_nova.network.powerkvm.agent.common import warning
from powervc_nova.network.powerkvm.agent.common import micro_op
from powervc_nova.network.powerkvm.agent.ovs_validation_mixin \
    import OVSValidator

LOG = logging.getLogger(__name__)


class OvsPortWarnAddRemove(micro_op.MicroOperation,
                           OVSValidator):
    """
    Remove a port from an OpenVSwitch.
    """

    def __init__(self, ovs_name):
        """
        :param ovs_name: The name of the vswitch to remove from, a string.
        :param ovs_port: An OVSPort DOM object to remove.
        """
        self.ovs_name = ovs_name
        self.commandex = commandlet.CommandExecutor()

        self.current_dom = None

    def validate(self, curr_dom):
        """
        Overrides parent method.

        Validate

        """
        warning_list = []

        try:
            self.current_dom = curr_dom

            if self.current_dom is None:
                raise exception.IBMPowerKVMOVSNoValidDomSpecified

            """add warnings"""
            #throw a WARNING that removing an OpenVSwitch port may cause a
            #network outage for Virtual Machines if they are using it.
            warning_list.append(
                warning.OVSPortModificationNetworkOutageWarning(
                    ovs=self.ovs_name))

            #throw a WARNING if the port being asked to be removed is the
            #LAST port on the vSwitch, informing them that no external
            #traffic will be available on this vSwitch.
            ovs = curr_dom.find_ovs_obj(self.ovs_name)

            # since we earlier confirmed that the port is on the specified
            # ovs, we can proceed knowing that if there is only one port on
            # the ovs it is the port we are attempting to remove
            if ovs and len(ovs.ovs_port_list) == 0:
                warning_list.append(warning.OVSLastPortRemovalWarning(
                    ovs=self.ovs_name))

        except Exception as exp:
            LOG.error(exp)
            raise

        return curr_dom, warning_list

    def execute(self):
        """
        Overrides parent method.

        Validation only
        """
        pass

    def undo(self):
        """
        Overrides parent method.

        Validation only
        """
        pass
