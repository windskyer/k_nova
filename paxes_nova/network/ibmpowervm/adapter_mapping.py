#
# =================================================================
# =================================================================

"""
For IBM PowerVC (R2), this is a set of query functions for
persistent Nova resources
"""

from nova import exception

from nova.openstack.common import log as logging
from nova.db.sqlalchemy import api as db_session
from paxes_nova.db import api as dom_api
from paxes_nova.db.network import models as dom
from nova import db
from nova import network as net_root
from oslo.config import cfg
from nova import rpc
from nova.openstack.common import lockutils
from nova.network import neutronv2
from paxes_nova import compute
from paxes_nova.virt.ibmpowervm.vif.common import exception as powexc
from paxes_nova.virt.ibmpowervm.vif.common import ras
from paxes_nova.virt.ibmpowervm.vif.common import utils as utils
from paxes_nova.virt.ibmpowervm.vif.common import warning as pwarnings
from paxes_nova.network.ibmpowervm import host_seas_query as seas_task
from paxes_nova.network.common.api_suppress_mixin import NetworkAPISuppressor

from paxes_nova import _
from nova import utils as nova_utils

LOG = logging.getLogger(__name__)

CONF = cfg.CONF

class AdapterMappingTaskClass(NetworkAPISuppressor):

    """
    Serves as a task class for host and network mappings. Also responsible for
    handling network conflicts
    """

    def __init__(self):
        super(AdapterMappingTaskClass, self).__init__()
        # A Cache that will contain all of the HOST DOMs.  Gets cleared on
        # every call.
        self.host_dom_cache = {}

        # Cache that will maintain all neutron networks  with
        # in a request
        self.neutrn_network_cache = {}

        self._nova_net_api = None

    def _get_host_dom(self, context, host_name, session=None):
        """
        Will return the host dom for the given object

        :param context: The context for building the data
        :param host_name: The name of the host
        :param session: Optional parameter that will pull this in the same
                        session.
        :returns: The host dom object.
        """
        ret_obj = self.host_dom_cache.get(host_name)
        # If it doesn't exist, capture and put into dict
        if not ret_obj:
            ret_obj = utils.get_host_dom(context, host_name, session)
            self.host_dom_cache[host_name] = ret_obj

        return ret_obj

    def _get_neutron_network(self, context, neutron_net_id):
        """
        Will return the neutron network from cache if exist
        otherwise calls neutron api to retrive the network
        for the given id

        :param context: The context for building the data
        :param neutron_net_id: The name of the host
        :returns: Neutron Network object.
        """
        ret_obj = self.neutrn_network_cache.get(neutron_net_id)

        # If it doesn't exist, capture and put into dict
        if not ret_obj:
            net_api = self._build_network_api()
            ret_obj = net_api.get(context, neutron_net_id)
            if ret_obj:
                self.neutrn_network_cache[neutron_net_id] = ret_obj
            else:
                raise neutronv2.exceptions.NeutronClientException()
        return ret_obj

    def _network_association_put_obj(self,
                                     context,
                                     network_association_list,
                                     force, session=None,
                                     reorder_option=True):
        """
        Create or update specified host network mappings returns list
         of errors/warnings
        :param context: context
        :param network_association_list: A list of NetworkAssociation objects
        :param force: User supplied boolean parameter which is used
        to deal with conflicts/warnings
            if true ,any conflicts found still proceeds
                to create/update mappings
            if false,any conflicts found user will be given a
                list of conflicts/warnings
        :param session: session
        :param reorder_option: If set to true, will enable re-ordering of the
                               VIFs.

        :returns: list of errors/warnings if exists, otherwise empty
                  list will be returned  that indicates success
        """
        if not session:
            session = db_session.get_session()

        results_list = []

        # Always clear out the host dom.  If not done, will create session
        # errors.
        self.host_dom_cache = {}

        # Query the hosts, gather the sanitized list of valid network
        # associations, the errors and warnings.
        network_association_list, warning_list, error_list = \
            self._validate_network_association_conflicts(
                context,
                network_association_list,
                force, session)

        # If we have any errors, return
        if len(error_list) > 0:
            return error_list

        # If there are warnings and we are not forced, return those warnings
        if len(warning_list) > 0 and not force:
            return warning_list

        had_conflicts = False
        conflicting_net_name = ''

        # Loop through every network association create
        for na in network_association_list:
            # If there are no VMs running on this network association, then
            # the update to the database should be done from this thread.
            # Otherwise, if there are VMs, we have to delegate to the compute
            # process because a plug/unplug is needed against the VIFs.
            if not na['has_vms'] or not reorder_option:
                issues = self._update_database_for_net_assn(na, context,
                                                            session)
                results_list.extend(issues)
            else:
                # This means that the given VLAN/vSwitch has VMs running on it.
                # The user has requested for a force as well and as a result we
                # need to unplug that VM from the older SEA and plug it into
                # the new requested SEA. We cannot do the plug and unplug in
                # the same thread, or in the same process.  Need to move the
                # code over to the compute processes.
                self._reorder_vifs_on_vios(context, na)
                had_conflicts = True

                # Get the conflicting network...
                net = self._get_neutron_network(context, na['net_id'])
                # ...and it's name.
                conflicting_net_name = net.get('name')

        # If there were networks that needed to be modified over the wire
        # which takes time, inform the user that it may take a while
        if had_conflicts:
            msg = _("PowerVC received the request to modify network %s "
                    "to use a different Shared Ethernet Adapter (SEA) on one "
                    "or more hosts. You will receive another message in "
                    "several minutes when the operation completes.") % \
                conflicting_net_name
            info = {'msg': msg}
            LOG.info(msg)
            notifier = rpc.get_notifier(service='api')
            notifier.info(context, 'compute.instance.log', info)

        return results_list

    def network_association_put_obj(self,
                                    context,
                                    network_association_list,
                                    force, session=None,
                                    clear_cache=True):
        """
        Create or update specified host network mappings returns list
         of errors/warnings
        :param context: context
        :param network_association_list: A list of NetworkAssociation objects
        :param force: User supplied boolean parameter which is used
        to deal with conflicts/warnings
            if true ,any conflicts found still proceeds
                to create/update mappings
            if false,any conflicts found user will be given a
                list of conflicts/warnings
        :param session: session
        :param clear_cache: If true, will clear out the cache of data

        :returns: list of errors/warnings if exists, otherwise empty
        list will be returned  that indicates success

        """
        # This class should only be used in PowerKVM environments
        self.raise_if_not_powervm()

        if not session:
            session = db_session.get_session()

        if clear_cache:
            self.neutrn_network_cache = {}

        with session.begin(subtransactions=True):
            return self._network_association_put_obj(
                context, network_association_list, force, session)

    def network_association_put(self,
                                context,
                                host_network_mapping_dict,
                                force, session=None):
        """
        Create or update specified host network mappings returns list
         of errors/warnings
        :param context: context
        :param host_network_mapping_dict: Dict of host and network
         mappings needs to be created/updated to data base
        for example
        [
        {
            "host_name": "testhost",
            "networks": [
                {
                    "network_id": "testnetworkid",
                    "primary_sea": "not-applicable"
                }
            ]
        },
        {
            "host_name": "testhost",
            "networks": [
                {
                    "network_id": "testnetworkid2",
                    "primary_sea": {
                        "name": "ent1",
                        "lpar_id": 5
                    }
                }
            ]
        }
        ]
        :param force: User supplied boolean parameter which is used
        to deal with conflicts/warnings
            if true ,any conflicts found still proceeds
                to create/update mappings
            if false,any conflicts found user will be given a
                list of conflicts/warnings
        :param session: session
        :returns: list of errors/warnings if exists, otherwise empty
        list will be returned  that indicates success
        """
        # This class should only be used in PowerKVM environments
        self.raise_if_not_powervm()

        # Clear the cache on each session transaction.
        self.neutrn_network_cache = {}

        if not session:
            session = db_session.get_session()

        with session.begin(subtransactions=True):
            net_assns, error_list = self.parse_host_network_mappings(
                context, host_network_mapping_dict, session)

            # Any parsing issues should be immediately returned
            if len(error_list) > 0:
                return error_list

            return self.network_association_put_obj(context,
                                                    net_assns,
                                                    force,
                                                    session,
                                                    clear_cache=False)

    def _update_database_for_net_assn(self, na, context, session):
        """
        Will update the backing database (DOM) for the network association
        update.  Should be invoked after all of the validation for that
        network association has been done.

        :param na: The network association dictionary to use for the update
        :param context: The user context.
        :param session: The network session to do the work within
        :returns error_list: A list of any errors that may have occurred during
                             the execution.
        """
        error_list = []
        try:
            # Attempt to create the Network association
            result = None
            if na['sea'] is None:
                result = dom_api.network_association_put_no_sea(
                    context, na['host_name'], na['net_id'],
                    session)

            else:
                # Need to find the SEA from this session
                # SB: Why do we need a session sea and not a normal
                # sea object?
                session_sea = self._find_matching_sea(context,
                                                      na['sea'],
                                                      na['host_name'],
                                                      session)
                result = dom_api.network_association_put_sea(
                    context, na['host_name'], na['net_id'],
                    session_sea, session)
                # Let's update the peer networks as well now. Note the
                # peer network can never be Not applicable when we come
                # till here, coz not-applicable peers would have
                # already resulted in an error
                for peer in na['peers']:
                    result_peer = dom_api.network_association_put_sea(
                        context, na['host_name'], peer, session_sea, session)
                    if not result_peer:
                        raise powexc.\
                            IBMPowerVMNetworkAssociationPutException(
                                hostid=na['host_name'])
                    else:
                        msg = (ras.vif_get_msg
                               ('info', 'NETWORK_ASSOCIATION_PUT') %
                               {'hostid': na['host_name'],
                                'networkid': na['net_id']})
                        ras.function_tracepoint(
                            LOG, __name__,
                            ras.TRACE_INFO, msg)
            # Validate the result of the ut
            if not result:
                raise powexc.\
                    IBMPowerVMNetworkAssociationPutException(
                        hostid=na['host_name'])
            else:
                msg = (ras.vif_get_msg
                      ('info', 'NETWORK_ASSOCIATION_PUT') %
                       {'hostid': na['host_name'],
                        'networkid': na['net_id']})
                ras.function_tracepoint(
                    LOG, __name__, ras.TRACE_INFO, msg)

        except powexc.\
                IBMPowerVMNetworkAssociationPutException as e:
            ras.function_tracepoint(
                LOG, __name__, ras.TRACE_ERROR, e.message)
            error_list.append(e)
        return error_list

    def _find_matching_sea(self, context, sea, host_name, session):
        """
        Will load the appropriate SEA into memory for the given session.
        """
        # Must be passed in via the object.
        host_dom = self._get_host_dom(context, host_name, session)
        return host_dom.find_adapter(sea.vio_server.lpar_id,
                                     sea.name)

    def _build_network_api(self):
        """
        Builds a nova network API.

        :returns: Nova Network API
        """
        if not self._nova_net_api:
            self._nova_net_api = net_root.API()
        return self._nova_net_api

    def _validate_network_association_conflicts(self, context,
                                                network_association_list,
                                                force, session):
        """
        Validates the given network associations for any conflicts.  If any
        conflicts are found, then this method will add to warnings list. If any
        errors are encountered, it adds to errors list.

        There is a possibility of two types of conflicts:

            network conflicts: when user wants to create/update a network
            association with a new sea and other networks exist on the same
            vios/lpar with a different sea.

            sea conflicts: when user wants to update a network association
            with a new sea and vms exist on the network, then there will
            be a network down time (from the unplug/plug needed to move the
            network to a new SEA) which should be intimated to user.

        :param context: context for db access
        :param network_association_list:network associations that need to be
                                        validated for conflicts
        :returns all_net: A list of the Network Association Dictionaries.  The
                          format of the dictionary element includes:
                           - host_name (The name of the host)
                           - sea (The shared ethernet adapter DOM object)
                           - net_id (the neutron network UUID)
                           - has_vms (boolean indicating whether or not VMs are
                             running on the network)
                           - vlan (VLAN ID that is being modified)
                           - peers (network id's of peer networks)
        :returns warning_list: The list of all warnings
        :returns error_list: The list of all errors
        """
        # Blank out our return lists
        warning_list = []
        error_list = []

        # Filter out associations that aren't different from what's already in
        # the db.
        filtered_associations_list = self._remove_unmodified_associations(
            context, session, network_association_list)
        all_net = []
        
        for new_assn in filtered_associations_list:
            # Note: The flow should break for any given network that results in
            # an 'error'. We should continue and proceed to the next one.
            neutron_network = None
            new_sea = new_assn.sea
            try:
                neutron_net_id = new_assn.neutron_net_id
                neutron_network = self._get_neutron_network(context,
                                                            neutron_net_id)
                #vlan = neutron_network.get("provider:segmentation_id")
                if neutron_network.get("provider:segmentation_id"):
                    vlan = neutron_network.get("provider:segmentation_id")
                else:
                    vlan = neutron_network.get("vlan")
            except neutronv2.exceptions.NeutronClientException as e:
                ras.function_tracepoint(
                    LOG, __name__, ras.TRACE_ERROR, e.message)
                error_list.append(e)
                continue
            # This refers to a not-applicable network, we would still need to
            # populate our overall dictionary
            host_name = new_assn.get_host_name()
            if new_sea is None:
                    request_net_dict = {}
                    request_net_dict['host_name'] = host_name
                    request_net_dict['sea'] = new_sea
                    request_net_dict['net_id'] = neutron_net_id
                    request_net_dict['has_vms'] = False
                    request_net_dict['vlan'] = vlan
                    # We will update this if we get peers ahead else updating
                    # with empty data.
                    request_net_dict['peers'] = []
                    all_net.append(request_net_dict)
            # If the network association had a SEA, run specific validators
            if new_sea:
                try:
                    # Start populating the Dictionary.
                    # Build a dictionary for each individual SEA
                    # We flush everything before we populate it.
                    request_net_dict = {}
                    request_net_dict['host_name'] = host_name
                    request_net_dict['sea'] = new_sea
                    request_net_dict['lpar_id'] = new_sea.vio_server.lpar_id
                    request_net_dict['net_id'] = neutron_net_id
                    request_net_dict['has_vms'] = False
                    request_net_dict['vlan'] = vlan
                    # We will update this if we get peers ahead else updating
                    # with empty data.
                    request_net_dict['peers'] = []
                    all_net.append(request_net_dict)
                    self._check_primary_sea_chosen(new_sea, host_name,
                                                   context, session)

                    self._check_reserved_vlan_conflict(vlan, new_sea,
                                                       host_name, context,
                                                       session)
                except powexc.IBMPowerVMInvalidSEAConfig as e:
                    ras.function_tracepoint(LOG, __name__,
                                            ras.TRACE_ERROR, e.message)
                    error_list.append(e)
                    continue
            try:
                # See if this new association conflicts with any existing
                # networks.
                all_net = self._check_network_conflict(context,
                                                       host_name,
                                                       new_assn.neutron_net_id,
                                                       new_sea,
                                                       vlan,
                                                       force,
                                                       session, all_net)
            except pwarnings.IBMPowerVMNetworkConflict as e:
                ras.function_tracepoint(
                    LOG, __name__, ras.TRACE_WARNING, e.message)
                warning_list.append(e)
            except pwarnings.IBMPowerVMSeaChangeConflict as e:
                ras.function_tracepoint(
                    LOG, __name__, ras.TRACE_WARNING, e.message)
                warning_list.append(e)
            if nova_utils.is_neutron():
                try:
                    # See if this new association conflicts with any deployed
                    # vlans (ie, they already exist on SEAs)
                    net_id = new_assn.neutron_net_id
                    all_net = self._check_existing_vm_conflict(context,
                                                               host_name,
                                                               net_id, new_sea,
                                                               vlan, force,
                                                               session, all_net)

                except pwarnings.IBMPowerVMSeaDefaultConflict as e:
                    ras.function_tracepoint(
                        LOG, __name__, ras.TRACE_ERROR, e.message)
                    warning_list.append(e)
                except powexc.IBMPowerVMNetworkInUseException as e:
                    ras.function_tracepoint(LOG, __name__, ras.TRACE_ERROR,
                                            e.message)
                    error_list.append(e)
                except powexc.IBMPowerVMSEAVSwitchNotAllowed as e:
                    ras.function_tracepoint(LOG, __name__, ras.TRACE_ERROR,
                                            e.message)
                    error_list.append(e)
        return all_net, warning_list, error_list

    def _check_network_conflict(self, context, host_id,
                                neutron_net_id, sea, vlan, force, session,
                                all_net):
        """
        This method will throw an exception if a network conflict is detected.
        A network conflict will be found when there are multiple network
        mappings within a given host for a specific vlanid/vswitch combo but
        they don't all map to the same VIOS (lparid) or to the same SEA on that
        VIOS.

        :param context: context for db access
        :param host_id: only check network associations for this host_id
        :param neutron_net_id: Unique identifier for Neutron network we're
                               validating
        :param sea: sea associated with neutron_net_id above
        :param vlan: vlanid associated with neutron_net_id above
        :param force: boolean to tell us whether we're here to force moving
                      the network association to a new SEA.  If true, and we
                      find current instances using the network, we'll set
                      self.plugUnplugRequired to True, indicating further
                      processing is needed to "move" the existing networks to
                      the new SEA.
        :param all_net: This is a variable that contains all the
                     filtered network associations that are updated and passed
                     on to the next method for adding fields.
        :param session: session for db access
        """
        if sea:
            sea_vswitch = sea.get_primary_vea().vswitch_name
        else:
            sea_vswitch = None
        peer_network_list = []
        conflicting_net_assns = []
        peer_sea = None
        # If SEA is none, that means the neutron network we're told to check
        # does not have an associated SEA yet.  Which implicitly means no
        # conflict can occur.
        if sea is None:
            return all_net

        # Get all this host's network associations
        all_host_net_assns = dom_api.network_association_find_all(context,
                                                                  host_id,
                                                                  session)
        # For each association on the host...
        for net_assn in all_host_net_assns:
            # If the current network association we're looping through is
            # associated with an SEA, then a conflict occurs if it has the
            # same vlan/vswitch as our given neutron network but is either on
            # a different VIOS, or it's on the same VIOS but different SEA.
            if (net_assn.neutron_net_id != neutron_net_id and
                net_assn.sea is not None and
                self._vlans_match(context, net_assn.neutron_net_id, vlan) and
                net_assn.sea.get_primary_vea().vswitch_name == sea_vswitch and
                (net_assn.sea.vio_server.lpar_id != sea.vio_server.lpar_id or
                 net_assn.sea.name != sea.name)):
                # Found a conflict.  Build a dict with the info.
                conflict_dict = {}
                conflict_dict['sea'] = sea
                conflict_dict['vlan'] = vlan
                conflict_dict['net_assoc'] = net_assn
                conflict_dict['peer_sea'] = net_assn.sea
                peer_network_list.append(net_assn.neutron_net_id)
                # There can only be ONE peer SEA.
                peer_sea = net_assn.sea
                # Maintain list that will be used in this method
                conflicting_net_assns.append(conflict_dict)

                # We update the all network dictionary with the peer networks
                # for the given host and SEA. The host_id check is because the
                # user may have searched across multiple hosts and all_net has
                # all of the users input
                for net_dict in all_net:
                    if net_dict['sea'] == sea and\
                            net_dict['host_name'] == host_id:
                        net_dict['peers'] = peer_network_list
                        net_dict['peer_sea'] = peer_sea

        # Check to see if the SEA they choose has the VLAN on it already...
        # regardless of VMs being there.  If so, we HAVE to do a plug/unplug
        host_dom = self._get_host_dom(context, host_id, session)
        cur_sea = host_dom.find_sea_for_vlan_vswitch(vlan, sea_vswitch)
        has_conflicting_sea = False
        if (cur_sea is not None and
                (cur_sea.name != sea.name or
                 cur_sea.vio_server.lpar_id != sea.vio_server.lpar_id)):
            for net_dict in all_net:
                if net_dict['sea'] == sea and\
                        net_dict['host_name'] == host_id:
                    net_dict['has_vms'] = True
                    has_conflicting_sea = True
                    net_dict['peer_sea'] = cur_sea

        # If we were told to force pushing the network to another SEA, see if
        # there are instances we'll need to plug/unplug vifs for.
        if force:
            for net_assn in conflicting_net_assns:
                # This check finds out if there are VMs running on other peer
                # networks. Hence that would mean a unplug and plug as well
                # However, i would like to think that the dom_api should be
                # doing the checks based on vlans rather than neutron ids.
                # Need confirmation on this. If it doesn't then we would have
                # to set the has_vms flag here as well.
                vm_list = dom_api.instances_find(context,
                                                 host_id,
                                                 net_assn['net_assoc'].
                                                 neutron_net_id)
                if vm_list:
                    # We need to still set the flag for plug/unplug here
                    # we would set the has_vms to true
                    for net_dict in all_net:
                        if host_id == net_dict['host_name'] \
                                and net_assn['peer_sea'] == \
                                net_dict['peer_sea']:
                            net_dict['has_vms'] = True
                    break
        else:
            # We weren't told to force.  Check to see if we found conflicts.
            if len(conflicting_net_assns) > 0:
                # Build up a string to be dumped in the exception message.
                str_networkids = ''
                for nw_assn in conflicting_net_assns:
                    # Get the conflicting network...
                    net = self._get_neutron_network(context,
                                                    nw_assn['net_assoc'].
                                                    neutron_net_id)
                    # ...and it's name.
                    net_name = net.get('name')

                    # If this isn't the first network we're adding to the
                    # string, be sure to put a comma in between
                    if len(str_networkids) > 0:
                        str_networkids += ', '

                    # Add in the network name to the string
                    str_networkids += str(net_name)

                # Raise the conflict exception
                raise pwarnings.IBMPowerVMNetworkConflict(
                    networklist=str_networkids,
                    vlanid=vlan,
                    vswitch=sea.get_primary_vea().vswitch_name,
                    sea=sea.name,
                    hostid=host_id)
            elif has_conflicting_sea:
                psea = net_dict['peer_sea']
                host_name = host_dom.host_name
                vios_name = psea.vio_server.lpar_name
                raise pwarnings.IBMPowerVMSeaChangeConflict(vlanid=vlan,
                                                            sea=psea.name,
                                                            host=host_name,
                                                            vios=vios_name)
        return all_net

    @lockutils.synchronized('adapter_mapping', 'adpater-mapping-')
    def _reorder_vifs_on_vios(self, context, net_dict):
        """
         This method unplugs/plugs VIFs if there's a network conflict detected
         and the user has forced the option to switch the SEA. Note the case
         where the user has requested for a force and the PVID of the old SEA
         hosts the VLAN, in that case, this flow will not be hit, an error
         would have got reported in the check for 'reserved vlans'. If it
         comes to the this method (there are VMs running
         on the given neutron id), then we will do the following:
         1. Create the Host DOM Object.
         2. Obtain the SEA_CHAINs for the old and new SEA.
         3. Create the bridge information.
         4. Call the unplug on the old SEA.
         5. Call a Plug on the new SEA.
         :param context: context
         :param net_dict: The network association dictionary object that
                contains the information to properly migrate a network
                association, and its peers, over to the new value.
        """
        host_api = compute.HostAPI()

        # The peer sea may not have been set in a very rare scenario...
        # In that case, we set the SEA to the original SEA
        if net_dict.get('peer_sea', None) is not None:
            peer_sea = net_dict['peer_sea']
        else:
            peer_sea = net_dict['sea']

        data = {'new_sea': net_dict['sea'].name,
                'new_lpar_id': net_dict['sea'].vio_server.lpar_id,
                'net_id': net_dict['net_id'],
                'old_sea': peer_sea.name,
                'old_lpar_id': peer_sea.vio_server.lpar_id,
                'peers': net_dict['peers'],
                'vlan': net_dict['vlan']}
        host_api.change_vif_sea(context, data, net_dict['host_name'])

    def _vlans_match(self, context, neutron_net_id, vlan):
        """
         This method checks if the neutron network passed in has a matching
         vlan to the vlan passed in.

         :param context: context
         :param neutron_net_id: Neutron unique identifier
         :param vlan: vlan which need to be compared

         :returns: True if both vlan ids match other wise
                   False will be returned
        """
        neutron_net = None

        try:
            neutron_net = self._get_neutron_network(context, neutron_net_id)
        except neutronv2.exceptions.NeutronClientException as e:
            ras.function_tracepoint(
                LOG, __name__, ras.TRACE_WARNING, e.message)
            return False
        if nova_utils.is_neutron():
            other_network_vlan = int(neutron_net.get("provider:segmentation_id"))
        else:
            other_network_vlan = int(neutron_net.get("vlan"))
        if(other_network_vlan == int(vlan)):
            return True
        else:
            return False

    def _remove_unmodified_associations(
            self, context, session, network_association_list):
        """
         This method checks each network association against db's network
         association.  If they do not match, it will be added to the return
         list.  If they do match, it will be ignored.

         :param context: context for db access
         :param session: session for db access
         :param network_association_list: network_association_list to filter

        """
        ret_list = []
        # Check every association in the list
        for assn in network_association_list:
            # Get SEA for new association
            sea = assn.sea
            # Get current association from the db, if any
            db_assn = dom_api.network_association_find(context,
                                                       assn.get_host_name(),
                                                       assn.neutron_net_id,
                                                       session)

            # Did the association already exist in the db?
            if db_assn:
                # If the new association and the db association both have SEAs
                if sea and db_assn.sea:
                    # If the new association and db associations are on the
                    # same lpar with a different adapter name, or if they're
                    # on different lpars...
                    if (db_assn.sea.vio_server.lpar_id !=
                            sea.vio_server.lpar_id or
                            db_assn.sea.name != sea.name):
                        # Process this association
                        ret_list.append(assn)
                elif sea or db_assn.sea:
                    # Either db entry doesn't have sea or modified association
                    # doesn't have sea.  Needs to be processed.  If NEITHER
                    # has an SEA, they don't need to be processed.
                    ret_list.append(assn)
            else:
                # No entry in the db.  Needs to be processed.
                ret_list.append(assn)

        return ret_list

    def _check_existing_vm_conflict(self, context, host_name,
                                    neutron_net_id, sea, vlan, force, session,
                                    all_net):
        """
         This method checks for sea default adapter conflicts if any exists
         then raises sea default conflict warning.

         To Check Sea conflicts
             check given network is used on the given host
                if yes Raise sea default conflict stating network down time
         :param context: context
         :param host_id: host id for which need to check sea conflict
         :param neutron_net_id: Neutron network unique identifier
         :param all_net: This is a list of all the filtered network assocs

        """
        # Get all VMs using this network
        vm_list = dom_api.instances_find(context, host_name, neutron_net_id)

        db_assn = None
        if force:
            # Put the force check here since self.plugUnplugRequired could have
            # been set in a different method based on force...
            db_assn = dom_api.network_association_find(context,
                                                       host_name,
                                                       neutron_net_id,
                                                       session)
        # The has_vms flag would mean that a force was applied to plug/unplug.
        # The reason a force applied implies with it is because if the force
        # is not applied, the code beginning with 'if vm_list' will throw an
        # exception or warning.
        for net_dict in all_net:
            if host_name == net_dict['host_name'] and sea == net_dict['sea']:
                # If the has_vms has already been set, meaning there's a peer
                # network which has VMs running on it - so we should check
                # that and not make an update even if vm_list is empty for the
                # current neutron network
                if vm_list or net_dict['has_vms']:
                    net_dict['has_vms'] = True
                else:
                    net_dict['has_vms'] = False
        # If there are VMs using this network
        if vm_list:
            # If the new network says to not use any SEA, that's bad because
            # we already have VMs using it.  Can't allow this.
            if sea is None:
                raise powexc.IBMPowerVMNetworkInUseException(hostid=host_name)
            else:
                # The new association has an SEA.  However, since we're only
                # looking at network associations that DON'T match what's
                # already in the DB, we know something's different.  We can
                # only allow a very specific difference...
                if force:
                    # If there was an existing association in the db...
                    if db_assn and db_assn.sea:
                        # Check if the requested SEA is on the same VSWITCH as
                        # the older SEA VSWITCH. IF that's not true then raise
                        # an exception. VMS are hooked to VSwitches and cannot
                        # be moved.
                        if db_assn.sea.get_primary_vea().vswitch_name !=\
                                sea.get_primary_vea().vswitch_name:
                            raise powexc.\
                                IBMPowerVMSEAVSwitchNotAllowed(sea=sea.name)
                else:
                    # Something is different between the new association and
                    # what's in the db.  We weren't told to force, so we can't
                    # allow this.
                    raise pwarnings.IBMPowerVMSeaDefaultConflict()
        return all_net

    def network_association_find(self, context, neutron_network_id, host_name,
                                 session=None, filter_closure=None):
        """
        Returns a list of all host network mappings in the system
        according to the given parameters
        :param context: context
        :param neutron_network_id: Neutron Network unique identifier
        :param host_name: host_name
        :param session: session
        :param filter_closure: A method used to filter the return list before
                               returning.  The method accepts a single
                               parameter as a list of network association
                               objects.  The method returns a list of network
                               association objects, which is generally an
                               altered version of the passed list.
        :returns:  list of host network mappings
        """
        # This class should only be used in PowerKVM environments
        self.raise_if_not_powervm()

        if not session:
            session = db_session.get_session()

        # Clear the cache on each GET.
        self.neutrn_network_cache = {}
        self.host_dom_cache = {}

        with session.begin(subtransactions=True):
            # If a network or host was provided, make sure all host-network
            # mappings are created for that host and/or network.  If neither
            # were provided, then we won't create all possible mappings.
            if neutron_network_id is not None or host_name is not None:
                self._network_association_validator(context,
                                                    host_name,
                                                    neutron_network_id,
                                                    session)

            network_associations = []
            try:
                if neutron_network_id is not None and host_name is not None:
                    network_association = dom_api.network_association_find(
                        context, host_name, neutron_network_id, session)
                    network_associations = [network_association]
                elif neutron_network_id is not None:
                    network_associations = dom_api.\
                        network_association_find_all_by_network(
                            context, neutron_network_id, session)
                else:
                    network_associations = dom_api.\
                        network_association_find_all(
                            context, host_name, session)
            except exception.ComputeHostNotFound as e:
                e.message += host_name
                error_dict = {'not found': [e.message]}
                return error_dict

            # Apply a filter if one was provided by the caller
            network_assocations_filtered = network_associations
            if filter_closure:
                network_assocations_filtered = \
                    filter_closure(context, network_associations)

            return self._format_host_network_mappings_response(
                context, network_assocations_filtered)

    def _format_host_network_mappings_response(self, context,
                                               network_association_list):
        """
        Returns a dict of all host network mappings according to the
        given parameters
        :param context: context
        :param network_association_list: list of network associations
        to be formated

        :returns:   dict of host network mappings
        """
        host_network_mapping_dict = {'host-network-mapping': []}
        host_dict = {}
        for nw_association in network_association_list:
            if host_dict.get(nw_association.get_host_name()) is None:
                host_dict[nw_association.get_host_name()] = []
            try:
                self._get_neutron_network(context,
                                          nw_association.neutron_net_id)
            except neutronv2.exceptions.NeutronClientException:
                # as the network does not exists in data base
                # we need to ignore the association
                continue
            host_dict.get(nw_association.get_host_name()).\
                append(self._format_networks_response(
                       nw_association.neutron_net_id,
                       nw_association.sea))
        for host in host_dict:
            host_network_mapping_dict.get('host-network-mapping').\
                append({
                       "host_name": host,
                       "networks": host_dict[host]
                       })
        return host_network_mapping_dict

    def _format_networks_response(self, neutron_net_id, sea):
        """
        Will format the input into an appropriate dictionary response structure

        :param neutron_net_id: Neutron Unique network identifier
        :param sea: shared ethernet adapter
        :returns:   dict of network and sea
        """
        if sea is not None:
            return {
                "network_id": neutron_net_id,
                "primary_sea": {
                    "lpar_id": sea.vio_server.lpar_id,
                    "name": sea.name
                }
            }
        else:
            return {
                "network_id": neutron_net_id,
                "primary_sea": "not-applicable"
            }

    def parse_host_network_mappings(self,
                                    context,
                                    host_network_mappings, session,
                                    dom_factory=dom.DOM_Factory()):
        """
        Parses a dictionary that represents a Host into a DOM Host object.

        :param context: The context for the user making the request.
        :param host_network_mappings: List of Host and Network mappings.
        :param session: session
        :param dom_factory: Optional factory used to create the DOM objects.
        Not required to be set.

        :return network_association_list: List of network association objects.
        :return errors: List of errors found during the parsing.
        """
        # This class should only be used in PowerKVM environments
        self.raise_if_not_powervm()

        # Always clear out the host dom.  If not done, will create session
        # errors from the last put call.
        self.host_dom_cache = {}

        error_list = []
        network_association_list = []
        for host in host_network_mappings:
            host_name = host.get('host_name')
            for network in host.get('networks'):
                try:
                    selected_sea = network.get("primary_sea")
                    if selected_sea is None:
                        continue

                    primary_sea = None

                    # If the user selected the SEA, go get it
                    if selected_sea != "not-applicable":
                        host_dom = self._get_host_dom(
                            context, host_name, session)
                        sea_name = selected_sea.get("name")
                        lpar_id = selected_sea.get("lpar_id")
                        sea_obj = host_dom.find_adapter(lpar_id, sea_name)
                        if sea_obj is None:
                            raise powexc.IBMPowerVMValidSEANotFound()
                        primary_sea = host_dom.\
                            find_sea_chain_for_sea(sea_obj)[0]
                        if primary_sea is None:
                            raise powexc.IBMPowerVMValidSEANotFound()
                    # Creates the network association in memory, not yet
                    # persisted to db
                    network_association = dom.No_DB_DOM_Factory().\
                        create_network_association(
                            host_name, network.get("network_id"), primary_sea)
                    network_association_list.append(network_association)
                except powexc.IBMPowerVMValidSEANotFound as e:
                    error_list.append(e)
                except exception.ComputeHostNotFound as e:
                    error_list.append(e)

        return network_association_list, error_list

    def _check_primary_sea_chosen(self, sea, host_id, context, session):
        """
        Validates that a primary sea was chosen.

        :param sea: The user picked SEA
        :param host_id: The host system identifier.
        :param context: The context for the query.
        :param session: The session for the database transactions.
        """

        # Get all the SEA Chains
        host_dom = self._get_host_dom(context, host_id, session)
        sea_chains = host_dom.find_sea_chains()

        found_match = False

        # Validate that the SEA passed in by the user is one of the
        # primary SEAs.
        for sea_chain in sea_chains:
            primary_sea = sea_chain[0]

            # Make sure the name matches
            if primary_sea.name != sea.name:
                continue

            # Make sure the lpar id matches
            if primary_sea.vio_server.lpar_id != sea.vio_server.lpar_id:
                continue

            # At this point it should match.
            found_match = True

        # If we didn't find a match, then we need to throw an error.
        if not found_match:
            raise powexc.IBMPowerVMNonPrimarySEASelection(devname=sea.name)

    def _check_reserved_vlan_conflict(self, vlan, sea, host_id, context,
                                      session):
        """
        A reserved VLAN is either the PVID of a SEA or the VLANs of it's
        primary VEA or any VLAN mapped to the orphan VEAs. However, every SEA
        is connected to a particular VSWITCH and hence the reserved VLANs are
        also specific to VSWITCHES. This method would find out that if there is
        a request made for a reserved VLAN (obtained from the DOM), then does
        the given SEA host the reserved VLAN or not. If the SEA doesn't host
        it viz. it's not a reserved VLAN for that SEA, an exception will be
        thrown back to the user. Note, that this condition cannot be forced
        either.

        :param vlan: Vlan id of the network requested for association.
        :param sea: The SEA requested for network association
        :param host_id: The host_id of the host for which the reserved VLANs
             have to be found.
        :param context: Context passed from the UI.
        :param session: session paramter
        """
        # The first check we make is to be sure they aren't trying to use vlan
        # 1 on an SEA who's pvid ISN'T vlan 1.  Vlan 1 is special and can't
        # be a tagged vlan.
        if vlan == 1 and sea.get_primary_vea().pvid != 1:
            raise powexc.IBMPowerVMTaggedVLAN1NotAllowed(host=host_id)

        host_dom = self._get_host_dom(context, host_id, session)
        vio_servers = host_dom.vio_servers
        for vio_server in vio_servers:
            vswitch = sea.get_primary_vea().vswitch_name
            reserved_vlans = vio_server.get_reserved_vlanids(vswitch)
            if int(vlan) in reserved_vlans:
                # Now check if the given SEA is hosting that VLAN or not.
                if vlan != sea.pvid and utils.\
                        is_vlan_in_primary_vea_addl_vlans(vlan, sea) is False:
                    # Need to build an end user error message,
                    # so need specific details to do so
                    reserved_vlan_dict = vio_server.\
                        get_reserved_vlanids_dict(vswitch)
                    pref_sea = 'unknown'
                    for sea_name, vlan_id in reserved_vlan_dict.items():
                        # Now check  the vlan supplied is in the list
                        # of VLANs against the SEA or not.
                        if int(vlan) in vlan_id:
                            pref_sea = sea_name
                    # If we get an Orhan VEA in the dictionary then we need
                    # to form a special message for the Orphans
                    if pref_sea == "orphan":
                        raise powexc.IBMPowerVMInvalidSEASelectionOnOrphanVEA(
                            vlan=vlan,
                            sea=sea.name,
                            host=host_id)
                    # If it has come till here - it's a normal SEA.
                    raise powexc.IBMPowerVMInvalidSEASelection(vlan=vlan,
                                                               sea=sea.name,
                                                               host=host_id,
                                                               psea=pref_sea)

    def _network_association_validator(self, context, host, net_id, session):
        """
        Ensures that all specified networks and hosts have a association, and
        creates a default association if one does not exist.  Either the host
        and/or the net_id parameter is required.

        :param context: The database context for queries.
        :param host: The name of the host to create mappings for.
        :param net_id: The neutron network ID to create mappings for.
        :param session: Allows the same database session to be used.
        """
        if host is None and net_id is None:
            # Filling in all possible associations is expensive.  If something
            # seems difficult, we shouldn't even try to avoid disappointment.
            return

        # Figure out what networks and hosts we need to map based on the host
        # and network we were passed.
        nets_to_map = []
        hosts_to_map = []
        if host is not None:
            # A host was provided, so we will only create mappings for it
            hosts_to_map = [host]
            if net_id is not None:
                # A network was provided, so we will only create mappings for
                # it
                nets_to_map = [net_id]
                try:
                    self._get_neutron_network(context, net_id)
                except neutronv2.exceptions.NeutronClientException as e:
                    ras.function_tracepoint(
                        LOG, __name__, ras.TRACE_ERROR, e.message)
                    raise
            else:
                # No network was provided, so map them all for this host
                network_api = self._build_network_api()
                all_net_dicts = network_api.get_all(context)
                # Find any networks that are already mapped for this host
                net_assn_list = dom_api.network_association_find_all(context,
                                                                     host,
                                                                     session)
                net_assn_ids = []
                for net_assn in net_assn_list:
                    net_assn_ids.append(net_assn.neutron_net_id)
                for a_net_dict in all_net_dicts:
                    if a_net_dict['id'] not in net_assn_ids:
                        self.neutrn_network_cache[
                            a_net_dict['id']] = a_net_dict
                        nets_to_map.append(a_net_dict['id'])
        elif net_id is not None:
            # No host was provided, so map them all for this network
            hosts_to_map = self.find_all_host_names(context)
            # A network was provided, so we will only create mappings for it
            nets_to_map = [net_id]
            try:
                self._get_neutron_network(context, net_id)
            except neutronv2.exceptions.NeutronClientException as e:
                ras.function_tracepoint(
                    LOG, __name__, ras.TRACE_ERROR, e.message)
                raise

            # Find any hosts that are already mapped for this network
            net_assns = dom_api.network_association_find_all_by_network(
                context, net_id, session)
            for net_assn in net_assns:
                hosts_to_map.remove(net_assn.get_host_name())

        # Create all needed host-network mappings (associations)
        for a_host in hosts_to_map:
            for a_net in nets_to_map:
                try:
                    neutron_network = self._get_neutron_network(context, a_neti)
                    if neutron_network.get("provider:segmentation_id"):
                        vlan = neutron_network.get("provider:segmentation_id", 1)
                    else:
                        vlan = neutron_network.get("vlan", 1)
                    self.build_default_network_assn(
                        context,
                        utils.get_host_dom(context, a_host, session),
                        int(vlan),
                        a_net, session=session)
                except (powexc.IBMPowerVMInvalidSEASelection,
                        powexc.IBMPowerVMValidSEANotFound) as e:
                    # Log and continue
                    ras.function_tracepoint(LOG, __name__, ras.TRACE_ERROR,
                                            e.message)

    def build_default_network_assn(self, context, host_dom, vlanid, net_id,
                                   vswitch=None, session=None):
        """
        In some scenarios, the network association will not exist and will
        need to be created at provision time.  This method will query the
        HostSeasQuery to find what the default adapter should be and will
        create the network association.

        :param context: The context for the queries
        :param host_dom: The DOM for the system
        :param vlanid: The VLAN that is being queried
        :param net_id: The Neurton network ID
        :param vswitch: The vswitch that is being queried
        :param session: The session object

        :returns sea: A Shared Ethernet Adapter that represents the primary
                      SEA for the Network Association
        """
        # This class should only be used in PowerKVM environments
        self.raise_if_not_powervm()

        if not session:
            session = db_session.get_session()

        host_id = host_dom.host_name

        dict_sea_resp = seas_task.HostSeasQuery().\
            get_host_seas(context, host_id, vswitch,
                          vlanid)
        msg = (ras.vif_get_msg('info', 'HOST_SEA_RESPONSE') %
               {'resp': dict_sea_resp})
        ras.function_tracepoint(LOG, __name__, ras.TRACE_INFO, msg)
        if not dict_sea_resp:
            raise powexc.IBMPowerVMValidSEANotFound(host=host_id)

        adapter = self._iterate_dictionary_for_sea(dict_sea_resp,
                                                   host_id)
        msg = (ras.vif_get_msg('info', 'DEFAULT_ADPT_RESP') %
               {'resp': adapter})
        ras.function_tracepoint(LOG, __name__, ras.TRACE_INFO, msg)
        if not adapter:
            msg = (ras.vif_get_msg('info', 'SEA_NOT_FOUND') %
                   {'net_id': net_id})
            ras.function_tracepoint(LOG, __name__, ras.TRACE_INFO, msg)
            raise powexc.IBMPowerVMValidSEANotFound(host=host_id)

        dom_fact = dom.No_DB_DOM_Factory()
        # Handle the case where this SEA has been set to do-not-use
        if adapter['sea_name'] == 'do-not-use':
            net_assoc = dom_fact.create_network_association(host_id, net_id,
                                                            None)
            with session.begin(subtransactions=True):
                results = self._network_association_put_obj(context,
                                                            [net_assoc],
                                                            True,
                                                            session,
                                                            False)
            if len(results) > 0:
                LOG.error(_('Unable to create default network association'))
                LOG.error(results[0])
                raise powexc.IBMPowerVMValidSEANotFound(host=host_id)
            return None

        pri_sea = host_dom.find_adapter(adapter['lpar_id'],
                                        adapter['sea_name'])

        # Check to be sure the SEA found is from an valid VIOS
        if not pri_sea.is_available():
            LOG.error(_('Primary SEA is on invalid VIOS'))
            LOG.error(pri_sea)
            raise powexc.IBMPowerVMValidSEANotFound(host=host_id)

        msg = (ras.vif_get_msg('info', 'PRIMARY_SEA_DEPLOY') %
               {'sea': pri_sea.name})
        ras.function_tracepoint(LOG, __name__, ras.TRACE_INFO, msg)
        net_assoc = dom_fact.create_network_association(host_id, net_id,
                                                        pri_sea)

        # It may fail...if it does, throw the error
        with session.begin(subtransactions=True):
            results = self._network_association_put_obj(context,
                                                        [net_assoc],
                                                        True, session,
                                                        reorder_option=False)
        if len(results) > 0:
            LOG.error(_('Unable to create default network association'))
            LOG.error(results[0])
            raise powexc.IBMPowerVMValidSEANotFound(host=host_id)

        return pri_sea

    def find_all_host_names(self, context):
        """
        Finds a list of compute node names.
        :param context: DB context object.
        :returns: A list of compute node names.
        """
        # This class should only be used in PowerKVM environments
        self.raise_if_not_powervm()

        compute_nodes = db.compute_node_get_all(context)
        return_nodes = []
        for compute in compute_nodes:
            if not compute['service']:
                LOG.warn(_("No service for compute ID %s" % compute['id']))
                continue
            return_nodes.append(compute['service']['host'])
        return return_nodes

    def _iterate_dictionary_for_sea(self, dict_sea_resp, host_id):
        """
        This method iterates through the sea dictionary returned by the
        host_seas_query task and then finds out the appropriate adapter to
        return. If the sea information was provided, then it would look to
        find the matching information using the SEA name else, it will return
        the default adapter.

        :dict_sea_resp: The SEA adapter dictionary.
        :host_id: the host_id to look for in the dictionary
        :sea: This is an optional parameter, basically an SEA object.
        :returns: An adapter that matches the above in the description.
        """
        for host in dict_sea_resp:
            for host_name in dict_sea_resp[host]:
                for adapter in host_name['adapters']:
                    if host_name['host_name'] == host_id \
                            and adapter['default']:
                            return adapter
        return None
