# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# =================================================================
# =================================================================

import logging

from powervc_k2 import k2operator as k2
from powervc_nova.db.network import models as model
from powervc_nova.virt.ibmpowervm.hmc.k2 import constants as k2const
from powervc_nova.virt.ibmpowervm.vif import topo
from powervc_nova.virt.ibmpowervm.vif.common import ras
from powervc_nova.virt.ibmpowervm.vif.hmc import utils
from powervc_nova.virt.ibmpowervm.vif import hmc
import time
from nova import rpc
from nova import context as ctx
from powervc_nova import _

from oslo.config import cfg

LOG = logging.getLogger(__name__)

CONF = cfg.CONF
CONF.import_opt('ibmpowervm_comm_timeout', 'powervc_nova.virt.ibmpowervm.hmc')
CONF.import_opt('send_rmc_busy_message', 'powervc_nova.virt.ibmpowervm.hmc')
CONF.import_opt('powervm_mgr', 'powervc_nova.virt.ibmpowervm.ivm')
CONF.import_opt('powervm_mgr_user', 'powervc_nova.virt.ibmpowervm.ivm')
CONF.import_opt('powervm_mgr_passwd', 'powervc_nova.virt.ibmpowervm.ivm')
CONF.import_opt('host', 'nova.netconf')


