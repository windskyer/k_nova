#
# =================================================================
# =================================================================

from oslo.config import cfg

from nova.openstack.common import log as logging

from powervc_nova import _
from powervc_nova.network.powerkvm.agent.common import micro_op
from powervc_nova.network.powerkvm.agent.common import exception
from powervc_nova.network.powerkvm.agent import commandlet

LOG = logging.getLogger(__name__)

CONF = cfg.CONF
CONF.import_opt('topic', 'nova.conductor.api', group='conductor')


class PingQpid(micro_op.MicroOperation):
    """
    Will ping the Qpid server and throw an exception if a connection can not
    be made ie no host adapter is able to ping the Qpid server
    """

    def __init__(self, context, rollback):
        self.dom_obj_converter = commandlet.DOMObjectConverter()
        self.rollback = rollback

    def validate(self, current_dom):
        """
        No validation required.

        :param current_dom: State of the system prior to running
        :returns updated_dom: State of the system post running
        :returns warning_list: List of warnings that were found during
                               validation
        """
        LOG.info(_("Validating if a ping to Qpid server can be "
                   "performed"))
        return current_dom, []

    def execute(self):
        """
        Overrides parent method.

        Will throw an error if the ping to the Qpid server fails.  If it
        succeeds this method does nothing.
        """
        LOG.info(_("Attempting to ping the Qpid Server"))
        if self.rollback:
            LOG.error(_("Rollback due to API parameter."))
            raise exception.QpidPingFailure(timeout="0")
        adapter = self.dom_obj_converter.find_management_adapter()
        if adapter is None:
            LOG.error(_("Failed to ping the Qpid Server."))
            raise exception.QpidPingFailure(timeout="1")

    def undo(self):
        """
        Overrides parent method.

        Undoing a ping doesn't do anything.
        """
        pass
