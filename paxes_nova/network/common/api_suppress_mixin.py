#
# =================================================================
# =================================================================

from oslo.config import cfg
from paxes_nova.network.common import exception as net_excp

CONF = cfg.CONF

network_api_suppressor_opts = [
    cfg.StrOpt('host_type',
               default='powervm',
               help='The type of compute host')
]

CONF.register_opts(network_api_suppressor_opts)


class NetworkAPISuppressor(object):
    """
    This class handles suppression for APIs in unsupported environments.  This
    class is intended to be used as a mixin.
    """

    def __init__(self):
        # Store the host_type
        self.host_type = CONF.host_type.lower()

    def raise_if_not_powerkvm(self):
        """
        Raise an exception if we're not running in a PowerKVM environment.
        """
        if 'kvm' not in self.host_type:
            raise net_excp.IBMPowerVMNetworkAPIUnsupportedOutsidePowerKVM()

    def raise_if_not_powervm(self):
        """
        Raise an exception if we're not running in a PowerVM environment.
        """
        if 'powervm' not in self.host_type:
            raise net_excp.IBMPowerVMNetworkAPIUnsupportedOutsidePowerVM()