class IBMPowerVMNetworkTopoHMC(topo.IBMPowerVMNetworkTopo):

    """
    IBM PowerVM Shared Ethernet Adapter Topology HMC class.
    """

    last_rmc_busy_notification = 0

    def __init__(self, host_name, operator, mtms=None):
        """
        Set the operator we'll use to interface with the HMC.

        :param host_name: Host_name for this compute process's node
        :param operator: K2 operator used to interface with the HMC.
        :param mtms: The MTMS for the managed system
        """
        super(IBMPowerVMNetworkTopoHMC, self).__init__(host_name=host_name,
                                                       operator=operator)

        self._managed_system_uuid = None

        if mtms:
            self.mtms = mtms
        else:
            self.mtms = CONF.host

        # Maintain a dictionary of key-values.  The key is the k2 vswitch id
        # and the value is the k2 object itself.
        self.vswitch_map = {}

        # Maintain a map of the Virtual IO Servers.  The key is the lpar
        # id.  The value is the href to load the VIO
        self.vio_server_map = {}

        self.k2_vios_resp = None
        self._k2_managed_system = None

        # Use the K2 operators default cache time
        self.read_age = -1

    def _set_default_k2_read_age(self, new_age):
        """
        Sets the default age to be used on K2 reads.  It defaults to -1, which
        is the default that looks based on schema type.  Setting to 0 will
        allow it to read everything fresh, without any K2 cache.
        """
        self.read_age = new_age

    def _populate_adapters_into_vios(self, vios,
                                     dom_factory=model.No_DB_DOM_Factory()):
        """
        This method will populate the given VIOS with all the adapters, both
        SEA and VEA, found on that VIOS.

        :param vios: VioServer to fetch adapters from
        :param dom_factory: Factory used to create the DOM objects, optional.
        :returns boolean: True if at least one SEA was found, False
                          otherwise.
        """
        # Now populate the VIOS information
        # If the VIOS RMC state is down, we can't use adapters on it.
        curr_state = vios.rmc_state.lower()
        if curr_state != 'active':
            # send notification to UI if RMC is in a busy state
            # and notification hasn't been sent during the
            # interval specified.
            if curr_state == 'busy' and CONF.send_rmc_busy_message:
                curr_time = time.time()
                send_interval = 3600  # 60 minutes

                if((curr_time -
                    IBMPowerVMNetworkTopoHMC.last_rmc_busy_notification) >
                   send_interval):

                    IBMPowerVMNetworkTopoHMC.last_rmc_busy_notification = \
                        curr_time
                    payload = {}
                    payload['msg'] = _('The RMC for %(vio)s on %(host)s has '
                                       'a status code of busy.  One reason '
                                       'for this can be the incorrect sizing '
                                       'of the VIOS. Refer to the IBM PowerVM '
                                       'Best Practices red book to ensure '
                                       'that the VIOS has the appropriate set '
                                       'of resources to service the '
                                       'requests.') \
                        % {'vio': vios.name,
                           'host': CONF.host_display_name}
                    notifier = rpc.get_notifier(service='compute',
                                                host=CONF.host)
                    notifier.warn(ctx.get_admin_context(),
                                  'compute.instance.log',
                                  payload)

            ras.trace(LOG, __name__, ras.TRACE_WARNING,
                      _('RMC state is not active on VIOS lpar %s') %
                      vios.lpar_name)
            return False

        # If we found NO VEAs, then we can't proceed.  An SEA backed by
        # no VEA is useless.
        if not utils.build_veas_from_K2_vios_response(self.k2_vios_resp,
                                                      vios,
                                                      self.vswitch_map,
                                                      self.operator,
                                                      dom_factory):
            ras.trace(LOG, __name__, ras.TRACE_WARNING,
                      ras.vif_get_msg('info', 'VIOS_NOVEA'))
            return False

        # If we find at least one SEA, we're successful.  NOTE: It may
        # seem like we're building duplicate VEAs into the VIOS with the
        # call below since we already built them with the call above.
        # This is ok because the VioServer object will filter out
        # duplicates.  It's necessary to do the first build above because
        # that's the only way we'll find orphaned VEAs.
        if utils.build_seas_from_K2_response(self.k2_vios_resp,
                                             vios,
                                             self.k2_net_bridge_resp,
                                             self.vswitch_map,
                                             self.operator,
                                             dom_factory):
            ras.trace(LOG, __name__, ras.TRACE_DEBUG,
                      ras.vif_get_msg('info', 'FOUND_SEAS') %
                      {'num_seas':
                       len(vios.get_shared_ethernet_adapters()),
                       'vios_name': vios.name})
            return True
        else:
            # Found no SEAs... this means the VIOS is not usable for deploy.
            ras.trace(LOG, __name__, ras.TRACE_WARNING,
                      ras.vif_get_msg('error', 'VIOS_NOSEA'))
            return False

    def _find_managed_system(self):
        """
        Finds the system uuid for this specific server that the HMC is
        managing.
        """
        if(self._managed_system_uuid is None or
           self._k2_managed_system is None):
            # Cache the values locally for future reads
            self._managed_system_uuid, self._k2_managed_system = \
                utils._find_managed_system(self.operator, self.mtms)

        return self._managed_system_uuid

    def _find_k2_managed_system(self):
        """
        Returns the K2 ManagedSystem for this specific server that the HMC is
        managing.
        """
        if self._k2_managed_system is None:
            # This call will also set the entry property
            self._find_managed_system()

        # Return the data
        return self._k2_managed_system

    def _repopulate_vswitch_data(self):
        """
        Repopulates the VSwitch Data
        """
        # Clear out the data
        self.vswitch_map = {}

        # Populate the vSwitch information...
        self.vswitch_map = utils.find_vswitch_map(self.operator,
                                                  self._find_managed_system())

    def _is_cec_running(self):
        """
        Will return if the CEC is up.
        """
        k2_mg_sys = self._find_k2_managed_system()
        state_elem = k2_mg_sys.element.find('State')
        if state_elem is None:
            LOG.info(_("State is not found.  Assuming CEC is powered off."))
            return False

        # It *should* always be a K2 element, and if so, get the actual state
        # off of it
        if isinstance(state_elem, k2.K2Element):
            state_elem = state_elem.gettext()

        state = (state_elem.lower() in ('operating', 'standby'))
        LOG.debug("State of the system is %s" % state_elem)
        return state

    def _get_all_vios(self, dom_factory=model.No_DB_DOM_Factory()):
        """
        This method will return all VIOSes available on the current host (as
        identified in the operator).

        :param dom_factory: Factory used to create the DOM objects, optional.
        :returns vioses: A list of VioServer objects
        """
        # Get the system we are managing
        managed_system_uuid = self._find_managed_system()

        # Clear out the data
        self.vio_server_map = {}
        self._repopulate_vswitch_data()

        # Move on to VIOS info.  Start with an empty list
        vios_list = []

        # Get all the VIOSes on the HMC.  The response will contain all the
        # info we need for adapters, so save it off so we don't have to
        # make unnecessary K2 calls later on.
        ras.function_tracepoint(LOG, __name__,
                                ras.TRACE_DEBUG, 'Getting VIOS')
        self.k2_vios_resp = self.operator.read(rootType='ManagedSystem',
                                               rootId=managed_system_uuid,
                                               childType='VirtualIOServer',
                                               timeout=hmc.K2_RMC_READ_SEC,
                                               xag=[k2const.VIOS_NET_EXT_PROP],
                                               age=self.read_age)
        ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG, 'Got VIOS')

        # Get the network bridges
        ras.function_tracepoint(LOG, __name__,
                                ras.TRACE_DEBUG,
                                'Getting Network Bridges')
        self.k2_net_bridge_resp = self.operator.read(
            rootType='ManagedSystem',
            rootId=managed_system_uuid,
            childType='NetworkBridge',
            timeout=hmc.K2_RMC_READ_SEC,
            age=self.read_age)
        ras.function_tracepoint(LOG, __name__,
                                ras.TRACE_DEBUG,
                                'Got Network Bridges')

        # If we find no VIOS, just return
        if not self.k2_vios_resp.feed or not self.k2_vios_resp.feed.entries:
            ras.trace(LOG, __name__, ras.TRACE_DEBUG,
                      ras.vif_get_msg('error', 'VIOS_NONE') % self.host_name)
            return vios_list

        # Loop through all VIOSes found
        for entry in self.k2_vios_resp.feed.entries:

            # Grab the necessary attributes
            lpar_id = int(entry.element.findtext('PartitionID'))
            lpar_name = entry.element.findtext('PartitionName')
            state = entry.element.findtext('PartitionState')
            rmc_state = entry.element.findtext('ResourceMonitoring'
                                               'ControlState')

            # If RMC is down, we want to log why.  Could be helpful down the
            # road...
            if rmc_state is None or rmc_state.lower() != 'active':
                LOG.warn(_('K2 RMC state for lpar %(lpid)d (%(lname)s): '
                           '%(state)s') % {'lpid': lpar_id, 'lname': lpar_name,
                                           'state': rmc_state})
                data = self.k2_vios_resp.body
                LOG.warn(_('K2Response: %(resp)s') % {'resp': data})

            # Add the data to the map
            self.vio_server_map[lpar_id] = entry.properties['link']

            # Create a VIOS DOM object
            vios = dom_factory.create_vios(lpar_name=lpar_name,
                                           lpar_id=lpar_id,
                                           host_name=self.host_name,
                                           state=state,
                                           rmc_state=rmc_state)
            vios_list.append(vios)

            ras.trace(LOG, __name__, ras.TRACE_DEBUG,
                      ras.vif_get_msg('info', 'FOUND_VIOS') %
                      {'lpar_id': lpar_id, 'lpar_name': lpar_name})

        return vios_list
