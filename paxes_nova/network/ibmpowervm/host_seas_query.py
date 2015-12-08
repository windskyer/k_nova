#
# =================================================================
# =================================================================

"""
This is a Task Module that binds the host_seas Network REST API with
the DOM Model by creating AOMs that are used as view objects for
the user.
"""
from nova import db
from nova import exception
from nova.openstack.common import lockutils
from nova.openstack.common import log as logging
from nova.db.sqlalchemy import api as db_session
from oslo.config import cfg
from paxes_nova.db import api as dom_api
from paxes_nova.db.network import models as dom_model
from paxes_nova.network.common import exception as network_exception
from paxes_nova.network.common.api_suppress_mixin import NetworkAPISuppressor
from paxes_nova.virt.ibmpowervm.vif.common import ras
from nova.network import neutronv2
from nova import network

from paxes_nova import _

LOG = logging.getLogger(__name__)
DO_NOT_USE_STR = "do-not-use"
CONF = cfg.CONF


class HostSeasQuery(NetworkAPISuppressor):
    """
    This class handles processing for the host-seas rest api call in PowerVC.
    """

    def __init__(self):
        super(HostSeasQuery, self).__init__()
        self._nova_net_api = None

    @lockutils.synchronized('host_seas', 'host-seas-')
    def get_host_seas(self, context, host_name=None, vswitch=None, vlan=None,
                      net_id=None, session=None):
        """
        This method will return a dictionary of data that represents the
        Shared Ethernet Adapter information for a given host or a set of hosts.
        If vlan or net_id are passed in, then only SEAs that are valid for the
        given vlan or net_id will be returned.

        :param context: The context for the request.
        :param host_name: The identifying name of the host to request the data
                          If None is passed in, a dictionary representing all
                          of the managed hosts will be found.
        :param vswitch: The vswitch that should be used to help identify the
                        default adapter.  If set to None (the default value),
                        only the VLAN ID will be used.  ie, all vswitches will
                        be candidates.
        :param vlan: The vlan that should be used to help identify the default
                     and candidate adapters.  If set to None (the default
                     value), a VLAN ID of 1 will be used.  This parameter will
                     be ignored if net_id is passed in.
        :param net_id: The network UUID for an existing neutron network.  If
                       this is passed in, then vlan will be ignored and the
                       vlan to use will be obtained from the neutron network.
        :param session: session to be used for db access

        :return: A dictionary of host level Shared Ethernet Adapter data.  Ex:
        {
         "host-seas": [
            {
             "host_name": "host1",
             "adapters": [
                {
                 "default": false,
                 "sea_name": "ent11",
                 "vswitch": "ETHERNET0",
                 "lpar_id": 1,
                 "ha_lpar_id": 2,
                 "ha_mode": "enabled",
                 "pvid": 1,
                 "state": "Available",
                 "ha_state": "Available",
                 "lpar_name": "10-23C2P",
                 "ha_lpar_name": "10-24C2P",
                 "ha_sea": "ent21"
                },
                {
                 "default": false,
                 "sea_name": "ent12",
                 "vswitch": "ETHERNET0",
                 "lpar_id": 1,
                 "ha_lpar_id": 2,
                 "ha_mode": "enabled",
                 "pvid": 2,
                 "state": "Available",
                 "ha_state": "Available",
                 "lpar_name": "10-23C2P",
                 "ha_lpar_name": "10-24C2P",
                 "ha_sea": "ent22"
                }
             ]
            },
            {
             "host_name": "host2",
             "adapters": [
                {
                 "default": true,
                 "sea_name": "ent5",
                 "vswitch": "ETHERNET0",
                 "lpar_id": 1,
                 "ha_lpar_id": null,
                 "ha_mode": "disabled",
                 "pvid": 1,
                 "state": "Available",
                 "ha_state": null,
                 "lpar_name": "15-34B9Z",
                 "ha_lpar_name": null,
                 "ha_sea": null
                }
             ]
            }
         ]
        }
        """
        ras.function_tracepoint(LOG, __name__, ras.TRACE_INFO,
                                ras.vif_get_msg('info', 'GET_HOST_SEAS') %
                                {'host': host_name, 'vlan': vlan,
                                 'vswitch': vswitch, 'net_id': net_id})

        # This class should only be used in PowerVM environments
        self.raise_if_not_powervm()

        if session is None:
            session = db_session.get_session()

        hosts = self._find_all_host_names(context)
        if host_name:
            if host_name in hosts:
                # Should make it empty before we add for below for loop
                # We want to be specific that now it has only one host
                hosts = [host_name]

            else:
                msg = (ras.vif_get_msg
                      ('info', 'HOST_NOT_FOUND') %
                       {'hostid': host_name})
                ras.function_tracepoint(
                    LOG, __name__, ras.TRACE_INFO, msg)
                raise exception.ComputeHostNotFound(host=host_name)

        # Validate that network exists
        if net_id or net_id == '':
            try:
                network.API().get(context, net_id)
            except:
                raise exception.InvalidID(id=net_id)

        host_dict_list = []

        with session.begin():

            ports = None
            if net_id:
                # Performance optimization -- read network ports before loop
                # because this operation is expensive
                search_opts = {'network_id': net_id}
                network_data = network.API().list_ports(context, **search_opts)
                ports = network_data.get('ports', [])

            for host in hosts:
                resp = self._get_specific_host_seas(context, host, vswitch,
                                                    vlan, net_id, session,
                                                    ports)
                host_dict_list.append(resp)

        return {
            'host-seas': host_dict_list
        }

    def _determine_vlan(self, vlan, net_id, host, context):
        """
        Determines the VLAN that should be used.

        :param vlan: The passed in VLAN from the user.  May be None.
        :param net_id: The neutron network identifier.  May be None.
        :param host: The host being queried.  String value
        :param context: The context for neturon queries
        :return: The VLAN identifier to use.  If both parameters are none, then
                 None will be returned, indicating 'all seas' should be
                 returned
        """
        # This is the scenario where the user wants all SEAs.
        if vlan is None and net_id is None:
            return None

        # If a network id is passed in, we will override any VLAN info
        # with the info from the network ID.
        if net_id:
            msg = (ras.vif_get_msg('info', 'NET_ID_SUPPLIED') %
                   {'vlan_id': vlan, 'host_name': host, 'net_id': net_id})
            ras.function_tracepoint(LOG, __name__, ras.TRACE_INFO, msg)
            try:
                net_api = self._build_network_api()
                neutron_net = net_api.get(context, net_id)
            except neutronv2.exceptions.NeutronClientException as e:
                ras.function_tracepoint(LOG, __name__,
                                        ras.TRACE_ERROR, e.message)
                # We need to stop execution here. Since we didn't get
                # the client
                raise

            if neutron_net and neutron_net.\
                    get('provider:segmentation_id'):
                vlan = neutron_net.get('provider:segmentation_id', 1)
            elif neutron_net and neutron_net.get('vlan'):
                vlan = neutron_net.get('vlan', 1)
            else:
                msg = _("Couldn't retrieve VLAN associated with Net_id"
                        "setting default to 1")
                ras.function_tracepoint(LOG, __name__,
                                        ras.TRACE_WARNING, msg)
                vlan = 1

        # Return the passed in value, but make sure its an int
        return int(vlan)

    def _get_specific_host_seas(self, context, host, vswitch=None, vlan=None,
                                net_id=None, session=None, ports=None):
        """
        This method will return the SEA candidates for a given host, and only
        that host.

        The format of the response will be:
        {
         "host_name": "host2",
         "adapters": [
            {
             "default": true,
             "sea_name": "ent5",
             "vswitch": "ETHERNET0",
             "lpar_id": 1,
             "ha_lpar_id": null,
             "ha_mode": "disabled",
             "pvid": 1,
             "state": "Available",
             "ha_state": null,
             "lpar_name": "15-34B9Z",
             "ha_lpar_name": null,
             "ha_sea": null
            }
         ]
        }

        :param context: The context for the request.
        :param host: The host name (as a string)
        :param vswitch: The vswitch that should be used to help identify the
                        default adapter.  If set to None all of the vSwitches
                        will be utilized.
        :param vlan: The vlan that should be used to help identify the default
                     adapter.  If set to None (the default value), a VLAN ID of
                     1 will be used.
        :param net_id: The network UUID of a neutron Network. This is optional
        :param ports: An optional list of ports for the specified network.
        """
        # Build basic data to determine targets
        vio_servers = dom_api.vio_server_find_all(context, host, session)
        host_dom = dom_model.Host(host, vio_servers)
        vswitches = self._get_vswitches(host_dom.find_all_primary_seas())

        # If the network id was set, then we should always use that networks
        # vlan id instead of the passed in value.
        vlan = self._determine_vlan(vlan, net_id, host, context)

        # We need to determine if this network has any VMs on the host.  If so,
        # then we can't be set to Do not Use.  We also can't allow them to
        # change vSwitches.
        allow_do_not_use_option = False
        if net_id:
            # We only allow the do not use option if the VM count for this
            # network is 0.  Otherwise a VM is using it, and we can't flip
            # to do not use until all the VMs are done.
            vm_list = dom_api.instances_find(context, host, net_id, ports)
            allow_do_not_use_option = (len(vm_list) == 0)

            # As noted above...if the network association has VMs, then we
            # can't let the user change the vSwitch that the networks are on.
            # Therefore, set the specific_vswitch so that the candidate_sea
            # loop won't take any other vSwitches into account.
            if len(vm_list) > 0:
                net_assn = dom_api.network_association_find(context,
                                                            host_dom.host_name,
                                                            net_id, session)

                # If there is a network association, just override the vSwitch
                # list with the network associations vSwitch.  This limits it
                # to a single vSwitch search scope.
                if net_assn and net_assn.sea:
                    vswitches = [net_assn.sea.primary_vea.vswitch_name]
        else:
            # If there was not a network id, then we assume that this is a
            # new network and therefore do not use should always be returned.
            allow_do_not_use_option = True

        # Variable to store all candidate SEAs
        candidate_seas = []

        # Walk through the vswitches on this host and determine the valid
        # candidates (broken into pools defined by the vswitches).
        for vswitch_name in vswitches:
            # If the user passed in a vswitch, and we don't match, continue
            if vswitch is not None and vswitch != vswitch_name:
                continue

            # Extend the candidates
            candidate_seas.extend(self._get_candidate_seas_for_vswitch(
                host_dom, vswitch_name, vlan))

        # Now we need to find the default adapter...may be None, which
        # indicates that it is a do not use.
        default_sea = self._find_default_adapter(host_dom, candidate_seas,
                                                 net_id, vlan, context,
                                                 session)
        # If the default sea is not selected, and there's only one vswitch
        # we need to determine a default adapter for this VLAN and create
        # that relationship.
        if(default_sea is None and not allow_do_not_use_option and net_id):
            vswitch_with_vlan = []
            for vswitch in vswitches:
                sea = host_dom.find_sea_for_vlan_vswitch(vlan, vswitch)
                if sea:
                    vswitch_with_vlan.append(sea)
            # We would like to set this as the default since in this
            # present call to host-seas - we need to report a default
            if len(vswitch_with_vlan) == 1:
                default_sea = vswitch_with_vlan[0]
                dom_api.network_association_put_sea(context, host, net_id,
                                                    vswitch_with_vlan[0],
                                                    session)

        # Now, build the adapter list to return
        adapter_list = []
        if allow_do_not_use_option or len(vswitches) > 1:
            adapter_list.append(self._format_sea_to_dict_response
                                (host_dom,
                                 None,
                                 default_sea is None))
        if default_sea and default_sea not in candidate_seas:
            candidate_seas.append(default_sea)
        for sea in candidate_seas:
            adapter_list.append(self._format_sea_to_dict_response
                                (host_dom, sea,
                                 default_sea == sea))

        msg = (ras.vif_get_msg('info', 'HOST_SEAS_RETURN') %
               {'vlan': vlan,
                'host_name': host,
                'list': adapter_list})
        ras.function_tracepoint(
            LOG, __name__, ras.TRACE_INFO, msg)
        return {
            'host_name': host_dom.host_name,
            'adapters': adapter_list
        }

    def _get_candidate_seas_for_vswitch(self, host_dom, vswitch, vlan):
        """
        Returns the dictionary responses for a given vSwitch on the system.
        Will return a list of SEA DOM objects that are valid for the given
        vlan.

        :param host_dom: The Host DOM object for the element.
        :param vswitch: The name of the vSwitch
        :param vlan: Optional VLAN for the request.  May be set to None

        :return: Set of SEAs that are valid candidates.
        """

        # If the VLAN is specified, make sure that the VLAN is not on an orphan
        # VEA or the pvid of a control channel VEA.
        if vlan and vlan in host_dom.get_unusable_vlanids(vswitch):
            return []

        # Find all of the SEAs for this vSwitch
        all_seas = host_dom.find_all_primary_seas()
        specific_seas = []
        for sea in all_seas:
            # if the vswitches don't match, continue on
            if sea.primary_vea.vswitch_name != vswitch:
                continue

            # Need to check to see if the vlan passed in matches the pvid
            # or additional VLANs on the primary VEA.  If so, our only
            # candidate for this vSwitch is this SEA.
            if vlan and\
                    (sea.primary_vea.pvid == vlan or
                     vlan in sea.primary_vea.addl_vlan_ids):
                return [sea]

            # If they're asking for vlan 1, it can only be used if it's the
            # pvid of an SEA (K2 does not allow VLAN 1 to be tagged).
            # Therefore, if we get here with vlan 1, we know it wasn't the pvid
            # because that would have been caught above and we should skip
            # this SEA.  If no SEA's on this host are found with vlan 1, then
            # the candidates list returned will be empty, thus causing this
            # host to properly be marked do-not-use.
            if vlan != 1:
                specific_seas.append(sea)

        LOG.debug('_get_candidate_seas_for_vswitch is returning %s' %
                  specific_seas)

        return specific_seas

    def _get_vswitches(self, primary_seas_all):
        """
        Lists all of the unique vswitches from the SEAsx

        param: primary_seas_all: all the seas of a given vios

        return: a List of all the unique vSwitch anmes
        """
        vswitch_resp = []
        for sea in primary_seas_all:
            vswitch = sea.primary_vea.vswitch_name
            if vswitch not in vswitch_resp:
                vswitch_resp.append(vswitch)
        LOG.debug('_get_vswitches is returning %s' % vswitch_resp)
        return vswitch_resp

    def _find_default_adapter(self, host_dom, candidate_seas, net_id, vlan,
                              context, session):
        """
        Will return the adapter from 'candiate_seas' that is the appropriate
        default adapter.

        :param host_dom: The DOM for the Host
        :param candidate_seas: All of the candidate SEA DOM objects.
        :param net_id: The neutron network id (optional).
        :param vlan: The VLAN for the query.  Optional.
        :param context: The context for all queries
        :param session: The database session

        :return: The sea from the candidate_seas list that is the default.  If
                 None is returned, then 'do-not-use' is the default.
        """
        LOG.debug('_find_default_adapter starting..')
        # First check to see is if a network association exists already,
        # and if so, return that.
        if net_id:
            net_assn = dom_api.network_association_find(context,
                                                        host_dom.host_name,
                                                        net_id, session)
            if net_assn:
                # If the network association has 'none' set for the SEA, then
                # return None
                if net_assn.sea is None:
                    LOG.debug('returning None')
                    return None
                else:
                    # Find the corresponding SEA from our query and return that
                    for sea in candidate_seas:
                        if sea.name == net_assn.sea.name and\
                            sea.vio_server.lpar_id == \
                                net_assn.sea.vio_server.lpar_id:
                            LOG.debug('returning %s' % sea)
                            return sea

        # Next up, check to see if there is a peer!
        # A peer network is one that shares the same VLAN id as this one.
        # NOTE: This isn't a true peer as we're disregarding vswitch here.  We
        # just know by this point that no SEA was associated with the passed
        # in net_id, so we are free to pick any vswitch that fits the bill.
        peer_sea = self._find_peer_network_sea(context, host_dom.host_name,
                                               vlan, session)
        if peer_sea:
            # Find the corresponding SEA from our candidate list.
            for sea in candidate_seas:
                if sea.name == peer_sea.name and\
                        sea.vio_server.lpar_id == peer_sea.vio_server.lpar_id:
                    LOG.debug('returning %s' % sea)
                    return sea

        # Next up is to check the SEAs to see if any contains the VLAN passed
        # in.
        if vlan:
            for sea in candidate_seas:
                if sea.pvid == vlan or vlan in sea.additional_vlans():
                    LOG.debug('returning %s' % sea)
                    return sea

        # Lastly, we just have to return the lowest adapter...
        return self._find_default_with_no_vlan(candidate_seas)

    def _find_default_with_no_vlan(self, primary_seas):
        """
        This method finds the default adapter when there's no vlanid specified.
        The default SEA is the one that has the lowest pvid in all the primary
        seas.

        :param: primary_seas: This is a list of all primary_seas for a given
        host

        :return: A default adapter. Note there can be only one in this case?
        """
        lowest_sea = None
        # A None should never be returned however
        low = 4096
        for i in range(0, len(primary_seas)):
            # if the sea is not available - we need to find the next available
            if primary_seas[i].pvid < low and primary_seas[i].is_available():
                low = primary_seas[i].pvid
                lowest_sea = primary_seas[i]
                msg = (ras.vif_get_msg
                      ('info', 'LOWEST_PVID') % {'lowest_sea':
                                                 lowest_sea.name})
                ras.function_tracepoint(LOG, __name__, ras.TRACE_INFO,
                                        msg)
        # Let's say that none of the seas are available, in this case we pick
        # anyone and return
        if lowest_sea is None and len(primary_seas) >= 1:
            lowest_sea = primary_seas[0]
            LOG.info(_('None of the seas are in available state, picking %s'
                       'as default' % lowest_sea.name))
        return lowest_sea

    def _format_sea_to_dict_response(self, host, sea, default=False):
        """
        Will format a shared ethernet adapter to the corresponding REST API
        response dictionary.

        :param sea: The SharedEthernetAdapter to format
        :param host: The owning Host object for the SEA.
        :param default: If set to true, will set the default
                attribute of the SEA to True.  Defaults to False.
        """
        # Set a default HA attributes of none.
        ha_lpar_name = None
        ha_sea = None
        ha_mode = 'disabled'
        ha_state = None
        ha_lpar_id = None
        sea_name = DO_NOT_USE_STR
        vswitch = DO_NOT_USE_STR
        pri_lparid = 0
        pri_lpar_name = None
        pvid = None
        state = "Not-Applicable"
        if sea:
            primary_sea_vios = sea.vio_server
            pri_lparid = primary_sea_vios.lpar_id
            pri_lpar_name = primary_sea_vios.lpar_name
            vswitch = sea.get_primary_vea().vswitch_name
            sea_name = sea.name
            pvid = sea.pvid
            if sea.is_available():
                state = "available"
            else:
                state = "unavailable"
            sea_chain = host.find_sea_chain_for_sea(sea)
            if len(sea_chain) > 1:
                # Two or more in the SEA chain indicates HA mode
                ha_sea_obj = sea_chain[1]
                ha_vios = ha_sea_obj.vio_server

                ha_lpar_id = ha_vios.lpar_id
                ha_lpar_name = ha_vios.lpar_name
                ha_sea = ha_sea_obj.name
                ha_mode = 'enabled'
                if ha_sea_obj.is_available():
                    ha_state = "available"
                else:
                    ha_state = "unavailable"
        # Return the formated data back to the user.
        return {
            'default': default,
            'sea_name': sea_name,
            'vswitch': vswitch,
            'lpar_id': pri_lparid,
            'lpar_name': pri_lpar_name,
            'pvid': pvid,
            'state': state,
            'ha_lpar_id': ha_lpar_id,
            'ha_lpar_name': ha_lpar_name,
            'ha_sea': ha_sea,
            'ha_mode': ha_mode,
            'ha_state': ha_state
        }

    def _find_all_host_names(self, context):
        """
        This returns a list of compute nodes after querying the Nova DB

        :param context: A context object that is used to authorize the DB
                        access.
        :returns: A set of compute nodes.
        """
        compute_nodes = db.compute_node_get_all(context)
        return_nodes = []
        for compute in compute_nodes:
            if not compute['service']:
                ras.trace(LOG, __name__, ras.TRACE_WARNING,
                          _('No service for compute ID %s' % compute['id']))
                continue
            host_to_send = compute['service']['host']
            return_nodes.append(host_to_send)
        return return_nodes

    def _find_peer_network_sea(self, context, host_name, vlanid, session):
        """
        This method finds whether a given vlanid exists for a host network or
        not. If it finds that this VLAN is part of an existing network, it
        returns the corresponding SEA.

        :param context: The context used to call the dom API
        :param host_id: The host_id of the host for which the network
                        associations are fetched.
        :param vlanid: The vlan id for which the match is needed.
        :param session: The database session object.
        :returns: either an SEA or None
        """
        # The VLAN ID may be none here, indicating a pull for all SEAs.
        if vlanid is None:
            return None

        network_associations = dom_api.\
            network_association_find_all(context, host_name, session)
        # net_map should always be of size 1
        for network_association in network_associations:
            if network_association:
                try:
                    net_api = self._build_network_api()
                    neutron_net = net_api.get(context,
                                              network_association.
                                              neutron_net_id)
                except Exception as e:
                    # Will throw an exception if the neutron client could
                    # not be found
                    ras.trace(LOG, __name__, ras.TRACE_WARNING,
                              _('Neutron client not found for net_id %s' %
                                network_association.neutron_net_id))
            if 'neutron_net' in locals() and neutron_net:
                if neutron_net.get("provider:segmentation_id") == vlanid:
                    # There should be only one SEA per VLAN so we return
                    # as soon as we find one.
                    return network_association.sea
                if neutron_net.get("vlan") == vlanid:
                    return network_association.sea
        return None

    def _build_network_api(self):
        """
        Builds a nova network API.

        :returns: Nova Network API
        """
        if not self._nova_net_api:
            self._nova_net_api = network.API()
        return self._nova_net_api
