# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# =================================================================
# =================================================================

import logging

#from powervc_k2.k2operator import K2Error
from paxes_nova.db.network import models as dom
from paxes_nova.virt.ibmpowervm.vif.common import exception as excp
from paxes_nova.virt.ibmpowervm.vif.common import ras
from paxes_nova import _
LOG = logging.getLogger(__name__)


class IBMPowerVMNetworkTopo(object):
    """
    IBM PowerVM Shared Ethernet Adapter Topology base class, version 2.
    This class provides all the logic for collecting the topology of a VIOS.
    Subclasses need only implement the methods that actually retrieve
    information from the hardware itself.  These method stubs are located at
    the bottom of this class and describe what they are to return.
    """

    def __init__(self, host_name, operator=None):
        """
        Initialize the DOM objects to None and set the operator we'll use
        to interface with the system.  NOTE: Subclasses' __init__ should call
        this __init__() and then set up their specific operator.  For IVM, this
        will be an IVMOperator object.  For HMC, this will be a K2 operator.

        :param host_name: Host_name for this compute process's node
        :param operator: Operator used to interface with the system.
        """
        self.host = None
        if not host_name:
            ras.function_tracepoint(LOG,
                                    __name__,
                                    ras.TRACE_ERROR,
                                    ras.vif_get_msg('error',
                                                    'DEVNAME_INVALID') %
                                    {'devname': host_name})
            raise excp.IBMPowerVMInvalidHostConfig(attr='host_name')
        self.host_name = host_name
        self.operator = operator

    def get_current_config(self, must_discover_sea=True):
        """
        This method will retrieve the current configuration of the SEAs on the
        PowerVM host and return information about them in a Host object.

        :param must_discover_sea: If True, we will raise an exception if no
                                  SEAs are discovered.
        :returns host: A Host object (from the DOM) containing info on
                       the VIOS(es) and their adapters.
        """
        ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG, "Enter")

        # Refresh the topology.  Force the discovery so that any errors
        # may be thrown upwards.
        self.refresh(must_discover_sea)

        # Return the Host created
        return self.host

    def refresh(self, must_discover_sea=True):
        """
        This method will refresh the topology such that it will reset its
        internal data model and then run a discovery on the system.  This
        ensures that any changes done on the system itself will be picked up
        and added to the data model.
        """
        # Reset the data model
        self.host = None

        # If the CEC is not running, do not discover the host.
        if self._is_cec_running():
            # Discover the VIOSes and remember if discovery succeeds
            self._discover_vios_config()

        # If we were told we must discover at least one SEA, verify we did.
        if must_discover_sea:
            if self.host is None:
                msg = ras.vif_get_msg('error', 'NET_INVALIDCFG')
                ras.function_tracepoint(LOG, __name__, ras.TRACE_ERROR, msg)
                raise excp.IBMPowerVMInvalidMgmtNetworkCfg(msg=msg)
            elif not self.host.find_all_primary_seas():
                # If any of the VIOSes have RMC down, they could have been the
                # one with an SEA on it.  Raise an exception pointing to RMC
                # as the problem.
                at_least_one_rmc_active = False
                at_least_one_rmc_inactive = False
                for vios in self.host.vio_servers:
                    if vios.rmc_state.lower() != 'active':
                        ras.function_tracepoint(LOG, __name__, ras.TRACE_ERROR,
                                                ras.vif_get_msg('error',
                                                                'VIOS_NORMC') %
                                                {'lpar': vios.lpar_name,
                                                 'state': vios.rmc_state})
                        at_least_one_rmc_inactive = True
                    else:
                        at_least_one_rmc_active = True

                if at_least_one_rmc_active and at_least_one_rmc_inactive:
                    # We found a mixture of both active and inactive RMC
                    # connections.  We can't definitively point to RMC as the
                    # problem, so put out a message that it COULD be the
                    # reason we found no SEAs.
                    raise excp.IBMPowerVMRMCDownNoSEA(host=self.host_name)
                elif at_least_one_rmc_active:
                    # We found an active RMC connection but no inactive ones,
                    # so we really have a situation where no SEAs were found.
                    raise excp.IBMPowerVMValidSEANotFound()
                elif at_least_one_rmc_inactive:
                    # We must've found NO active RMC connections.  Put out
                    # the very specific message about this.
                    raise excp.IBMPowerVMAllRMCDown(host=self.host_name)
                else:
                    # This can only be reached if neither active nor inactive
                    # RMC connections were found.  In other words, if no
                    # VIOSes were found.  I doubt this will ever happen, but
                    # better safe than sorry.
                    msg = ras.vif_get_msg('error', 'NET_INVALIDCFG')
                    raise excp.IBMPowerVMInvalidHostConfig(attr='No VIOS')
        else:
            return

    def _discover_vios_config(self):
        """
        This function will discover the SEA configuration on the managed
        VIOSes. If it detects any faulty configuration, an exception will
        be thrown.  The exception should include data on what the issue was.
        """
        ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG, "Enter")

        try:
            # Get all the VIOS under this host, and verify we have at least one
            vio_servers = self._get_all_vios()
            if not vio_servers:
                ras.function_tracepoint(LOG,
                                        __name__,
                                        ras.TRACE_ERROR,
                                        ras.vif_get_msg('error',
                                                        'VIOS_NONE') %
                                        self.host_name)
                raise excp.IBMPowerVMInvalidHostConfig(attr='vios')

            # Loop through every VIOS on the host.
            for vios in vio_servers:
                # See if we find some adapters
                if not self._populate_adapters_into_vios(vios):
                    # Found no adapters... this could be fine, but log it.
                    ras.function_tracepoint(LOG,
                                            __name__,
                                            ras.TRACE_WARNING,
                                            vios.lpar_name + ': ' +
                                            ras.vif_get_msg('error',
                                                            'VIOS_NOADAPTERS'))

            # If we get here, we've found all adapters, added them to their
            # respective VioServer, and verified every VioServer has at least
            # one SharedEthernetAdapter.  Create the Host object with those
            # VioServers and we're done!
            self.host = dom.Host(self.host_name, vio_servers)

            # Update the available pool of VLAN IDs
            self.host.maintain_available_vid_pool()

