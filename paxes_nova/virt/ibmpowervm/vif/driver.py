# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# =================================================================
# =================================================================

"""
IBMPowerVMBaseVIFDriver
"""

import time
import traceback

from nova import context as ctx
from nova.openstack.common import lockutils
from nova.openstack.common import log as logging
from nova.db.sqlalchemy import api as db_session

from oslo.config import cfg

#from powervc_k2.k2operator import K2Error
from paxes_nova.db import api as dom_api
from paxes_nova.db.network import models as dom
from paxes_nova.virt.ibmpowervm.vif.common import exception as excp
from paxes_nova.virt.ibmpowervm.vif.common import ras
from paxes_nova.virt.ibmpowervm.vif.common import utils
from paxes_nova.network.ibmpowervm import adapter_mapping as mapping_task

from pprint import pformat

from paxes_nova import _

LOG = logging.getLogger(__name__)
CONF = cfg.CONF


class IBMPowerVMBaseVIFDriver(object):
    """
    This is the IBMPowerVM base VIF driver class.

    The intended way to set IBM nova VIF driver up is to instantiate the real
    VIF driver in the powervm driver's __init__ method based on the specific
    VIF driver configuration defined in the nova.conf. Then the vif plug/unplug
    method will be ready for use.
    """

    def __init__(self):
        """
        Base initialization of driver variables
        """
        self.current_topo = None
        self.current_topo_time = None

    @lockutils.synchronized('powervm_vifs', 'nova-vif-powervm-')
    def plug(self, instance, vifs, dom_factory=dom.DOM_Factory()):
        """
        IBMPowerVMVlanVIFDriver's plug method.  Will plug each VIF into the
        VIOS SEA.

        :param instance: network instance
        :param vifs: The list of VIFs that will be plugged.  Each should have
                     a vlan attribute that tells us what is being plugged.
        :param dom_factory: Used to create DOM objects.
        """
        LOG.debug('Enter plug with vifs: %s' % vifs)
        # Walk through each VIF entry in the list for the deploy
        succesful_vif_plugs = []

        host = None
        try:
            host = self._get_current_topo()
            # Get all the seas from the host_dom
            all_seas = host.find_all_primary_seas()
        except Exception as e:
            # This will happen in a scenario where Paxes is restarted, at
            # the same time as a POWER CEC is down.  Should ignore.
            pass

        if host is None:
            LOG.debug("Plug is passing due to Host being none.  May be due to"
                      " CEC being powered off upon reboot.")
            return

        try:
            for vif_entry in vifs:
                # Call _parse_vif in a try block so we catch an RMC busy
                # exception and can retry topo collection
                try:
                    vswitch, data_vlan_id, sea, needs_provision =\
                        self._parse_vif(host, vif_entry, True)
                except excp.IBMPowerVMRMCBusy:
                    # Give it one retry.  If THIS fails, we're done.
                    LOG.warn(_('RMC was busy, retrying topo'))
                    host = self._get_current_topo()
                    all_seas = host.find_all_primary_seas()
                    vswitch, data_vlan_id, sea, needs_provision =\
                        self._parse_vif(host, vif_entry, True)

                # First, see if we have some fixing up of SEAs to do.  If
                # we're not going to deploy the vlan because it's already
                # there, we want to be sure we still fix up the config if
                # the user altered it outside Paxes.
                self._check_and_correct_vlan_config(data_vlan_id, dom_factory,
                                                    host, sea)

                # If _parse_vif determined this vlan doesn't need provisioning,
                # or a vswitch wasn't returned, skip this vif
                if not needs_provision or vswitch is None:
                    LOG.debug('Skipping provision (needs_provision: %s, '
                              'vswitch: %s) for vif_entry: %s',
                              needs_provision, vswitch, vif_entry)
                    continue

                start_tb = time.time()
                # We only need to pass the first SEA in the chain.  IVM does
                # not support redundant VIOS (therefore, no chain) and HMC will
                # automatically handle the redundant SEA.
                self._plug_data_vlan_into_sea(host=host,
                                              sea_dev=sea,
                                              vlan_id=data_vlan_id,
                                              dom_factory=dom_factory)
                ras.trace(LOG, __name__, ras.TRACE_INFO,
                          _('Successfully plugged data vlan %(vid)d into '
                            'SEA') % {'vid': data_vlan_id}, start_tb)
                succesful_vif_plugs.append(vif_entry)
        except Exception as e:
            if 'host' in locals() and host is not None:
                LOG.error(_('host:'))
                LOG.error(pformat(host.to_dictionary()))

            if not 'data_vlan_id' in locals():
                data_vlan_id = -1

            # If this was a K2 error, we want to log more info about it
#             if isinstance(e, K2Error) and e.k2response is not None: Bug0002104,NameError: global name 'K2Error' is not defined
#                 LOG.error(_('Request headers:'))
#                 LOG.error(e.k2response.reqheaders)
#                 LOG.error(_('Request body:'))
#                 LOG.error(e.k2response.reqbody)
#                 LOG.error(_('Response headers:'))
#                 LOG.error(e.k2response.headers)
#                 LOG.error(_('Response body:'))
#                 LOG.error(e.k2response.body)

            ras.trace(LOG, __name__, ras.TRACE_ERROR,
                      ras.msg('error', 'SEA_FAILTOPLUG') %
                      {'vid': data_vlan_id})
            ras.trace(LOG, __name__, ras.TRACE_ERROR, traceback.format_exc())

            # Do a clean up by calling unplug
            for vif_entry in succesful_vif_plugs:
                vswitch, failed_vlan_id, sea, needs_provision = \
                    self._parse_vif(host, vif_entry)
                if needs_provision:
                    self._unplug_data_vlan_from_sea(host=host,
                                                    sea=sea,
                                                    vlan_id=failed_vlan_id,
                                                    dom_factory=dom_factory)

            # Reraise the exception that got us here
            raise

    @lockutils.synchronized('powervm_vifs', 'nova-vif-powervm-')
    def unplug(self, instance, vifs, dom_factory=dom.DOM_Factory()):
        """
        IBMPowerVMVlanVIFDriver unplug method.  Will unplug each VIF from the
        VIOS SEA

        :param instance: network instance
        :param vifs: The list of VIFs that will be unplugged.  Each should have
                     a bridge attribute that contains the vSwitch/VLAN
        :param dom_factory: Used to create DOM objects.
        """
        LOG.debug('Enter unplug with vifs: %s' % vifs)

        try:
            # Walk through each VIF entry in the list for the deploy
            host = self._get_current_topo()
        except Exception as exc:
            # This can happen if K2 throws an error on the VIOS read, the
            # Client Network Adapter read or some other K2 error.
            # As part of the 'do not block unplug' strategy, we should catch
            # this error, log and return.
            LOG.warn(_('Ignoring Unplug Error: %s') % exc)
            return

        try:
            for vif_entry in vifs:
                vswitch, data_vlan_id, sea, needs_provision =\
                    self._parse_vif(host, vif_entry)

                if not needs_provision or vswitch is None:
                    LOG.debug('Skipping provision (needs_provision: %s, '
                              'vswitch: %s) for vif_entry: %s',
                              needs_provision, vswitch, vif_entry)
                    continue

                force = vif_entry['meta'].get('force', False)

                start_tb = time.time()
                # We only need to pass the first SEA in the chain.  IVM does
                # not support redundant VIOS (therefore, no chain) and HMC will
                # automatically handle the redundant SEA.
                self._unplug_data_vlan_from_sea(host=host,
                                                sea=sea,
                                                vlan_id=data_vlan_id,
                                                dom_factory=dom_factory,
                                                force=force)
                ras.trace(LOG, __name__, ras.TRACE_INFO,
                          _('Successfully unplugged data vlan %(vid)d from '
                            'SEA') % {'vid': data_vlan_id}, start_tb)
        except Exception as e:
            if 'host' in locals() and host is not None:
                LOG.error(_('host:'))
                LOG.error(pformat(host.to_dictionary()))

            # If this was a K2 error, we want to log more info about it
