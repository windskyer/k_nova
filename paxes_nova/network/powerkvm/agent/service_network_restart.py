#
# =================================================================
# =================================================================

from nova.openstack.common import log as logging
from powervc_nova import _

from powervc_nova.network.powerkvm.agent import commandlet
from powervc_nova.network.powerkvm.agent.common import micro_op
from powervc_nova.network.powerkvm.agent.common import warning

LOG = logging.getLogger(__name__)


class ServiceNetworkRestart(micro_op.MicroOperation):
    """
    Restarts networking on the host endpoint to pick up configuration changes.
    """

    def __init__(self):
        """
        Constructor.
        """
        self.commandex = commandlet.CommandExecutor()

    def validate(self, current_dom):
        """
        No validation required.

        :param current_dom: State of the system prior to running
        :returns updated_dom: State of the system post running
        :returns warning_list: List of warnings that were found during
                               validation
        """
        LOG.info(_("Validating if a network restart can be performed"))
        return current_dom, [warning.OVSPortModificationVMActionWarning()]

    def execute(self):
        """
        Overrides parent method.

        Restarts networking on the host endpoint to pick up configuration
        changes. Checks and starts neutron-openvswitch-agent if it is not
        running already on host.
        """
        LOG.info(_("Running 'systemctl restart network.service' on host."))
        self.commandex.send_network_restart()
        LOG.info(_("Running 'systemctl restart "
                   "neutron-openvswitch-agent.service' on host."))
        self.commandex.restart_neutron_agent()

    def undo(self):
        """
        Overrides parent method.

        Undoing a restart isn't really possible.
        """
        LOG.info(_("Running undo 'service restart network.service' on host."))
        self.commandex.send_network_restart()
