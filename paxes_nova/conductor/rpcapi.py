#
#
# =================================================================
# =================================================================

"""Overrides the Conductor RPC API for additional PowerVC DB Access:
    1. Query HMC Information for a given Host
    2. Add/Update/Deleted/Query VIOS Information
    3. Add/Update/Delete/Query Adapter Information (SEA/VEA/HBA)
    4. Add/Update/Delete/Query Host Metric/Status Information
    5. Add/Update/Delete/Query LPAR Allocation Information
"""

from nova.conductor import rpcapi
from nova.openstack.common import log as logging

LOG = logging.getLogger(__name__)


class PowerVCConductorRPCAPI(rpcapi.ConductorAPI):
    """ Extends the base Conductor RPC API class with PowerVC capabilities """

    def __init__(self):
        """Constructor for the PowerVC extension to the Conductor RPC API"""
        super(PowerVCConductorRPCAPI, self).__init__()

    ####################################################
    #########  Network Adapter Implementation  #########
    ####################################################
    def host_reconcile_network(self, context, host_networking_dict):
        """ Sends the collected topology of a host's networking resources """
        LOG.info('pvc_nova.conductor.rcpapi.PowerVCConductorRPCAPI '
                 'host_reconcile_network: context, '
                 'host_networking_dict len()= '
                 + str(len(host_networking_dict)))
        msg = self.make_msg('host_reconcile_network',
                            host_networking_dict=host_networking_dict)
        # TODO: 03 What 'version' should be sent in 'call'?
        return self.call(context, msg, version='1.52')
