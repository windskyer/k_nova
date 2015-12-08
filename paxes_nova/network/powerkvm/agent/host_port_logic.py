#
# =================================================================
# =================================================================


from oslo.config import cfg

from powervc_nova.network.powerkvm.agent import commandlet
from nova.openstack.common import log as logging

LOG = logging.getLogger(__name__)
CONF = cfg.CONF


class HostPortAggregator():
    """
    This class is designed to aggregate various command outputs into one
    logical format and provide that as an input to the host-ovs REST API.
    """

    def get_host_dom(self):
        """
        This API is used to return the DOM object of net devices on the system
        This operates at the host and will be used to return DOM objects for
        the given host
        :return: host_ovs_config: A DOM object representing the host_ovs config
        """
        dom_converter = commandlet.DOMObjectConverter()
        return dom_converter.get_dom(self.get_host_name())

    def get_host_name(self):
        '''
        Returns the host name
        '''
        return CONF.host
