#
#
# =================================================================
# =================================================================

"""Overrides the Conductor API for additional PowerVC DB Access:
    1. Query HMC Information for a given Host
    2. Add/Update/Deleted/Query VIOS Information
    3. Add/Update/Delete/Query Adapter Information (SEA/VEA/HBA)
    4. Add/Update/Delete/Query Host Metric/Status Information
    5. Add/Update/Delete/Query LPAR Allocation Information
"""

import oslo.config.cfg

from nova import utils
from nova.conductor import api
from nova.openstack.common import log as logging
LOG = logging.getLogger(__name__)

from paxes_nova.conductor import rpcapi
from paxes_nova.conductor import manager


class PowerVCConductorAPI(api.API):
    """ Extends the base Conductor API class with PowerVC capabilities """

    def __init__(self):
        """Constructor for the PowerVC extension to the Conductor API"""
        super(PowerVCConductorAPI, self).__init__()
        if oslo.config.cfg.CONF.conductor.use_local:
            LOG.info(self.__class__.__name__ + ": Using local access to "
                     "PowerVCConductorManager")
            self._manager = utils.ExceptionHelper(
                manager.PowerVCConductorManager())
        else:
            LOG.info(self.__class__.__name__ + ": Using RPC access to "
                     "PowerVCConductorManager")
            self._manager = rpcapi.PowerVCConductorRPCAPI()

    ####################################################
    #########  Network Adapter Implementation  #########
    ####################################################
    def host_reconcile_network(self, context, host_networking_dict):
        """ Sends the collected topology of a host's networking resources """
        LOG.info('pvc_nova.conductor.api.PowerVCConductorAPI '
                 'host_reconcile_network: context, '
                 'host_networking_dict len()= '
                 + str(len(host_networking_dict)))
        return self._manager.host_reconcile_network(context,
                                                    host_networking_dict)