#         except K2Error as e:  # Bug0002104,NameError: global name 'K2Error' is not defined
#             # If this was a K2Error, we want to reraise it so we don't put
#             # out a message about an invalid configuration, which is misleading
#             if e.k2response is not None:
#                 LOG.error(_('Request headers:'))
#                 LOG.error(e.k2response.reqheaders)
#                 LOG.error(_('Request body:'))
#                 LOG.error(e.k2response.reqbody)
#                 LOG.error(_('Response headers:'))
#                 LOG.error(e.k2response.headers)
#                 LOG.error(_('Response body:'))
#                 LOG.error(e.k2response.body)
#             raise

        except Exception as e:
            msg = (ras.vif_get_msg('error', 'VIOS_UNKNOWN') + " (" +
                   (_('%s') % e) + ")")

            ras.function_tracepoint(LOG, __name__, ras.TRACE_EXCEPTION, msg)

    """
    ALL METHODS BELOW MUST BE IMPLEMENTED BY SUBCLASSES
    """

    def _populate_adapters_into_vios(self, vios, dom_factory):
        """
        This method should put all the adapters, both SEA and VEA, into the
        given VIOS.  Each VEA should be attached to it's owning SEA, if it
        has one.

        :param vios: VioServer to fetch adapters from
        :param dom_factory: Factory used to create the DOM objects, optional.
        :returns boolean: True if at least one adapter was found, False
                          otherwise.
        """
        raise NotImplementedError("_get_adapters_for_vios not implemented")

    def _get_all_vios(self, dom_factory):
        """
        This method should return all VIOS available on the current host (as
        identified in the operator).  For IVM, this should only be one VIOS,
        for HMC it could be one or more.

        :param dom_factory: Factory used to create the DOM objects, optional.
        :returns vioses: A list of VioServer objects
        """
        raise NotImplementedError("_get_all_vios not implemented")

    def _is_cec_running(self):
        """
        Will return if the CEC is up.
        """
        # Default implementation is that the CEC is alive.
        return True