#             if isinstance(e, K2Error) and e.k2response is not None: Bug0002104,NameError: global name 'K2Error' is not defined
#                 LOG.error(_('Request headers:'))
#                 LOG.error(e.k2response.reqheaders)
#                 LOG.error(_('Request body:'))
#                 LOG.error(e.k2response.reqbody)
#                 LOG.error(_('Response headers:'))
#                 LOG.error(e.k2response.headers)
#                 LOG.error(_('Response body:'))
#                 LOG.error(e.k2response.body)
# 
#                 # Reraise the exception that got us here
#                 raise

    def _get_current_topo(self):
        """
        Will return the topology for the system.  If it has not been
        invalidated, or has been less than a period of time allowed, will
        return a cached value.  Otherwise will grab from the system.
        """
        # If the time was set, and the time is less than X seconds, we can
        # just return the current topology.
        if self.current_topo_time and\
                time.time() - self.current_topo_time < 10:
            LOG.debug(self.current_topo.to_dictionary())
            ras.trace(LOG, __name__, ras.TRACE_INFO,
                      _('Using cached system topology'))
            ras.trace(LOG, __name__, ras.TRACE_DEBUG, 'Cached host:\n%s' %
                      self.current_topo.to_dictionary())
            return self.current_topo

        # Either the topo wasn't set or its too old.  Lets go get it again.
        # Always get the time AFTER the topo, since the topo could take some
        # multiple seconds.  Also note we are thread safe by the invokers of
        # this method.
        self.current_topo = self._topo.get_current_config()
        self.current_topo_time = time.time()
        return self.current_topo

    def _invalidate_topo_cache(self):
        """
        Will invalidate the topology cache from _get_current_topo()
        """
        self.current_topo = None
        self.current_topo_time = None

    def _get_ctx(self):
        """
        Returns the context that should be used for database calls.
        """
        return ctx.get_admin_context()

    def _get_host(self):
        """
        Returns the host that this process is running on.
        """
        return CONF.host

    def _get_host_display_name(self):
        """
        Returns the display name of the host that this process is running on.
        """
        return CONF.host_display_name

    def _parse_vif(self, host, vif, here_for_plug=False):
        """
        Parses out the VIF information to return the vSwitch and VLAN

        :param host: The Host DOM object that represents the endpoint (IVM or
                     HMC)
        :param vif: The VIF that will contain the VLAN and vSwitch via the
                    bridge attribute.
        :param here_for_plug: Boolean to indicate whether the caller is plug or
                              unplug
        :return vSwitch: the vSwitch attribute (string)
        :return vlan: the VLAN for the element (int)
        :return sea: The Primary SEA to provision against
        """
        LOG.debug('Enter _parse_vif with vif: %s' % vif)

        if not vif:
            LOG.warn(_('No vif passed in'))
            return None, None, None, False

        try:
            context = self._get_ctx()
            host_name = self._get_host()
            host_display_name = self._get_host_display_name()

            meta = vif['meta']
            netmeta = vif['network']['meta']
            if meta and ('vlan' in meta):
                vlan_string = meta['vlan']
            elif netmeta and ('vlan' in netmeta):
                vlan_string = str(netmeta['vlan'])
            else:
                # Occasionally happens, shouldn't fail but just continue
                LOG.warn(_('Failed to find vlan in vif: %s') % pformat(vif))
                return None, None, None, False

            # First try to get the network-mapping
            net_id = vif.get('network').get('id')
            session = db_session.get_session()
            with session.begin():
                net_assn = dom_api.network_association_find(context, host_name,
                                                            net_id, session)

                # Check to see if the user has a network association, but they
                # specifically said that it can not be used on this host
                if net_assn is not None and net_assn.sea is None:
                    raise excp.IBMPowerVMNetworkNotValidForHost(
                        host=host_display_name)

                # Set the Shared Ethernet Adapter to the value in the network
                # association, if it exists...we may yet have to create the
                # default network association
                sea = None
                if net_assn is not None:
                    sea = net_assn.sea

                # If the network association doesn't exist...we must create the
                # default association
                if net_assn is None:
                    msg = (ras.vif_get_msg('info', 'SEA_DEPLOY') %
                           {'net_id': net_id})
                    ras.function_tracepoint(LOG, __name__, ras.TRACE_INFO, msg)
                    adpt_mapper = mapping_task.AdapterMappingTaskClass()
                    sea = adpt_mapper.build_default_network_assn(context,
                                                                 host,
                                                                 vlan_string,
                                                                 net_id)

                # If the SEA isn't available (either it's state is 'Defined' or
                # VIOS isn't running or rmc is inactive), can't use it.
                if not sea:
                    raise excp.IBMPowerVMValidSEANotFound(
                        host=host_display_name)

                # Now let's check the VIOSes as well and throw an
                # exception if anyone of them is not available.  Note, we have
                # to use the db version of the host DOM as the live topology
                # will not include any SEAs on VIOSes with RMC inactive.
                lp_id = sea.vio_server.lpar_id
                host_db = utils.get_host_dom(context, host_name)
                sea_chain = host_db.find_sea_chain_for_sea_info(sea.name,
                                                                lp_id)

                # A list of VIOS ids which have RMC down.
                inactive_rmc_vios = []
                # To notify that one of the VIOS has RMC down
                inactive_rmc_flag = False
                for sea_obj in sea_chain:
                    if not sea_obj.is_available():
                        inactive_rmc_flag = True
                        inactive_rmc_vios.append(sea_obj.vio_server.lpar_id)
                        ras.trace(LOG, __name__, ras.TRACE_ERROR,
                                  ras.msg('error', 'VIOS_RMC_DOWN') %
                                  {'lpar': sea_obj.vio_server.lpar_id,
                                   'host': host_name})

                # We should only throw the RMC inactive exception if the VLAN
                # is not currently on the SEA and we're here for a plug
                # (meaning it needs to be plugged) or if the vlan currently IS
                # on the SEA and we're here for an unplug (meaning it needs to
                # be unplugged).  needs_provision means so much more though,
                # not just about RMC.  So we first need to try to base that
                # off live topology, if we can.
                vid = int(vlan_string)
                live_sea = host.find_adapter(sea.vio_server.lpar_id, sea.name)
                if live_sea is not None:
                    LOG.debug('Checking for vlan %d in live topo' % vid)
                    vea_with_vlan = live_sea.get_vea_by_vlan_id(vid,
                                                                throw_error=
                                                                False)
                else:
                    LOG.debug('Checking for vlan %d in db topo' % vid)
                    vea_with_vlan = sea.get_vea_by_vlan_id(vid,
                                                           throw_error=False)

                # Determine if we need to provision the vlan
                needs_provision = False
                if here_for_plug:
                    # If the vlan was on no VEAs, we obviously need a plug.
                    if vea_with_vlan is None:
                        needs_provision = True
                    elif vea_with_vlan.pvid == vid:
                        # The vlan was found as the pvid of a VEA.  This
                        # potentially means we still need to move the vlan to
                        # an addl_vlan_id if this is a secondary VEA.  If it's
                        # a primary VEA, then the plug will no-op.  However,
                        # we'll let the logic in _plug_data_vlan_into_sea
                        # handle all this.
                        needs_provision = True
                else:
                    # We're here for an unplug.  If we found the vlan on any
                    # VEA, we'll allow the provision.  Let
                    # _unplug_data_vlan_from_sea handle the logic of no-op'ing
                    # if it's a reserved vlanid.
                    if vea_with_vlan is not None:
                        needs_provision = True

                LOG.debug('_parse_vif for %s needs_provision: %s',
                          'plug' if here_for_plug else 'unplug',
                          needs_provision)

                # Set the data for this network into its corresponding net_info
                # now pop out all the RMC information and throw the exception.
                if inactive_rmc_flag and needs_provision:
                    down_rmc_str = str(inactive_rmc_vios)
                    down_rmc_str = down_rmc_str.replace("[", "")
                    down_rmc_str = down_rmc_str.replace("]", "")
                    raise excp.IBMPowerVMRMCDown(host=host_display_name,
                                                 lpar_id=down_rmc_str)
                if isinstance(sea, dict):
                    sea = host.find_adapter(sea['lpar_id'], sea['sea_name'])

                # At this point, we know sea is not None because we would've
                # raised IBMPowerVMValidSEANotFound if it were.  If live_sea is
                # None, that means adapter mapping found an SEA (ie, the
                # variable "sea") that the live topology does not have (ie,
                # the host.find_adapter() call just prior to here).  This is
                # bad.
                if live_sea is None:
                    # Dump the network association's SEA's VIOS because this
                    # comes from the db.  This can be compared to the live
                    # topology's VIOSes to see where the discrepancy lies.
                    LOG.error(_('Failed to find %(sea)s on lpar %(lpid)d') %
                              {'sea': sea.name,
                               'lpid': sea.vio_server.lpar_id})
                    LOG.error(_('VIF:'))
                    LOG.error(pformat(vif))
                    LOG.error(_('Network association found:'))
                    LOG.error(pformat(net_assn.to_dictionary()) if net_assn
                              else None)
                    LOG.error(_('Network association\'s SEA\'s VIOS (from '
                                'db):'))
                    LOG.error(pformat(sea.vio_server.to_dictionary()))
                    LOG.error(_('Live topology host:'))
                    LOG.error(pformat(host.to_dictionary()))

                    # We could get here if the target SEA is on a VIOS whose
                    # RMC is not active but it wasn't updated in the db yet (ie
                    # it changed from active in between the last reconcile and
                    # now) which is why the inactive_rmc_flag check above
                    # didn't catch this since it's based on the db dom.
                    # However, we do want to differentiate between RMC inactive
                    # vs RMC busy in that we want to do a retry if it was just
                    # busy.  That said, none of this matters if the VLAN
                    # doesn't need to be provisioned.
                    if needs_provision:
                        sea_vios_from_live_topo = host.find_vio_server(
                            sea.vio_server.lpar_id)

                        # If we got a VIOS back from live topo, check its state
                        if sea_vios_from_live_topo:
                            rmc = sea_vios_from_live_topo.rmc_state.lower()
                            if rmc == 'busy':
                                LOG.warn(_('RMC busy on lpar %(lpid)s') %
                                         {'lpid': sea.vio_server.lpar_id})
                                raise excp.IBMPowerVMRMCBusy(
                                    host=host_display_name,
                                    lpar_id=sea.vio_server.lpar_id)
                            else:
                                LOG.warn(_('RMC %(rmc)s on lpar %(lid)s' %
                                           {'rmc': rmc,
                                            'lid': sea.vio_server.lpar_id}))
                                raise excp.IBMPowerVMRMCDown(
                                    host=host_display_name,
                                    lpar_id=sea.vio_server.lpar_id)
                        else:
                            # The target SEA's vios wasn't found in the live
                            # topo at all.  Probably can't happen, but we'd
                            # better check anyways.
                            LOG.error(_('LPAR %s not found in live topo'),
                                      sea.vio_server.lpar_id)
                            raise excp.IBMPowerVMValidSEANotFound(
                                host=host_display_name)

                return sea.primary_vea.vswitch_name, vid, live_sea,\
                    needs_provision
        except excp.IBMPowerVMValidSEANotFound:
            raise
        except excp.IBMPowerVMNetworkNotValidForHost:
            raise
        except excp.IBMPowerVMRMCDown:
            raise
        except excp.IBMPowerVMRMCBusy:
            raise
        except Exception:
            ras.trace(LOG, __name__, ras.TRACE_EXCEPTION,
                      ras.msg('error', 'VIF_NOVLANID'))
            raise excp.exception.VirtualInterfaceCreateException()

    def _unplug_data_vlan_from_sea(self, host, sea, vlan_id,
                                   dom_factory=dom.DOM_Factory(), force=False):
        """
        Unplug a VM's VLAN from SEA. If this is the last reference
        to the given VLAN, the vlan id will be removed from the SEA
        to stop bridging. Unplug needs to be called after VM has been
        destroyed.

        :param host: The Host DOM object that IVM runs on
        :param sea: The SharedEthernetAdapter DOM object.
        :param vlan_id: VLAN id to unplug from SEA.
        :param dom_factory: Used to create DOM objects.
        :param force: Will tell us to unplug the VLAN from the SEA regardless
                      of finding client users of the VLAN.
        """
        ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG, "Enter")

        try:
            # Get this SEA's owning VIOS
            vios = sea.vio_server
            if not vios:
                ras.trace(LOG, __name__, ras.TRACE_DEBUG,
                          _('VIF unplug: ').join(ras.msg('error',
                                                         'VIF_NO_VIOS') %
                                                 {'vlanid': vlan_id}))
                return

            # If the VLAN ID is in the reserved_vlans list, then we do not want
            # to do anything
            vswitch = sea.get_primary_vea().vswitch_name
            if vios.get_reserved_vlanids(vswitch).count(vlan_id) > 0:
                ras.trace(LOG, __name__, ras.TRACE_DEBUG,
                          'Skipping unplug of reserved vlanid %d' % vlan_id)
                return

            if not utils.is_valid_vlan_id(vlan_id):
                ras.trace(LOG, __name__, ras.TRACE_DEBUG,
                          _('VIF unplug: ').join(ras.msg('error',
                                                         'VLAN_OUTOFRANGE') %
                                                 {'vlanid': vlan_id}))
                return

            # Get the VEA slot that bridges this vlan_id
            # output looks like the following:
            # lparid,slot,port_vlan_id,is_trunk,ieee_virtual_eth,addl_vlan_ids
            # [1,13,4093,1,1,"201,207"],
            port_vlan_id, num_users = \
                self._find_vlan_users(vios, vlan_id,
                                      sea.get_primary_vea().vswitch_name)

            if num_users > 1:
                if force:
                    # Continue on
                    ras.trace(LOG, __name__, ras.TRACE_DEBUG,
                              'Forcing unplug of vlan id %d' % vlan_id)
                else:
                    # other VMs are still using the vlan. Do nothing
                    ras.trace(LOG, __name__, ras.TRACE_DEBUG,
                              'Exit: VLAN %d is still in use, not removed' %
                              vlan_id)
                    return
            elif num_users == 0:
                # Didn't find the vlan on ANY veth.  This could be because
                # another unplug already deleted the same vlanid while we
                # were waiting to get the lock, or it could be some invalid
                # configuration.  Either way, we don't want to fail the
                # whole operation because of it.  Just return.
                ras.trace(LOG, __name__, ras.TRACE_DEBUG,
                          ras.msg('error', 'SEA_VIDNOTFOUND') %
                          {'vid': vlan_id})
                return

            # only the VIO's VEA left. It is the VEA that bridges the vlan
            sea_dev = self._find_sea_by_port_vlan_id(vios=vios,
                                                     port_vlan_id=port_vlan_id,
                                                     vswitch=vswitch)

            if port_vlan_id == vlan_id:
                # For the VM data VLAN that is bridged by the SEA's
                # pvid adapter's pvid vlan, it cannot be removed.
                # just return.
                ras.trace(LOG, __name__, ras.TRACE_DEBUG,
                          ('Exit: The VLAN %(vlanid)d to unplug matches '
                           'SEA(%(devname)s) pvid vlan, leave it '
                           'configured') %
                          {'vlanid': vlan_id, 'devname': sea_dev.name})
                return
            else:
                if sea_dev is not None:
                    # At this point, modifications to the system will be made.
                    self._invalidate_topo_cache()

                    #vlan_id is in addl_vlan_ids list
                    vea_dev = sea_dev.get_vea_by_vlan_id(vlan_id)

                    self._rm_vlan_from_existing_vea(host=host,
                                                    vios=vios,
                                                    vlan_id=vlan_id,
                                                    vea_dev=vea_dev,
                                                    sea_dev=sea_dev)

                    # Give the vlan_id back to the available pool
                    host.available_vid_pool[vea_dev.vswitch_name]\
                        .append(vlan_id)

                    ras.trace(LOG, __name__, ras.TRACE_INFO,
                              _('Exit: VLAN %(vid)d successfully removed from '
                                'SEA %(devname)s') %
                              {'vid': vlan_id, 'devname': sea_dev.name})
                else:
                    ras.trace(LOG, __name__, ras.TRACE_WARNING,
                              _('Exit: Unable to remove VLAN %(vid)d because '
                                'no sea found ') %
                              {'vid': vlan_id})

            return
        except Exception:
            ras.trace(LOG, __name__, ras.TRACE_ERROR,
                      ras.msg('error', 'SEA_FAILTOUNPLUG') %
                      {'vid': vlan_id})
            ras.trace(LOG, __name__, ras.TRACE_ERROR, traceback.format_exc())

    def _plug_data_vlan_into_sea(self, host, sea_dev, vlan_id,
                                 dom_factory=dom.DOM_Factory()):
        """
        A private method that tries to add a vlan to a configured
        SEA device. It does nothing if the vlan has been bridged by
        the SEA.

        :param host: The Host DOM object that IVM runs on.
        :param sea_dev: The SharedEthernetAdapter DOM object.
        :param vlan_id: An integer VLAN id to add
        :param dom_factory: Used to create DOM objects.
        """
        ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG, "Enter")

        # Validate that an SEA was passed in
        if not sea_dev or not isinstance(sea_dev, dom.SharedEthernetAdapter):
            raise excp.IBMPowerVMInvalidSEAConfig()

        # Get this SEA's owning VIOS
        vios = sea_dev.vio_server
        if not vios:
            ras.trace(LOG, __name__, ras.TRACE_DEBUG,
                      _('VIF plug: ').join(ras.msg('error', 'VIF_NO_VIOS') %
                                           {'vlanid': vlan_id}))
            raise excp.IBMPowerVMInvalidHostConfig(attr='vios')

        # Check for a valid vlan_id
        if not utils.is_valid_vlan_id(vlan_id):
            ras.trace(LOG, __name__, ras.TRACE_DEBUG,
                      _('VIF plug: ').join(ras.msg('error',
                                                   'VLAN_OUTOFRANGE') %
                                           {'vlanid': vlan_id}))
            raise excp.IBMPowerVMFailToAddDataVlan(data_vlan_id=vlan_id)

        # K2 has a restriction that vlan 1 can't be a tagged vlan (ie, it
        # can't be part of addl_vlan_ids).  So, only allow vlan 1 to be the
        # pvid of an SEA.  We'll do this for both HMC and IVM just to maintain
        # consistency even though IVM doesn't have the restriction.
        if vlan_id == 1 and sea_dev.get_primary_vea().pvid != 1:
            raise excp.IBMPowerVMTaggedVLAN1NotAllowed(
                host=self._get_host_display_name())

        try:
            if not isinstance(sea_dev, dom.SharedEthernetAdapter):
                ras.trace(LOG, __name__, ras.TRACE_ERROR,
                          ras.msg('error', 'OBJECT_INVALIDINST') %
                          {'classname': 'SeaDevice'})
                raise excp.IBMPowerVMInvalidSEAConfig()

            # If the VLAN ID is in the reserved_vlans list, then we do not want
            # to do anything since it's already "plugged"
            vswitch = sea_dev.get_primary_vea().vswitch_name
            if vios.get_reserved_vlanids(vswitch).count(vlan_id) > 0:
                return

            if sea_dev.get_primary_vea().pvid == vlan_id:
                # VM is going to be plugged into PVID vlan on the
                # pvid_adapter for SEA. Nothing needs to be done.
                # Just return.
                return

            try:
                vea = sea_dev.get_vea_by_vlan_id(vlan_id)
            except excp.IBMPowerVMVIDPVIDConflict as e:
                ras.trace(LOG, __name__, ras.TRACE_INFO,
                          _('VLAN ID %(vid)d is already configured as a pvid. '
                            'Reassigning pvid.') % {'vid': vlan_id})
                # Make sure to invalidate the topo cache if we are re-assigning
                # the pvid.
                self._invalidate_topo_cache()

                vea = e.vea
                self._reassign_arbitrary_pvid(host, vios, vea, sea_dev)
                vea = None

            if vea:
                # vid is already configured on one of the virt_adpaters.
                ras.trace(LOG, __name__, ras.TRACE_DEBUG,
                          'VID %(vid)d is already configured on VEA '
                          '%(devname)s.' % {'vid': vlan_id,
                                            'devname': vea.name})
                return

            #Now add the vid to one of the least used virt_adapters' vea
            vea = sea_dev.get_least_used_vea()

            # At this point, modifications to the system will be made.
            self._invalidate_topo_cache()

            if (not vea or
                ((len(vea.addl_vlan_ids) >= (utils.VEA_MAX_VLANS)) and
                 (len(sea_dev.additional_veas) < utils.SEA_MAX_VEAS))):

                # addl_vlan_ids list is empty,
                # or the least used VEA is full, just create a vea with
                # arbitrary pvid and put the new vlan as 8021Q tag into
                # virt_adapters list
                ras.trace(LOG, __name__, ras.TRACE_DEBUG,
                          'Add vlan %(vlan)d to new VEA on SEA '
                          '%(devname)s' %
                          {'vlan': vlan_id, 'devname': sea_dev.name})
                self._add_vlan_to_new_vea(host=host,
                                          vios=vios,
                                          vlan_id=vlan_id,
                                          sea_dev=sea_dev,
                                          dom_factory=dom_factory)

            elif (len(vea.addl_vlan_ids) < (utils.VEA_MAX_VLANS) and
                  len(sea_dev.additional_veas) < utils.SEA_MAX_VEAS):

                # the VEA found still has empty slots, use it
                ras.trace(LOG, __name__, ras.TRACE_DEBUG,
                          'Add vlan %(vlan)d to existing VEA %(veaname)s on '
                          'SEA %(seaname)s' % {'vlan': vlan_id,
                                               'veaname': vea.name,
                                               'seaname': sea_dev.name})

                self._add_vlan_to_existing_vea(host=host,
                                               vios=vios,
                                               vlan_id=vlan_id,
                                               vea_dev=vea,
                                               sea_dev=sea_dev)

            else:
                # The 15 VEAs per SEA Paxes limit has been reached.
                ras.trace(LOG, __name__, ras.TRACE_ERROR,
                          ras.msg('error', 'SEA_FULL') %
                          {'seaname': sea_dev.name, 'vid': vlan_id})
                raise excp.IBMPowerVMMaximumVEAsPerSEA()

            ras.trace(LOG, __name__, ras.TRACE_INFO,
                      _('Exit: vlan %(vlanid)d successfully plugged into SEA '
                        '%(devname)s') %
                      {'vlanid': vlan_id, 'devname': sea_dev.name})
        except Exception as e:
            ras.trace(LOG, __name__, ras.TRACE_ERROR,
                      ras.msg('error', 'SEA_FAILTOPLUG') %
                      {'vid': vlan_id})
            ras.trace(LOG, __name__, ras.TRACE_ERROR, traceback.format_exc())

            raise

    def _check_and_correct_vlan_config(self, vlan, dom_factory, host_dom,
                                       sea_to_plug):
        """
        This method is used to check if the VLAN requested for a plug operation
        is present in another SEA within the host than the one which has been
        requested. If it is on another SEA, we raise an ERROR if it's on the
        primary VEA. If it's on the secondary VEA, we unplug and then proceed
        with the plug.

        :param vlan: Requested VLAN id to Plug.
        :param dom_factory: The DOM Factory object
        :param host_dom: The host DOM object
        :param sea_to_plug: The SEA on which the plug is requested.
        """
        # sea_to_plug could potentially be None if RMC is down (since it came
        # from the live topology).  If this is the case, we can't do any
        # configuration corrections and will need to leave this for next time.
        # The only way we could get here like this is if RMC was down but
        # _parse_vif determined needs_provision=False
        if sea_to_plug is None:
            LOG.debug('_check_and_correct_vlan_config received None for '
                      'sea_to_plug')
            return

        # Inspect all the primary seas for the given host to see if the vlan
        # was moved to some other SEA chain.
        vswitch = sea_to_plug.get_primary_vea().vswitch_name
        vlan_pri_sea = None
        vlan_sec_sea = None
        for sea in host_dom.find_all_primary_seas():
            # Check if the VLAN is present on the primary VEA of another SEA.
            if sea.get_primary_vea().is_vlan_configured(vlan, vswitch,
                                                        throw_error=False):
                vlan_pri_sea = sea
                break
            # Check if this is present in any of the secondary VEAs
            for vea in sea.additional_veas:
                if vea.is_vlan_configured(vlan, vswitch, throw_error=False):
                    vlan_sec_sea = sea
                    break
            if vlan_sec_sea is not None:
                break
        if vlan_pri_sea and vlan_pri_sea != sea_to_plug:
            # If it's on the primary VEA of a different SEA. We should
            ras.trace(LOG, __name__, ras.TRACE_WARNING,
                      _('Potential conflict detected during a plug operation. '
                        'Attempting an unplug on SEA: %(sea)s for VLAN id: '
                        '%(vid)s to correct the state of the system before a '
                        'plug') %
                      {'sea': vlan_pri_sea.name,
                       'vid': vlan})
            raise excp.IBMPowerVMSEANotValidForPlug(sea=sea_to_plug,
                                                    host=host_dom.host_name,
                                                    sea_alt=vlan_pri_sea.name)
        if vlan_sec_sea and vlan_sec_sea != sea_to_plug:
            # If the vlan is in the secondary VEA of another SEA than
            # the one in question. Unplug that from the other SEA.
            self._unplug_data_vlan_from_sea(host=host_dom, sea=vlan_sec_sea,
                                            vlan_id=vlan,
                                            dom_factory=dom_factory)

    def _reassign_arbitrary_pvid(self, host, vios, vea_dev, sea_dev):
        """
        Will take the passed in vea and reassign it a new, unused, arbitrary
        pvid.  This will also put the old pvid back on the list of available
        vlan ids, which means the invoker of this function is free to use it.

        :param host: The Host DOM object that IVM is running on
        :param vios: The VioServer DOM object that IVM is running on
        :param vea_dev: VEA whose pvid needs to be reassigned
        :param sea_dev: SEA the VEA is attached to
        """
        ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG, "Enter")

        if not vea_dev or not isinstance(vea_dev, dom.VirtualEthernetAdapter):
            raise excp.IBMPowerVMInvalidVETHConfig(attr='not_vea_type')
        if not sea_dev or not isinstance(sea_dev, dom.SharedEthernetAdapter):
            raise excp.IBMPowerVMInvalidSEAConfig()

        # Capture the original pvid
        old_pvid = vea_dev.pvid

        # Grab a new, unused arbitrary pvid and assign it to the vea
        try:
            new_pvid = host.available_vid_pool[vea_dev.vswitch_name].pop()
        except IndexError:
            ras.trace(LOG, __name__, ras.TRACE_ERROR,
                      ras.msg('error', 'SEA_OUTOFVIDPOOL'))
            raise excp.IBMPowerVMOutOfAvailableVIDPool()

        # Update the veth device on the VIOS
        ras.trace(LOG, __name__, ras.TRACE_DEBUG,
                  'Reassigning pvid using new value of %(new_pvid)d.' %
                  {'new_pvid': new_pvid})
        self._update_virt_adapters_with_vea(host, vios, vea_dev, sea_dev, True,
                                            new_pvid)

        # Return the original pvid to the available list
        ras.trace(LOG, __name__, ras.TRACE_DEBUG,
                  'Returning %(old_pvid)d to the available list.' %
                  {'old_pvid': old_pvid})
        host.available_vid_pool[vea_dev.vswitch_name].append(old_pvid)

        ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG, "Exit")

    def _find_sea_by_port_vlan_id(self, vios, port_vlan_id=1, vswitch=None):
        """
        Fetch the SeaDevice object by the untagged port vlan id.
        Needs to check both sea pvid adapter's port vlan id and
        all the virt_adapter's port vlan id

        :param vios: The VioServer DOM object
        :param port_vlan_id: The untagged port vlan id(integer) to seach on
        :param vswitch: vswitch to check SEAs on
        """
        ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG, "Enter")

        for sea_dev in vios.get_shared_ethernet_adapters():
            if vswitch is not None and \
                    sea_dev.get_primary_vea().vswitch_name != vswitch:
                continue
            if sea_dev.get_primary_vea().pvid == port_vlan_id:
                return sea_dev
            if len(sea_dev.additional_veas) > 0:
                for vea in sea_dev.additional_veas:
                    if vea.pvid == port_vlan_id:
                        return sea_dev
        return

    def _add_vlan_to_existing_vea(self, host, vios, vlan_id, vea_dev, sea_dev):
        """
        Add a VLAN id to an existing VeaDevice object.

        :param host: The Host DOM object that IVM runs on
        :param vios: The VioServer DOM object that IVM runs on
        :param vlan_id: VLAN id to add.
        :param vea_dev: VeaDevice object
        :param sea_dev: SeaDevice object
        """
        ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG, "Enter")

        if not vea_dev or not isinstance(vea_dev, dom.VirtualEthernetAdapter):
            raise excp.IBMPowerVMInvalidVETHConfig(attr='not_vea_type')
        if not sea_dev or not isinstance(sea_dev, dom.SharedEthernetAdapter):
            raise excp.IBMPowerVMInvalidSEAConfig()

        # We're about to use vlan_id, so remove it from the list of
        # available vlan ids
        vswitch_name = vea_dev.vswitch_name
        if host.available_vid_pool[vswitch_name].count(vlan_id) > 0:
            host.available_vid_pool[vswitch_name].remove(vlan_id)
        else:
            # vlan_id is not in the available list, which means it's
            # used already.  Throw an exception.
            ras.trace(LOG, __name__, ras.TRACE_ERROR,
                      ras.msg('error', 'VLAN_NOT_AVAIL') %
                      {'vlan_id': str(vlan_id)})
            raise excp.IBMPowerVMVlanNotAvailable()

        vea_dev.add_vea_vlan_tag(vlan_id)

        self._update_virt_adapters_with_vea(host=host,
                                            vios=vios,
                                            vea_dev=vea_dev,
                                            sea_dev=sea_dev,
                                            isupdate=True)

        ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG, "Exit")

    def _rm_vlan_from_existing_vea(self, host, vios, vlan_id, vea_dev,
                                   sea_dev):
        """
        Remove a VLAN ID from an existing VEA configured on SEA.

        :param host: The Host DOM object that IVM is running on
        :param vlan_id: VLAN id to remove from VEA
        :param ved_dev: VeaDevice object
        :param sea_dev: SeaDevice object
        """
        ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG, "Enter")

        if not vea_dev or not isinstance(vea_dev, dom.VirtualEthernetAdapter):
            raise excp.IBMPowerVMInvalidVETHConfig(attr='not_vea_type')
        if not sea_dev or not isinstance(sea_dev, dom.SharedEthernetAdapter):
            raise excp.IBMPowerVMInvalidSEAConfig()

        # remove vlan tag from add_vlan_ids list for VEA.
        vea_dev.addl_vlan_ids.remove(vlan_id)

        self._update_virt_adapters_with_vea(host=host,
                                            vios=vios,
                                            vea_dev=vea_dev,
                                            sea_dev=sea_dev,
                                            isupdate=True)
        ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG, "Exit")

    def _add_vlan_to_new_vea(self, host, vios, vlan_id, sea_dev,
                             dom_factory=dom.DOM_Factory()):
        """
        Add a VLAN id to a new VEA and configure it on the SEA

        :param host: The Host DOM object that VIOS runs on
        :param vios: The VioServer DOM object that VIOS runs on
        :param vlan_id: vlan id to configure
        :param sea_dev: DOM SharedEthernetAdapter object
        :param dom_factory: Used to create DOM objects.
        """
        ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG, "Enter")

        if (not sea_dev or
                not isinstance(sea_dev, dom.SharedEthernetAdapter) or
                not utils.is_valid_vlan_id(vlan_id) or
                vios.lpar_id < 1):
            ras.trace(LOG, __name__, ras.TRACE_ERROR,
                      ras.msg('error', 'SEA_INVALIDSTATE'))
            raise excp.IBMPowerVMInvalidSEAConfig()

        # Be sure there's an available slot
        slotnum = self._find_first_avail_slot(vios)
        if not slotnum:
            raise excp.IBMPowerVMMaximumVEAsPerSEA()

        # We're about to use vlan_id, so remove it from the list of available
        # vlan ids
        vswitch_name = sea_dev.get_primary_vea().vswitch_name
        if host.available_vid_pool[vswitch_name].count(vlan_id) > 0:
            host.available_vid_pool[vswitch_name].remove(vlan_id)
        else:
            # vlan_id is not in the available list, which means it's
            # used already.  Throw an exception.
            ras.trace(LOG, __name__, ras.TRACE_ERROR,
                      ras.msg('error', 'VLAN_NOT_AVAIL') %
                      {'vlan_id': str(vlan_id)})
            raise excp.IBMPowerVMVlanNotAvailable()

        try:
            arbitrary_pvid = host.available_vid_pool[vswitch_name].pop()
        except IndexError:
            ras.trace(LOG, __name__, ras.TRACE_ERROR,
                      ras.msg('error', 'SEA_OUTOFVIDPOOL'))

            raise excp.IBMPowerVMOutOfAvailableVIDPool()

        # The arbitrary pvid will be used as pvid VEA's port
        # vlan id during VEA creation.
        port_vlan_id = arbitrary_pvid

        # Create the VEA on the system.  Note, this could raise an exception
        # if something goes wrong.  We'll just let that bubble up.
        vea_devname, snum = self._create_vea_on_vios(vios, sea_dev, slotnum,
                                                     port_vlan_id, [vlan_id])

        # If this was HMC, they returned the newly created slot
        if snum:
            slotnum = snum

        # Create the VirtualEthernetAdapter DOM object
        vea_dev = dom_factory.create_vea(
            vea_devname,
            vios,
            slotnum,
            port_vlan_id,
            True,
            1,
            'Available',
            True,
            sea_dev.get_primary_vea().vswitch_name,
            [vlan_id])

        # now insert the new VEA to the SEA's virt_adapters list on VIOS
        # and update SeaDevice object if successful
        self._update_virt_adapters_with_vea(host=host,
                                            vios=vios,
                                            vea_dev=vea_dev,
                                            sea_dev=sea_dev,
                                            isupdate=False)
        ras.trace(LOG, __name__, ras.TRACE_DEBUG,
                  'Successfully add vlan %(vlanid)d to VEA %(devname)s' %
                  {'vlanid': vlan_id, 'devname': vea_dev.name})

    def _update_virt_adapters_with_vea(self, host, vios, vea_dev, sea_dev,
                                       isupdate, new_pvid=None):
        """
        This function handles updating an existing VEA already
        configured on the SEA or adding a new VEA to SEA.

        It requires that VEA has been updated with the required VLAN
        before this function is called. This function updates the
        PowerVM SEA device.

        :param host: The Host DOM object that VIOS runs on
        :param vios: The Vios DOM object that VIOS runs on
        :param vea_dev: VeaDevice object
        :param sea_dev: SeaDevice object
        :param isupdate: Whether this is a VEA update request.  This is mostly
                         interesting on IVM since you can't dynamically update
                         VEAs on IVM.  HMC allows dynamic updating, so this
                         param will likely be ignored for HMC.
        :param new_pvid: Only required if changing the PVID of the VEA.
                         Represents the VLAN ID of the VEA after the operation.
                         The vea_dev passed in should have the original VLAN.
        """
        ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG, "Enter")

        if not vea_dev or not isinstance(vea_dev, dom.VirtualEthernetAdapter):
            raise excp.IBMPowerVMInvalidVETHConfig(attr='not_vea_type')
        if not sea_dev or not isinstance(sea_dev, dom.SharedEthernetAdapter):
            raise excp.IBMPowerVMInvalidSEAConfig()

        virt_adpts_list = []
        if len(sea_dev.additional_veas) > 0:
            for dev in sea_dev.additional_veas:
                virt_adpts_list.append(dev.name.strip())

        # Are we updating an existing VEA already on a SEA?
        if isupdate:
            # If the VIOS supports dynamically updating VEAs (only HMC can do
            # this) AND there will be addl_vlan_ids left on the VEA, do the
            # update dynamically.  If there are no addl_vlan_ids left, then
            # we don't need this VEA any more, so we want to fall into the else
            # leg where the VEA will just be removed.  Since we don't operate
            # on primary VEAs, we don't need to worry about this code removing
            # a primary VEA, which would have no addl_vlan_ids either.
            if self._supports_dynamic_update(vios) and\
                    len(vea_dev.addl_vlan_ids) > 0 and new_pvid is None:
                self._dynamic_update_vea(vios, sea_dev, vea_dev)
            else:
                # IVM doesn't support dynamic adding VID to the addl_vlan_ids
                # list, nor do older firmware versions of HMC, so it has to be
                # done in two steps: remove the original addl_vlan_ids VEA and
                # add it back with additional vid added to addl_vlan_ids list.
                ras.trace(LOG, __name__, ras.TRACE_DEBUG,
                          'update virt_adapters')

                sea_dev.remove_vea_from_sea(vea_dev)

                # Remove VEA from local adapter list
                if virt_adpts_list.count(vea_dev.name) > 0:
                    virt_adpts_list.remove(vea_dev.name)
                else:
                    ras.trace(LOG, __name__, ras.TRACE_ERROR,
                              ras.msg('error', 'SEA_VEANOTFOUND') %
                              {'veaname': vea_dev.name,
                               'seaname': sea_dev.name})
                    raise excp.IBMPowerVMInvalidSEAConfig()

                # Remove the VEA from the system
                self._delete_vea_on_vios(vios, vea_dev, sea_dev,
                                         virt_adpts_list)

                # If there were no addl_vlan_ids on the VEA, then the vlan
                # we're unplugging is the pvid of the VEA.  Thus, we're just
                # removing the old VEA and not adding a new.
                if len(vea_dev.addl_vlan_ids) == 0:
                    # addl_vlan_ids is empty, this gets VEA removed and
                    # need to return port_vlan_id back to the available pool
                    vswitch_name = vea_dev.vswitch_name
                    host.available_vid_pool[vswitch_name].append(vea_dev.pvid)

                    ras.trace(LOG, __name__, ras.TRACE_DEBUG,
                              'virt_adapter %(veaname)s removed from SEA '
                              '%(seaname)s' %
                              {'veaname': vea_dev.name,
                               'seaname': sea_dev.name})
                    return

                # If we are to update the PVID, set it on the vea_dev prior to
                # the creation of the device.
                if new_pvid is not None:
                    vea_dev.pvid = new_pvid

                new_veadevname, snum = self._create_vea_on_vios(vios, sea_dev,
                                                                vea_dev.slot,
                                                                vea_dev.pvid,
                                                                vea_dev.
                                                                addl_vlan_ids)

                # update existing VEA's devname in case it was changed
                sea_dev.remove_vea_from_sea(vea_dev)
                vea_dev.name = new_veadevname
                # If we got a slot number back (ie, HMC), set it in the dev.
                if snum:
                    vea_dev.slot = snum
                sea_dev.add_vea_to_sea(vea_dev)

                # Add the VEA to the local adapter list
                virt_adpts_list.append(vea_dev.name)

                # Attach VEA to SEA on the system
                self._update_sea(sea_dev, virt_adpts_list)

                ras.trace(LOG, __name__, ras.TRACE_DEBUG,
                          'Successfully updated VEA %(veaname)s on SEA '
                          '%(seaname)s' %
                          {'veaname': vea_dev.name, 'seaname': sea_dev.name})
        else:
            # Just adding a brand new VEA to a SEA.  The VEA is actually
            # already created, it just needs to be added to the SEA.

            # Add the VEA to the local adapter list
            virt_adpts_list.append(vea_dev.name)

            # Attach VEA to SEA on the system
            self._update_sea(sea_dev, virt_adpts_list)

            # Update the DOM with the new setup
            sea_dev.add_vea_to_sea(vea=vea_dev)
            vios.add_adapter(sea_dev)
            host.vio_servers = [vios]

            ras.trace(LOG, __name__, ras.TRACE_DEBUG,
                      'Successfully add new VEA %(veaname)s to SEA '
                      '%(seaname)s' %
                      {'veaname': vea_dev.name, 'seaname': sea_dev.name})

    """
    ALL METHODS BELOW MUST BE IMPLEMENTED BY SUBCLASSES
    """

    def _dynamic_update_vea(self, vios, sea, vea):
        """
        This method will only work if _supports_dynamic_update returns True.

        Will dyanmically update VLANs on an existing VEA.

        :param vios: VioServer DOM object representing VIOS this is being
                     created on.
        :param sea: SharedEthernetAdapter DOM object that owns the VEA
        :param vea: VirtualEthernetAdapter DOM object that represents the
                    element on the system to update.  Should have the updated
                    information about the VLANs on it already.
        """
        raise NotImplementedError('_dynamic_update_vea not implemented')

    def _create_vea_on_vios(self, vios, sea, slotnum, port_vlan_id,
                            addl_vlan_ids):
        """
        This method will create the 802.1Q VirtualEthernetAdapter on the
        VIOS and return the device name and slot number of the newly created
        adapter.  A IBMPowerVMFailToAddDataVlan should be raised if unable to
        create the new VEA.

        NOTE: On IVM, this method is told what slot to use and will NOT return
              a slot number.  On HMC, a slotnumber is not needed because HMC
              will determine this for us.  Thus, the slot number needs to be
              returned so the VEA DOM object can be created with the right
              slot number.

        :param vios: VioServer DOM object representing VIOS this is being
                     created on.
        :param sea: SharedEthernetAdapter DOM object that owns the new VEA
        :param slotnum: Virtual slot number to create the new VEA in.
        :param port_vlan_id: pvid to set on the new VEA
        :param addl_vlan_ids: Additional vlan ids to set on the new VEA
        :returns vea_devname: Device name of the newly created VEA
        :returns slot_number: On HMC, slot number the VEA was created in.  On
                              IVM, None.
        """
        raise NotImplementedError('_create_vea_on_vios not implemented')

    def _delete_vea_on_vios(self, vios, vea, sea, virt_adpts_list):
        """
        This method will remove the given VEA from the given SEA on the
        system and delete it.

        :param vios: VioServer DOM object representing VIOS this is being
                     removed from.
        :param vea_dev: VirtualEthernetAdapter DOM object
        :param sea_dev: SharedEthernetAdapter DOM object
        :param virt_adpts_list: List of virtual adapters that should remain
                                on the SEA
        """
        raise NotImplementedError('_delete_vea_on_vios not implemented')

    def _find_first_avail_slot(self, vios):
        """
        This method will return the first available virtual slot on the VIOS.
        To be used when creating a VEA.

        :param vios: VioServer DOM object representing VIOS we're working with.
        :returns slotnum: Virtual slot number of the first available slot
        """
        raise NotImplementedError('_find_first_avail_slot not implemented')

    def _find_vlan_users(self, vios, vlanid, vswitch=None):
        """
        This method will search for the given vlanid on the system and return
        two things.  First, the pvid of the SEA on the VIOS containing the
        vlanid.  Second, the number of users of that vlanid total (ie, client
        LPARs in addition to VIOS).  This is needed for unplug() to determine
        if we're unplugging the last client from a vlan or not.

        :param vios: HMC needs this since they support multi-VIOS
        :param vlanid: VLANID to search for
        :param vswitch: vswitch name associated with this vlanid.
        :returns pvid: port vlanid of the SEA on the VIOS containing the
                       vlanid.  Will be 0 if not found.
        :returns num_users: Total number of adapters found with this vlanid.
        """
        raise NotImplementedError('_find_vlan_users not implemented')

    def _supports_dynamic_update(self, vios=None):
        """
        This method returns whether the VIOS supports dynamically updating VLAN
        ids on VEAs or not.  On IVM, this will always return false.  On HMC,
        it depends on the system's capabilities.

        :param vios: VioServer DOM object representing VIOS running against
        :returns boolean: True if dynamic updates supported, False otherwise
        """
        raise NotImplementedError('_supports_dynamic_update not implemented')

    def _update_sea(self, sea_dev, virt_adpts_list):
        """
        This method will update the passed in SEA to include all virtual
        adapters passed in.  NOTE: This does not touch the SEA's primary VEA.

        :param sea_dev: SharedEthernetAdapter DOM object representing SEA to
                        be updated on the system.
        :param virt_adpts_list: List of virtual adapters (VEAs) to be attached
                                to the SEA when done.
        """
        raise NotImplementedError('_update_sea not implemented')
