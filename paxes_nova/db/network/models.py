#
# =================================================================
# =================================================================

from sqlalchemy.orm import session as sqla_orm_session

import copy

from nova.openstack.common import log as logging

from paxes_nova.db.sqlalchemy import models as powervm_dto_models
TBL = powervm_dto_models.PowerVCTableNames

from paxes_nova.virt.ibmpowervm.vif.common import exception as excp
from paxes_nova.virt.ibmpowervm.vif.common import ras

LOG = logging.getLogger(__name__)

# Maximum number of 8021Q tagged VLAN id supported on a single VEA
VEA_MAX_VLANS = 20


def parse_to_host(host_dict, dom_factory=None):
    """
    Parses a dictionary that represents a Host into a DOM Host object.

    :param host_dict: The dictionary that represents the Host object.
    :param dom_factory: Optional factory used to create the DOM objects.  Not
                        required to be set.
    """
    if not dom_factory:
        dom_factory = No_DB_DOM_Factory()

    host_name = host_dict.get('host_name')
    vio_servers = _parse_to_vio_servers(host_dict.get('vio_servers'),
                                        host_name, dom_factory)
    return dom_factory.create_host(host_name, vio_servers)


def _parse_to_vio_servers(vio_servers_dict, host_name, dom_factory):
    """
    Parses a dictionary that contains a list of VIOSes into a list of
    corresponding VIOServer objects.

    :param vio_servers_dict: The dictionary of the VIO Server objects.
    :param host_name: The host name that the VIO Server will be running on.
    :param dom_factory: Factory used to create the DOM objects.

    :return: The list of VIO Servers
    """
    vio_servers = []
    for vio_server in vio_servers_dict:
        vio_obj = dom_factory.create_vios(lpar_name=vio_server['lpar_name'],
                                          lpar_id=int(vio_server['lpar_id']),
                                          host_name=host_name,
                                          state=vio_server['state'],
                                          rmc_state=vio_server['rmc_state'])
        _parse_to_adapter_list(vio_server['adapter_list'], vio_obj,
                               dom_factory)
        vio_servers.append(vio_obj)

    return vio_servers


def _parse_to_adapter_list(adapters_dict, vio_server, dom_factory):
    """
    Parses a list of adapters dictionary objects (for a given VioServer) and
    will pass back a list of corresponding Adapters.

    :param adapters_dict: The List of adapter dictionary objects.
    :param vio_server: the owning VioServer for the adapters.
    :param dom_factory: Factory used to create the DOM objects.
    """
    adapter_list = []

    # Need to loop through all the VEAs first.  The VEAs are referenced by the
    # SEAs.  So the VEAs need to be loaded so that they can be passed down into
    # the SEA.

    for adpt in adapters_dict:
        if adpt.get('type') == 'VirtualEthernetAdapter':
            vea = dom_factory.create_vea(adpt['name'],
                                         vio_server,
                                         adpt['slot'],
                                         adpt['pvid'],
                                         adpt['is_trunk'],
                                         adpt['trunk_pri'],
                                         adpt['state'],
                                         adpt['ieee_eth'],
                                         adpt['vswitch'],
                                         adpt['addl_vlan_ids'])
            adapter_list.append(vea)

    # At this point, all of the VEAs should have been made...start building the
    # Shared Ethernet Adapters.  Seed them with the elements from the VEA list
    for adpt in adapters_dict:
        if adpt.get('type') == 'SharedEthernetAdapter':
            # Use gets here...because they may be VEAs and therefore not have
            # these attributes.
            primary_vea = _find_adapter_by_name(adapter_list,
                                                adpt.get('primary_vea'))

            control_channel = None
            if adpt.get('control_channel'):
                ctl_chan_name = adpt['control_channel']
                if ctl_chan_name != 'None':
                    control_channel = _find_adapter_by_name(adapter_list,
                                                            ctl_chan_name)

            additional_veas = None
            if adpt.get('additional_veas'):
                addl_vea_names = adpt['additional_veas']
                additional_veas = _find_adapters_by_name_list(adapter_list,
                                                              addl_vea_names)

            sea = dom_factory.create_sea(adpt['name'],
                                         vio_server,
                                         adpt['slot'],
                                         adpt['state'],
                                         primary_vea,
                                         control_channel,
                                         additional_veas)
            adapter_list.append(sea)


def _find_adapter_by_name(adapter_list, adapter_name):
    """
    Within a set of adapters, will find the corresponding one for the given
    name.

    :param adapter_list: The list of adapter objects to search through
    :param adapter_name: The name of the adapter to find.
    """
    for adapter in adapter_list:
        if adapter.name == adapter_name:
            return adapter
    raise excp.IVMPowerVMDomParseError()


def _find_adapters_by_name_list(adapter_list, adapter_names):
    """
    Finds a List of adapters by the list of adapter names

    :param adapter_list: The list of discovered Adapters.
    :param adapter_names: The list of adapter device names to query from the
                          adapter_list.
    :return: The corresponding adapter objects, which is a subset of the
             adapter_list.
    """
    ret_list = []
    for adapter_name in adapter_names:
        ret_list.append(_find_adapter_by_name(adapter_list, adapter_name))
    return ret_list


class DOM_Factory(object):

    """
    Used to create the DOM objects internally.  Main purpose is for unit test
    override, so as to remove the SQL dependencies.  This implementation keeps
    the existing SQL function in place.
    """

    def create_host(self, host_name, vio_servers):
        """
        Creates a DOM Host object.

        :param host_name: The host that this mapping applies to.  Should be a
                          string that identifies the host.
        :param vio_servers: The Virtual IO Servers.  Should be a list of
               VioServer objects.
        """
        return Host(host_name, vio_servers)

    def create_network_association(self, host_name, neutron_net_id, sea=None):
        """
        Creates a DOM Network Association object.

        :param host_name: The host that this mapping applies to.  Should be a
                          string that identifies the host.
        :param neutron_net_id: The reference to the Neutron Networks UUID.
                               Should be a string value.
        :param sea: The SharedEthernetAdapter that should be used.  Must reside
                    on the vios previously passed in.  If set to None,
                    indicates that this association should NOT allow VMs to be
                    placed on the Host defined above for the network specified.
        """
        return NetworkAssociation(host_name, neutron_net_id, sea)

    def create_vea(self, name, vio_server, slot, pvid, is_trunk, trunk_pri,
                   state, ieee_eth, vswitch_name, addl_vlan_ids):
        """
        Creates a DOM VirtualEthernetAdapter object.

        :param name: Vea device name
        :param vio_server: Virtual I/O Server this VEA is owned by
        :param slot: VEA's virtual slot number
        :param pvid: untagged vlan id for the VEA
        :param is_trunk: whether this VEA is a bridging VEA or not
        :param state: the current states are Defined and Available
        :param trunk_pri: VEA's trunk priority
        :param ieee_eth: whether the VEA is 8021Q enabled or not
        :param vswitch_name: The name of the IBMPowerVM vswitch_name that VEA
                connects to. The default vswitch_name is ETHERNET0
        :param addl_vlan_ids:  VEA's 8021Q vlan tag list
        """
        return VirtualEthernetAdapter(name, vio_server, slot, pvid, is_trunk,
                                      trunk_pri, state, ieee_eth, vswitch_name,
                                      addl_vlan_ids)

    def create_sea(self, name, vio_server, slot, state, primary_vea,
                   control_channel=None, additional_veas=None):
        """
        Creates a DOM SharedEthernetAdapter object.

        :param name: Sea device name
        :param vios_server: Virtual I/O Server this SEA is owned by
        :param slot: SEA's virtual slot number
        :param state: the current state are Defined and Available
        :param primary_vea: the preferred VirtualEthernetAdapter
        :param control_channel: the VirtualEthernetAdapter used for management
        :param additional_veas: other VirtualEthernetAdapters on this SEA
        """
        return SharedEthernetAdapter(name, vio_server, slot, state,
                                     primary_vea, control_channel,
                                     additional_veas)

    def create_vios(self, lpar_name, lpar_id, host_name, state, rmc_state):
        """
        Creates a DOM VioServer object.

        :param lpar_name: The LPAR Name for the system as presented on the Host
                          itself.
        :param lpar_id: The LPAR identifier as presented on the Host itself.
        :param host_name: The host that this mapping applies to.  Should be a
                          string that identifies the host.
        :param state: State of the VIOS
        :param rmc_state: State of the RMC connection
        """
        return VioServer(lpar_name=lpar_name,
                         lpar_id=lpar_id,
                         host_name=host_name,
                         state=state,
                         rmc_state=rmc_state)


class Host(object):

    """
    The Host is an object that represents a single host system (that may
    contain several LPARs).
    """

    def __init__(self, host_name, vio_servers):
        """
        Represents a single host.  A host may contain several VIO Servers.
        Each Virtual IO Server (VIOS) may have several Shared Ethernet
        Adapters.

        :param host_name: The host that this mapping applies to.  Should be a
                          string that identifies the host.
        :param vio_servers: The Virtual IO Servers.  Should be a list of
               VioServer objects.
        """

        # Check host name. Cannot be empty.
        if not host_name:
            ras.function_tracepoint(LOG,
                                    __name__,
                                    ras.TRACE_ERROR,
                                    ras.vif_get_msg('error',
                                                    'DEVNAME_INVALID') %
                                    {'devname': host_name})
            raise excp.IBMPowerVMInvalidHostConfig(attr='host_name')
        self.host_name = host_name

        # Check VioServer list. vio_servers cannot be null and it needs to be
        # a list of VioServer objects.
        try:
            if not vio_servers:
                vio_servers = []

            if((len(vio_servers) > 0) and
               not isinstance(vio_servers[0], VioServer)):
                raise TypeError()
        except TypeError:
            ras.function_tracepoint(LOG,
                                    __name__,
                                    ras.TRACE_ERROR,
                                    ras.vif_get_msg('error',
                                                    'OBJECT_INVALIDINST') %
                                    {'classname': 'VioServer'})
            raise excp.IBMPowerVMInvalidHostConfig(attr=repr(vio_servers))
        self.vio_servers = vio_servers

        # Set the primary SEA list to none so that it may be initialized
        self.primary_sea_list = None

    def find_all_primary_seas(self, rebuild=False):
        """
        Will return a list of all of the primary SEAs on the system.

        :param rebuild: If set to True, will rebuild the SEA list.  This may
                        be a very expensive operation to add, so only use as
                        needed.  If set to false, will use cached data.
        """
        chains = self.find_sea_chains(rebuild)

        primary_sea_list = []
        for sea_chain in chains:
            primary_sea_list.append(sea_chain[0])
        return primary_sea_list

    def find_sea_chain_for_sea(self, sea, rebuild=False):
        """
        Will find the SharedEthernetAdapter chain for a given SEA.  The SEA
        chain is an ordered list of Shared Ethernet Adapters.  The list is
        ordered such that the first element (SEA) is the primary SEA.  The
        second element is the failover SEA.  The third is the next failover
        element.

        :param rebuild: If set to True, will rebuild the SEA list.  This may
                        be a very expensive operation to add, so only use as
                        needed.  If set to false, will use cached data.
        """
        chains = self.find_sea_chains(rebuild)
        for sea_chain in chains:
            if sea_chain.count(sea) > 0:
                return sea_chain

        # If no chain was found, build as its own chain.
        return [sea]

    def find_sea_chain_for_sea_info(self, sea_name, lpar_id, rebuild=False):
        """
        Will find the SharedEthernetAdapter chain for a given SEA.  The SEA
        chain is an ordered list of Shared Ethernet Adapters.  The list is
        ordered such that the first element (SEA) is the primary SEA.  The
        second element is the failover SEA.  The third is the next failover
        element.

        :param sea_name: The name of the shared ethernet adapter
        :param lpar_id: The LPAR containing the SEA
        :param rebuild: If set to True, will rebuild the SEA list.  This may
                        be a very expensive operation to add, so only use as
                        needed.  If set to false, will use cached data.
        """
        chains = self.find_sea_chains(rebuild)
        for sea_chain in chains:
            for sea in sea_chain:
                if sea_name == sea.name and lpar_id == sea.vio_server.lpar_id:
                    return sea_chain

        # If no chain was found, return an empty list
        return []

    def find_sea_chain_by_pvea_info(self, sea, rebuild=False):
        """
        Finds an SEA chain by inspecting the passed in SEA's primary VEA
        properties.  A match is made by using the K2 criteria, as described in
        __find_failover_seas.

        :param sea: SEA to inspect primary VEA from
        :param rebuild: Boolean to tell if we should rebuild the chains before
                        looking through them.
        :return: SEA chain associated with passed in SEA's primary VEA info.
        """
        LOG.debug('Enter find_sea_chain_by_pvea_info')

        # Be sure we got something.
        if sea is None:
            LOG.debug('Exit find_sea_chain_by_pvea_info, no SEA')
            return []

        # Look through the chains and find one that has the same PVEA
        # attributes.
        pvea = sea.get_primary_vea()
        for chain in self.find_sea_chains(rebuild):
            # Check for empty chains...
            if chain is None or len(chain) == 0:
                continue

            # Look at the first SEA as the candidate
            cand_sea = chain[0]
            cand_pvea = cand_sea.get_primary_vea()

            # Do the pvea attributes match?
            if (cand_pvea.pvid == pvea.pvid and
                    cand_pvea.vswitch_name == pvea.vswitch_name and
                    len(cand_sea.additional_veas) == len(sea.additional_veas)):
                LOG.debug('Exit find_sea_chain_by_pvea_info, found match: %s' %
                          ['sea %s, lpar %d' % (s.name, s.vio_server.lpar_id)
                          for s in chain])
                return chain

        # Didn't find a matching SEA chain.  Return empty list
        LOG.debug('Exit find_sea_chain_by_pvea_info, no match found')
        return []

    def find_sea_chains(self, rebuild=False):
        """
        Returns a 'list of lists' representing the SEAs on the system.  Each
        element in the primary list represents a SEA chain on the system.  A
        SEA chain is a list of SharedEthernetAdapters.  The element at 0 is
        the primary SEA.  The subsequent elements are the back up adapters.
        The order of the adapters represents the order in which the failover
        would occur.

        :param rebuild: If set to True, will rebuild the SEA list.  This may
                        be a very expensive operation to add, so only use as
                        needed.  If set to false, will use cached data.
        """
        if(self.primary_sea_list is not None and
           not rebuild):
            LOG.debug('Returning cached sea chains')
            return self.primary_sea_list

        # Maintain a list of all the primary SEAs.  This is actually a list of
        # lists.  Each list (within the primary list) will be a sorted 'chain'
        # of SEAs.  The second element within that list would be the failover
        # SEA.  The third would be the third failover (rare).  Etc...
        self.primary_sea_list = []
        seas_already_listed = []
        all_seas = self._list_all_seas()

        # Loop through all of the SEAs.
        for sea in all_seas:
            # See if this SEA has already made it into a list
            if sea in seas_already_listed:
                # Already processed, skip it
                LOG.debug('Skipping sea %s on lpar %d' %
                          (sea.name, sea.vio_server.lpar_id))
                continue

            # We're processing an SEA that hasn't been put in a list yet.
            # Start a list for it.
            LOG.debug('Starting list for sea %s on lpar %d' %
                      (sea.name, sea.vio_server.lpar_id))
            sea_chain = [sea]

            # Append all failover SEAs associated with this one
            sea_chain.extend(self.__find_failover_seas(sea, all_seas))

            # Sort the list such that the available SEAs are first (sorted by
            # trunk priority), then the unavailable SEAs are after that (also
            # sorted by trunk priority).  Note, python sorts False before True,
            # thus the "not" in the sort.
            sea_chain = sorted(sea_chain, key=lambda sea: sea.trunk_pri)

            LOG.debug('Chain built: %s' % ['sea %s, priority %d, lpar_id %d, '
                                           'is_available %s' %
                                           (s.name,
                                            s.trunk_pri,
                                            s.vio_server.lpar_id,
                                            s.is_available())
                                           for s in sea_chain])

            # Now add everything we just processed to seas_already_listed
            seas_already_listed.extend(sea_chain)

            # And add the chain to the main list of chains
            self.primary_sea_list.append(sea_chain)

        return self.primary_sea_list

    def __find_failover_seas(self, sea, all_seas):
        """
        This method takes in an SEA and a list of SEAs and finds all the SEAs
        that are considered failover for the passed in SEA.  K2 considers an
        SEA a failover of another if:
            1) The pvid of the SEAs' primary VEAs match
            2) The vswitch of the SEAs' primary VEAs match
            3) The SEAs have the same number of trunk adapters

        :param sea: SEA of which to look for others that serve as failovers
        :param all_seas: List of SEAs to search for failovers
        :return: List of SEAs considered failover SEAs for the passed in SEA
        """
        LOG.debug('Enter __find_failover_seas')
        ret_list = []

        # Be sure we got something passed in
        if sea is None:
            LOG.debug('Exit __find_failover_seas, no SEA given')
            return []

        # Look through every candidate
        pvea = sea.get_primary_vea()
        for cand_sea in all_seas:
            # Be sure not to include the SEA passed in
            if cand_sea == sea:
                continue

            cand_pvea = cand_sea.get_primary_vea()
            # If we have the same pvid, vswitch, and number of trunk adapters..
            if (cand_pvea.pvid == pvea.pvid and
                    cand_pvea.vswitch_name == pvea.vswitch_name and
                    len(cand_sea.additional_veas) == len(sea.additional_veas)):
                ret_list.append(cand_sea)

        LOG.debug('Exit __find_failover_seas with list %s' %
                  ['sea %s, lpar_id %d' % (s.name, s.vio_server.lpar_id)
                   for s in ret_list])
        return ret_list

    def _list_all_seas(self):
        """
        Returns a list of all of the Shared Ethernet Adapters on the host
        system.
        """
        sea_list = []
        for vio_server in self.vio_servers:
            sea_list.extend(vio_server.get_shared_ethernet_adapters())
        return sea_list

    def find_adapter(self, lpar_id, adapter_name):
        """
        Returns an adapter on a given LPAR.  If it does not exist, it will
        return None.

        :param lpar_id: The lpar identifier
        :param adapter_name: The adapter name
        :return: The adapter if it exists, otherwise None
        """
        for vio_server in self.vio_servers:
            if vio_server.lpar_id != int(lpar_id):
                continue

            # Know that we must be on the right lpar
            for adapter in vio_server.get_all_adapters():
                if adapter.name == adapter_name:
                    return adapter

        return None

    def is_vlan_vswitch_on_sea(self, vlan_id, vswitch_name):
        """
        Returns true if a VLAN/vSwitch combination is on a SEA.  False
        otherwise.  Does not include the Control Channel VLAN IDs.

        :param vlan_id: The VLAN identifier
        :param vswitch_name: The name of the vSwitch
        """
        sea = self.find_sea_for_vlan_vswitch(vlan_id, vswitch_name)
        return sea is not None

    def find_sea_for_vlan_vswitch(self, vlan_id, vswitch_name):
        """
        Returns the SEA that is hosting the VLAN/vSwitch combination.  If
        one is not found, None will be returned.  Does not include the Control
        Channel VLAN IDs.

        :param vlan_id: The VLAN identifier
        :param vswitch_name: The name of the vSwitch
        """
        sea_list = self.find_all_primary_seas()
        for sea in sea_list:
            # See if it is on the sea's primary vea
            if sea.primary_vea.is_vlan_configured(vlan_id, vswitch_name,
                                                  False):
                return sea

            # Next, loop through all the additional VEAs and see if it
            # exists there...
            for vea in sea.additional_veas:
                if vea.is_vlan_configured(vlan_id, vswitch_name, False):
                    return sea

        # Didn't match, return None
        return None

    def find_vio_server(self, lpar_id=0, lpar_name=None):
        '''
        Returns the VIOS on this host which matches the passed in lpar_id
        and/or lpar_name.  Note, if both are passed in, then the VIOS must
        match both attributes.

        :param lpar_id: LPAR id to look for
        :param lpar_name: LPAR name to look for
        :returns: The matching VIOS, None if one isn't found.
        '''
        if not lpar_id and not lpar_name:
            return None

        # If they asked us to search for a specific lpar id
        if lpar_id:
            # Loop through all VIOSes
            for vios in self.vio_servers:
                # If we find a lpar id match
                if vios.lpar_id == lpar_id:
                    # It's only a match if they didn't pass in a lpar name or
                    # if they did and that matches too.
                    if not lpar_name or vios.lpar_name == lpar_name:
                        return vios
                    else:
                        # lpar name was passed in but didn't match
                        return None

            # If we get here, the caller's lpar_id wasn't found
            return None

        # If we get here, they only specified lpar_name
        for vios in self.vio_servers:
            if vios.lpar_name == lpar_name:
                return vios

        # If we get here, no matches were found at all.
        return None

    def to_dictionary(self):
        """
        Creates a dictionary representation of the object
        """

        vio_server_dictionary_list = []
        for vio_server in self.vio_servers:
            vio_server_dictionary_list.append(vio_server.to_dictionary())

        return {'host_name': self.host_name,
                'vio_servers': vio_server_dictionary_list}

    def maintain_available_vid_pool(self):
        """
        Look up all the active vids on the system and remove them
        from the available vid pool to avoid conflict during plug time.

        This method does not return a value, instead it updates internal
        attributes on this object.
        """

        # The list that stores the available vlan ids used for the pvid VEA's
        # port_vlan_id.  Initialized to EVERYTHING available, but
        # maintain_available_vid_pool will remove the vlan ids in use on the
        # VIOS from this list.
        self.available_vid_pool = {}

        # Find all of the vswitch names
        for vios in self.vio_servers:
            for vea in vios.get_all_virtual_ethernet_adapters():
                vswitch_name = vea.vswitch_name
                if not vswitch_name in self.available_vid_pool.keys():
                    self.available_vid_pool[vswitch_name] = range(1, 4095)

        # Find all of the vlan ids in use for each vswitch including port vlan
        # id and addtional 8021Q vlan ids from the PowerVM. Those vids will be
        # removed from the available vid pool.
        for vios in self.vio_servers:
            for vea in vios.get_all_virtual_ethernet_adapters():
                vswitch_name = vea.vswitch_name
                if self.available_vid_pool[vswitch_name].count(vea.pvid) > 0:
                    self.available_vid_pool[vswitch_name].remove(vea.pvid)
                for vid in vea.addl_vlan_ids:
                    if self.available_vid_pool[vswitch_name].count(vid) > 0:
                        self.available_vid_pool[vswitch_name].remove(vid)

    def get_unusable_vlanids(self, vswitch):
        """
        This method will return all vlans on this Host that cannot be used.
        This includes all orphan VEAs' vlans (other than those also configured
        on a usable adapter) and all control channel pvids.  This differs from
        get_reserved_vlanids() because that method also returns primary VEA
        pvid and addl_vlan_ids.

        This method will look across all VIOSes for interrogation

        :param vswitch: Only VEAs on this vswitch are examined.
        :return: A list of VLANs that are exclusively on orphan VEAs or are
                 the pvid of a control channel.
        """
        existing_vlans = []

        # First, gather all of the VLANs that are used - across ALL VIOSes
        for vio_server in self.vio_servers:
            for sea in vio_server.get_shared_ethernet_adapters():
                # Validate vSwitch names - if needed
                if vswitch is not None and\
                        sea.primary_vea.vswitch_name != vswitch:
                    continue

                # Add the primary
                existing_vlans.append(sea.primary_vea.pvid)
                if sea.primary_vea.addl_vlan_ids:
                    existing_vlans.extend(sea.primary_vea.addl_vlan_ids)

                # Walk through the additional veas
                for vea in sea.additional_veas:
                    existing_vlans.append(vea.pvid)
                    if vea.addl_vlan_ids:
                        existing_vlans.extend(vea.addl_vlan_ids)

        # Now walk through each Vio Servers to see the unusable candidates.
        unusable_cands = []
        for vio_server in self.vio_servers:
            # First get all the orphan vlans
            unusable_cands.extend(vio_server.get_vlans_orphan_vea(vswitch))

            # Now loop through and look at control channels
            for sea in vio_server.get_shared_ethernet_adapters():
                # If we're looking for a specific vswitch and this isn't it...
                if vswitch is not None and\
                        sea.primary_vea.vswitch_name != vswitch:
                    # Move on to the next sea
                    continue

                if sea.control_channel is not None:
                    unusable_cands.append(sea.control_channel.pvid)

        # Lastly, return the list of unusable vlans.  They equal the VLANs that
        # are in the unusable_cands but are NOT in the existing vlans
        unusable_vlans = []
        for unusable_cand in unusable_cands:
            # Don't add if it is on the system already...
            if unusable_cand in existing_vlans:
                continue

            unusable_vlans.append(unusable_cand)
        return unusable_vlans


class VioServer(powervm_dto_models.VioServerDTO):

    """
    Represents a single Virtual IO Server.
    """
    __tablename__ = TBL.vio_server
    __table_args__ = ()

    def __init__(self, lpar_name, lpar_id, host_name, state, rmc_state):
        """
        Creates a Virtual IO Server object.

        :param lpar_name: The LPAR Name for the system as presented on the Host
                          itself.
        :param lpar_id: The LPAR identifier as presented on the Host itself.
        :param host_name: The host that this mapping applies to.  Should be a
                          string that identifies the host.
        :param state: State of the VIOS
        :param rmc_state: State of the RMC connection
        """
        self._host_name = host_name

        lpar_id = int(lpar_id)

        # Invoke super class constructor
        powervm_dto_models.VioServerDTO.__init__(self,
                                                 lpar_name=lpar_name,
                                                 lpar_id=lpar_id,
                                                 host_name=host_name,
                                                 state=state,
                                                 rmc_state=rmc_state)

    def add_adapter(self, adapter):
        """
        Add an adapter (VEA or SEA) to the existing list of adapters on this
        VioServer.  This function will prevent duplicate adapters from being
        added to the VioServer.

        :param adapter: The adapter (either Virtual or Shared) to add
        """
        if isinstance(adapter, SharedEthernetAdapter):
            for sea in self.sea_list:
                if sea.name == adapter.name:
                    self.sea_list.remove(sea)
                    break
            self.sea_list.append(adapter)
        elif isinstance(adapter, VirtualEthernetAdapter):
            for vea in self.vea_list:
                if vea.name == adapter.name:
                    self.vea_list.remove(vea)
                    break
            self.vea_list.append(adapter)

    def remove_adapter(self, adapter):
        """
        Removes an adapter (VEA or SEA) to the existing list of adapters on
        this VioServer.

        :param adapter: The adapter (either Virtual or Shared) to remove
        """
        if isinstance(adapter, SharedEthernetAdapter):
            for sea in self.sea_list:
                if sea.name == adapter.name:
                    self.sea_list.remove(sea)
                    break
        elif isinstance(adapter, VirtualEthernetAdapter):
            for vea in self.vea_list:
                if vea.name == adapter.name:
                    self.vea_list.remove(vea)
                    break

    def get_shared_ethernet_adapters(self):
        """
        Provides a list of all the SharedEthernetAdapters on the system.
        """
        return shallow_copy_as_ordinary_list(self.sea_list)

    def get_all_virtual_ethernet_adapters(self):
        """
        Provides a list of all the VirtualEthernetAdapters on the system.
        """
        return shallow_copy_as_ordinary_list(self.vea_list)

    def get_all_adapters(self):
        """
        Provides an aggregate list of all of the adapters on the VioServer.
        """
        aggregate = []
        aggregate.extend(self.vea_list)
        aggregate.extend(self.sea_list)
        return aggregate

    def get_sea_for_vea(self, vea):
        """
        Will return the SharedEthernetAdapter that owns a corresponding
        VirtualEthernetAdapter.  If the VirtualEthernetAdapter provided does
        not have a valid owning SharedEthernetAdapter, None will be returned.

        :param vea: The VirtualEthernetAdapter to search for.
        """

        if not isinstance(vea, VirtualEthernetAdapter):
            ras.function_tracepoint(LOG,
                                    __name__,
                                    ras.TRACE_ERROR,
                                    ras.vif_get_msg('error',
                                                    'OBJECT_INVALIDINST') %
                                    {'classname': 'VirtualEthernetAdapter'})
            raise excp.IBMPowerVMInvalidVEAConfig(attr=repr(vea))

        for adapter in self.sea_list:
            if adapter.is_managing_vea(vea):
                return adapter

        # Must not have found the appropriate adapter.
        return None

    def get_orphan_virtual_ethernet_adapters(self, vSwitch=None):
        """
        Provides a list of all of the Orphan Virtual Ethernet Adapters present
        on the system.

        An Orphan VEA is one that is not 'owned' by a Shared Ethernet Adapter.
        """
        vea_list = []
        for adapter in self.vea_list:
            # Check to make sure that the vSwitches match
            if vSwitch is not None and vSwitch != adapter.vswitch_name:
                continue

            # Check to make sure that the adapter is a VEA and that there is
            # not an owning SEA.
            if self.get_sea_for_vea(adapter) is None:
                vea_list.append(adapter)

        return vea_list

    def is_vlan_configured(self, vlan_id, vswitch=None):
        """
        Check whether a client VLAN id has been configured on this VIOS
        partition. It checks both pvid_adapter, ctl_channel and virt_adapters
        on all VEAs on the system.

        :param vlan_id: VLAN id
        :param vswitch: The vSwitch for the virtual network.  If None, will
                        ignore vSwitch checking.
        :returns: True or False

        :throws IBMPowerVMVIDPVIDConflict: Thrown if the VLAN ID being queried
                                           is a VLAN on a PVID on an existing
                                           addl_vlan
        :throws IBMPowerVMVIDisCtlChannel: Thrown if the VLAN is on this VEA,
                                           but this VEA is the ctl_channel VEA
        """
        vea_list = self.get_all_virtual_ethernet_adapters()
        for vea in vea_list:
            if vea.is_vlan_configured(vlan_id, vswitch):
                return True
        return False

    def get_reserved_vlanids_dict(self, vswitch=None):
        """
        Returns a dictionary of all the reserved vlanids on the VIOS associated
        with their SEAs (could be orphan VEAs as well) Vlanids are "reserved"
        if they either a) belong to an orphaned VEA or b) belong to the primary
        VEA of an SEA.

        :returns reserved_vlan_dict: a dictionary of reserved VLANs with their
        corresponding SEA/Orphan VEA details
        {orphan: [1,2,4], ent12: [7], ent3: [5,6]}
        orphan is a keyword used to denote a Virtual Ethernet adapter that does
        not have a parent Shared Ethernet Adapter.
        """

        reserved_vlan_dict = {}
        reserved_adapters = []
        # A reserved adapter is an orphan VEA, primary VEA on a SEA, or a
        # control channel.
        orphan_vlans = self.get_vlans_orphan_vea(vswitch)
        reserved_vlan_dict['orphan'] = orphan_vlans
        for sea in self.get_shared_ethernet_adapters():
            # Build an empty list for each SEA so that the next loop can extend
            # the elements within it.
            reserved_vlan_dict[sea.name] = []

            # The SEAs should check both the Primary VEA and the control
            # channel VEA
            if vswitch:
                if sea.get_primary_vea().vswitch_name == vswitch:
                    reserved_adapters.append(sea.primary_vea)
            else:
                reserved_adapters.append(sea.primary_vea)

            if sea.control_channel:
                if (vswitch is None or vswitch ==
                        sea.control_channel.vswitch_name):
                    reserved_adapters.append(sea.control_channel)

        # Loop through all the reserved adapters
        for adapter in reserved_adapters:
            # Reserve the pvid
            reserved_vlanids = []
            reserved_vlanids.append(adapter.pvid)
            # Reserve addl_vlan_ids, if there are any
            if adapter.addl_vlan_ids:
                reserved_vlanids.extend(adapter.addl_vlan_ids)
            # The adapters are VEAs - Control Channel Ones too? Hence we need
            # to find out the SEA.
            reserved_vlan_dict[self.get_sea_for_vea(adapter).name]\
                .extend(reserved_vlanids)

        return reserved_vlan_dict

    def get_reserved_vlanids(self, vswitch=None):
        """
        Returns a list of all the reserved vlanids on the VIOS.  Vlanids are
        "reserved" if they either a) belong to an orphaned VEA or b) belong
        to the primary VEA of an SEA.

        :returns reserved_vlanids: a list of integers representing all the
                                   reserved vlanids on the VIOS
        """
        reserved_vlanids = []
        # The Dictionary is obtained for the reserved VLANs and parsed
        # to form a list of VLANs. This gives the consumer of this API
        # easy access to a flat list of reserved VLANs.
        reserved_vlan_dict = self.get_reserved_vlanids_dict(vswitch)
        for adpt in reserved_vlan_dict:
            reserved_vlanids.extend(reserved_vlan_dict.get(adpt, []))
        # sort the vlans for proper order back to user
        reserved_vlanids.sort()

        return reserved_vlanids

    def get_vlans_orphan_vea(self, vswitch=None):
        """
        This method is used to fetch the Orphan VEA VLANs. This optionally
        takes in the vswitch information to search VLANs on a Orphan VEA for
        a particular Vswitch

        :param: vswitch: The VEA connected to the VSwitch
        :return: a list of VLANs hosted on the orphan VEAs of the host
        """
        # Need to first get the 'used' VLANs.  A VLAN may be 'orphaned' and
        # also used simulatenously.  We don't want an orphan to overlap with
        # a used VLAN...so we gather a list of all used and only add the
        # orphan if it is not in this list
        used_vlans = []
        for sea in self.get_shared_ethernet_adapters():
            # Skip if the vSwitches don't line up
            if vswitch and vswitch != sea.primary_vea.vswitch_name:
                continue

            # Add the primary
            used_vlans.append(sea.primary_vea.pvid)
            if sea.primary_vea.addl_vlan_ids:
                used_vlans.extend(sea.primary_vea.addl_vlan_ids)

            # Walk through the additional veas
            for vea in sea.additional_veas:
                used_vlans.append(vea.pvid)
                if vea.addl_vlan_ids:
                    used_vlans.extend(vea.addl_vlan_ids)

        # Now walk through the standard orphans
        orphan_adapters = self.get_orphan_virtual_ethernet_adapters()
        orphan_vlans = []
        for adapter in orphan_adapters:
            if vswitch and adapter.vswitch_name != vswitch:
                continue

            # These are potential orphans...but need to be cross checked
            # to the used_vlans
            potential_adds = [adapter.pvid]
            if adapter.addl_vlan_ids:
                potential_adds.extend(adapter.addl_vlan_ids)

            # Now for each potential add, add it
            for potential_add in potential_adds:
                if potential_add not in used_vlans:
                    orphan_vlans.append(potential_add)

        return orphan_vlans

    def to_dictionary(self):
        """
        Creates a dictionary representation of the object
        """

        adapter_dictionary_list = []
        for adapter in self.sea_list:
            adapter_dictionary_list.append(adapter.to_dictionary())
        for adapter in self.vea_list:
            adapter_dictionary_list.append(adapter.to_dictionary())

        return {
            'lpar_name': self.lpar_name,
            'lpar_id': self.lpar_id,
            'adapter_list': adapter_dictionary_list,
            'state': self.state,
            'rmc_state': self.rmc_state
        }


class Adapter(object):

    """
    The Adapter provides a common base class for network adapters such as
    SEA and VEA, to hold common attributes.
    """

    # The owning VioServer for this adapter
    _vio_server = None

    def __init__(self, name, slot, vio_server):
        """
        __init__ method for Class Adapter

        :param name: the adapter device name
        :param slot: the virtual slot number
        :param vio_server: the owning VIOS of this adapter
        """

        # Check device name. Cannot be empty.
        if not name:
            ras.function_tracepoint(LOG,
                                    __name__,
                                    ras.TRACE_ERROR,
                                    ras.vif_get_msg('error',
                                                    'DEVNAME_INVALID') %
                                    {'devname': name})
            raise excp.IBMPowerVMInvalidVETHConfig(attr='devname: %s' % name)

        # On IBMPowerVM slot number 0 is reserved for vsa0 device and the
        # slot number cannot go negative.
        if slot < 1:
            ras.function_tracepoint(LOG,
                                    __name__,
                                    ras.TRACE_ERROR,
                                    ras.vif_get_msg('error',
                                                    'SLOT_INVALIDNUMBER') %
                                    {'slotnum': slot})
            raise excp.IBMPowerVMInvalidVETHConfig(attr=str(slot))

        # Add the adapter to the vios
        self._vio_server = vio_server
        self._vio_server.add_adapter(self)


class VirtualEthernetAdapter(Adapter,
                             powervm_dto_models.VirtualEthernetAdapterDTO):

    """
    for IVM's SEA pvid_adapter, do not configure it as 8021Q, only set pvid
    for the VEA. This prevents any IVM network disruption since
    no addl_vlan_ids can be added to the pvid_adapter.

    For HMC managed VIOS, addl_vlan_ids can be updated dynamically for
    pvid_adapter

    The VEA is an abstraction of a virtual ethernet device(VethDevice)
    on VIOS/IVM which has an AIX device name. It is the related entity
    of veth device living in a LPAR's OS context.
    """
    __tablename__ = TBL.virtual_ethernet_adapter
    __table_args__ = ()

    def __init__(self, name, vio_server, slot, pvid, is_trunk, trunk_pri,
                 state, ieee_eth, vswitch_name, addl_vlan_ids):
        """
        __init__ method for Class VirtualEthernetAdapter.

        :param name: Vea device name
        :param vio_server: Virtual I/O Server this VEA is owned by
        :param slot: VEA's virtual slot number
        :param pvid: untagged vlan id for the VEA
        :param is_trunk: whether this VEA is a bridging VEA or not
        :param state: the current states are Defined and Available
        :param trunk_pri: VEA's trunk priority
        :param ieee_eth: whether the VEA is 8021Q enabled or not
        :param vswitch_name: The name of the IBMPowerVM vswitch_name that VEA
                connects to. The default vswitch_name is ETHERNET0
        :param addl_vlan_ids:  VEA's 8021Q vlan tag list
        """

        ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG, "Enter")

        # Trunk priority should be 0 or greater
        if trunk_pri < 0:
            ras.function_tracepoint(LOG,
                                    __name__,
                                    ras.TRACE_ERROR,
                                    ras.vif_get_msg('error',
                                                    'TRUNK_PRI_OUTOFRANGE') %
                                    {'trunkpri': trunk_pri})
            raise excp.IBMPowerVMInvalidVETHConfig(attr='trunk_pri: %d' %
                                                   trunk_pri)

        # Validate pvid is in range
        if not self._is_valid_vlan_id(pvid):
            ras.function_tracepoint(LOG,
                                    __name__,
                                    ras.TRACE_ERROR,
                                    'port_vlan_id: ' +
                                    ras.vif_get_msg('error',
                                                    'VLAN_OUTOFRANGE') %
                                    {'vlanid': pvid})
            raise excp.IBMPowerVMInvalidVETHConfig(attr='pvid: %d' % pvid)

        # Validate state is a string
        if not state or not isinstance(state, basestring) or len(state) == 0:
            ras.function_tracepoint(LOG,
                                    __name__,
                                    ras.TRACE_ERROR,
                                    'state: ' +
                                    ras.vif_get_msg('error',
                                                    'SEA_INVALIDSTATE'))
            raise excp.IBMPowerVMInvalidVETHConfig(attr='state: %s' %
                                                   str(state))

        if not vswitch_name or vswitch_name == '':
            ras.function_tracepoint(LOG,
                                    __name__,
                                    ras.TRACE_ERROR,
                                    ras.vif_get_msg('error', 'VSWITCH_NULL'))
            raise excp.IBMPowerVMInvalidVETHConfig(attr='vswitch: %s' %
                                                   str(vswitch_name))

        # Validate addl_vlan_ids is not null.  An empty list is fine,
        # but if it's not empty, validate each addl_vlan_id in the list.
        if (addl_vlan_ids is None or
                (len(addl_vlan_ids) > 0 and
                    (not isinstance(addl_vlan_ids, list) or
                     (False in (self._is_valid_vlan_id(x)
                                for x in addl_vlan_ids))))):
            ras.function_tracepoint(LOG,
                                    __name__,
                                    ras.TRACE_ERROR,
                                    ras.vif_get_msg('error',
                                                    'ADDLVLANIDS_INVALID') %
                                    {'addlvlans': str(addl_vlan_ids)})
            raise excp.IBMPowerVMInvalidVETHConfig(attr='addl_vlan_ids: %s' %
                                                   str(addl_vlan_ids))

        # Invoke super class constructors
        powervm_dto_models.VirtualEthernetAdapterDTO.__init__(
            self,
            name,
            vio_server,
            slot=slot,
            pvid=pvid,
            vswitch_name=vswitch_name,
            addl_vlan_ids=addl_vlan_ids,
            state=state,
            ieee_eth=ieee_eth,
            trunk_pri=trunk_pri,
            is_trunk=is_trunk)
        Adapter.__init__(self, name, slot, vio_server)

        ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG, "Exit")

    def is_pvid_adapter(self):
        """
        Will determine if this adapter is the primary VEA on a SEA.
        """
        owning_sea = self.vio_server.get_sea_for_vea(self)
        if not owning_sea:
            return False

        return owning_sea.get_primary_vea().name == self.name

    def is_ctl_channel_adapter(self):
        """
        Will determine if this adapter is the ctl_channel VEA on a SEA.
        """
        owning_sea = self.vio_server.get_sea_for_vea(self)
        if not owning_sea:
            return False

        if not owning_sea.control_channel:
            return False

        return owning_sea.control_channel.name == self.name

    def is_vlan_configured(self, vlan_id, vswitch_name=None,
                           throw_error=True):
        """
        Check whether a VLAN id has been configured on VEA. It checks both
        pvid_adapter, ctl_channel and virt_adapters under the VEA.

        :param vlan_id: VLAN id
        :param vswitch: The vSwitch for the virtual network.  If None, will
                        ignore vSwitch checking.
        :param throw_error: If set to true, will throw the errors noted below.
                            If false, and a PVID or Control channel VLAN is
                            found, will simply return true.
        :returns: True or False

        :throws IBMPowerVMVIDPVIDConflict: Thrown if the VLAN ID being queried
                                           is a VLAN on a PVID on an existing
                                           addl_vlan
        :throws IBMPowerVMVIDisCtlChannel: Thrown if the VLAN is on this VEA,
                                           but this VEA is the ctl_channel VEA
        """
        ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG, "Enter")

        # Not the same domain if the vSwitches on the system do not match
        if vswitch_name and vswitch_name != self.vswitch_name:
            return False

        # Runtime checking to ensure data integrity of the ieee VLANs
        if ((not self.ieee_eth and len(self.addl_vlan_ids) > 0)):
            # if the vea is not ieee, then it cannot have 8021Q vlans
            ras.function_tracepoint(LOG,
                                    __name__,
                                    ras.TRACE_ERROR,
                                    ras.vif_get_msg('error',
                                                    'OBJECT_INVALIDATTR') %
                                    {'objname': 'self.veth',
                                     'attrs': str(self)})

            # If we are not to throw an error, but its a PVID, return 'True'
            # that this VLAN is configured.
            if not throw_error:
                return True

            raise excp.IBMPowerVMInvalidVETHConfig(attr=self.name)

        # The VLAN may be attached to either the primary VEA (either as its
        # PVID or via additional VLANS) or by an 'addl' vea
        if self.is_pvid_adapter():
            # The primary adapter may support either its PVID
            if vlan_id == self.pvid:
                ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG, "Exit")
                return True

            # Or in its additional VLAN list
            ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG, "Exit")
            return vlan_id in self.addl_vlan_ids

        # Check to see if this is the Ctl Channel adapter
        elif self.is_ctl_channel_adapter():
            # The control channel adapter can't be used at all.  Check the
            # PVID then the addl vlans
            msg = ras.vif_get_msg('error', ('VLANID_CTL_CHANNEL_CONFLICT') %
                                  {'vlanid': vlan_id})
            if vlan_id in self.addl_vlan_ids or vlan_id == self.pvid:
                ras.function_tracepoint(LOG, __name__, ras.TRACE_ERROR, msg)

                # If we are not to throw an error, but its a ctl channel,
                # return 'True' that this VLAN is configured.
                if not throw_error:
                    return True

                raise excp.IBMPowerVMVIDisCtlChannel()

        # Below are non pvid adapter cases.
        elif vlan_id == self.pvid:
            # non pvid_adapter VEA's port_vlan_id is picked randomly from the
            # VID range above 4000
            ras.function_tracepoint(LOG,
                                    __name__,
                                    ras.TRACE_ERROR,
                                    ras.vif_get_msg('error',
                                                    'VLANID_CONFLICT') %
                                    {'vlanid': vlan_id,
                                     'pvid': self.pvid})

            # If we are not to throw an error, but its a PVID, return 'True'
            # that this VLAN is configured.
            if not throw_error:
                return True

            raise excp.IBMPowerVMVIDPVIDConflict(vea=self)
        elif vlan_id in self.addl_vlan_ids:
            ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG, "Exit")
            return True
        else:
            ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG, "Exit")
            return False

    def set_vea_pvid(self, pvid):
        """
        Set the VEA's untagged port vlan id.

        :param pvid: untagged port vlan id
        """
        ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG, "Enter")
        self.pvid = pvid
        ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG, "Exit")

    def add_vea_vlan_tag(self, vlan_id):
        """
        Add the 8021Q VLAN tag on the VEA. It is stored in The
        addl_vlan_ids list of the underline VethDevice object.

        :param vlan_id: 8021Q VLAN tag id.
        """

        ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG, "Enter")
        if vlan_id in self.addl_vlan_ids:
            pass
        elif (len(self.addl_vlan_ids) < VEA_MAX_VLANS):
            self.addl_vlan_ids.append(vlan_id)
        else:
            ras.function_tracepoint(LOG,
                                    __name__,
                                    ras.TRACE_ERROR,
                                    ras.vif_get_msg('error',
                                                    'VEA_8021QVLANFULL') %
                                    {'vethname': self.name,
                                     'slot': self.slot,
                                     'addlvlans': str(self.addl_vlan_ids)
                                     })
            raise excp.IBMPowerVMVEATooManyVlan()
        ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG, "Exit")

    def to_dictionary(self):
        """Creates a dictionary representation of the object"""
        return {
            'name': self.name,
            'slot': self.slot,
            'is_trunk': self.is_trunk,
            'trunk_pri': self.trunk_pri,
            'pvid': self.pvid,
            'state': self.state,
            'ieee_eth': self.ieee_eth,
            'vswitch': self.vswitch_name,
            'addl_vlan_ids': self.addl_vlan_ids,
            'type': 'VirtualEthernetAdapter'
        }

    def _is_valid_vlan_id(self, vlan_id):
        """
        check whether vlan_id is in the range of [1,4094]

        :param vlan_id: vlan id to validate
        :returns: True or False
        """
        return True if vlan_id and (vlan_id > 0 and vlan_id < 4095) else False


class SharedEthernetAdapter(Adapter,
                            powervm_dto_models.SharedEthernetAdapterDTO):

    """
    SharedEthernetAdapter is a logic entity that contains one pvid adapter
    and bunch of virtual adapters. The SharedEthernetAdapter is a one to one
    mapping to the SEA device configured on VIOS. The PVID adapter will be
    configured as non 8021Q VEA(ieee_eth=False). The untagged port vlan id for
    the SEA cannot be updated dynamically. And pvid adapter is the minimal
    requirement for SEA device. The additional virtual adapters can be
    either 8021Q VEA or non 8021Q VEA or a mix. The virtual adapters can
    be updated dynamically without reconfigure the SEA device.

    Information stored within this object
    pvid_vea # The primary Virtual Ethernet Adapter (VirtualEthernetAdapter)
    virt_vea_dict # The dictionary of additional VEAs (VirtualEthernetAdapter)
    name # The name of the device
    """
    __tablename__ = 'adapter_ethernet_shared'
    __table_args__ = ()

    def __init__(self, name, vio_server, slot, state, primary_vea,
                 control_channel=None, additional_veas=None):
        """
        __init__ method for SeaDevice Class

        :param name: Sea device name
        :param vios_server: Virtual I/O Server this SEA is owned by
        :param slot: SEA's virtual slot number
        :param state: the current state are Defined and Available
        :param primary_vea: the preferred VirtualEthernetAdapter
        :param control_channel: the VirtualEthernetAdapter used for management
        :param additional_veas: other VirtualEthernetAdapters on this SEA
        """

        ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG, "Enter")

        # Validate state is a string
        if not state or not isinstance(state, basestring) or len(state) == 0:
            ras.function_tracepoint(LOG,
                                    __name__,
                                    ras.TRACE_ERROR,
                                    'state: ' +
                                    ras.vif_get_msg('error',
                                                    'SEA_INVALIDSTATE'))
            raise excp.IBMPowerVMInvalidSEAConfig(attr=str(state))

        # Validate the primary_vea type
        if(not primary_vea or
           not isinstance(primary_vea, VirtualEthernetAdapter)):
            ras.function_tracepoint(LOG,
                                    __name__,
                                    ras.TRACE_ERROR,
                                    ras.vif_get_msg('error', 'SEA_ERROR') %
                                    {'devname': 'primary_vea',
                                     'pvidvea': 'None'})
            raise excp.IBMPowerVMInvalidSEAConfig()
        # Invoke super class constructors
        powervm_dto_models.SharedEthernetAdapterDTO.__init__(
            self,
            name,
            vio_server,
            primary_vea,
            slot=slot,
            state=state,
            control_channel=control_channel,
            additional_veas=additional_veas)
        Adapter.__init__(self, name, slot, vio_server)

        ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG, "Exit")

    def get_primary_vea(self):
        """
        Returns this SEA's primary VEA that.
        """
        return self.primary_vea

    def is_available(self):
        """
        Aggregates the various states (SEA state, VIOS's state and rmc_state)
        and returns a boolean indicating the "availableness" of this SEA.
        """
        if self.vio_server:
            return self.vio_server.state.lower() == 'running' and \
                self.vio_server.rmc_state.lower() == 'active' and \
                self.state.lower() == 'available'
        else:
            return self.state.lower() == 'available'

    def is_managing_vea(self, vea):
        """
        Inspects all VEAs on this SEA for a specific VEA.
        """
        if not vea:
            return False

        if self.get_primary_vea() and self.get_primary_vea().name == vea.name:
            return True

        if self.control_channel and self.control_channel.name == vea.name:
            return True

        if self.additional_veas:
            for additional_vea in self.additional_veas:
                if additional_vea.name == vea.name:
                    return True

        return False

    def add_vea_to_sea(self, vea):
        """
        Add VeaDevice object to SEA device.

        :param vea: A VeaDevice object to add
        """
        # add a VEA to SEA's virt_adapters
        ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG, "Enter")

        if not vea or not isinstance(vea, VirtualEthernetAdapter):
            ras.function_tracepoint(LOG,
                                    __name__,
                                    ras.TRACE_ERROR,
                                    ras.vif_get_msg('error',
                                                    'OBJECT_INVALIDINST') %
                                    {'classname': 'IBMPowerVMVEADecice'})
            raise excp.IBMPowerVMInvalidVETHConfig(attr=self.name)

        name = vea.name

        if self.primary_vea.name == name:
            # virt vea has been configured on PVID VEA, this is wrong
            ras.function_tracepoint(LOG,
                                    __name__,
                                    ras.TRACE_ERROR,
                                    ras.vif_get_msg('error', 'SEA_VEACFG') %
                                    {'veaname': name,
                                     'seaname': self.name})
            raise excp.IBMPowerVMInvalidSEAConfig()

        # Can't just do a list.append() here because of SQL Alchemy
        additional_veas = self.additional_veas
        additional_veas.append(vea)
        self.additional_veas = additional_veas
        ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG, "Exit")

    def remove_vea_from_sea(self, vea):
        """
        Removes a VirtualEthernetAdapter from the SEA.

        :param vea: A VirtualEthernetAdapter device to remove.
        """
        ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG, "Enter")

        if not vea or not isinstance(vea, VirtualEthernetAdapter):
            ras.function_tracepoint(LOG,
                                    __name__,
                                    ras.TRACE_ERROR,
                                    ras.vif_get_msg('error',
                                                    'OBJECT_INVALIDINST') %
                                    {'classname': 'IBMPowerVMVEADecice'})
            raise excp.IBMPowerVMInvalidVETHConfig(attr=self.name)

        name = vea.name

        if self.primary_vea.name == name:
            # Can't remove the primary vea
            ras.function_tracepoint(LOG,
                                    __name__,
                                    ras.TRACE_ERROR,
                                    ras.vif_get_msg('error', 'SEA_VEACFG') %
                                    {'veaname': name,
                                     'seaname': self.name})
            raise excp.IBMPowerVMInvalidSEAConfig()

        # Can't just do a list.remove() here because of SQL Alchemy
        additional_veas = self.additional_veas
        vea_to_remove = None
        for additional_vea in additional_veas:
            if additional_vea.name == vea.name:
                vea_to_remove = additional_vea
                break
        if vea_to_remove:
            additional_veas.remove(vea_to_remove)
        self.additional_veas = additional_veas
        ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG, "Exit")

    def get_vea_by_vlan_id(self, vlan_id, throw_error=True):
        """
        Fetch the VeaDevice object based on the VLAN id.

        :param vlan_id: VLAN id
        :param throw_error: Whether exceptions should be raised when found.
        :returns: VeaDevice object or None
        """
        ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG, "Enter")
        if self.get_primary_vea().is_vlan_configured(vlan_id,
                                                     throw_error=throw_error):
            return self.get_primary_vea()
        else:
            for vea in self.additional_veas:
                if vea.is_vlan_configured(vlan_id, throw_error=throw_error):
                    ras.function_tracepoint(LOG, __name__,
                                            ras.TRACE_DEBUG, "Exit")
                    return vea
            ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG, "Exit")
            return None

    def get_least_used_vea(self):
        """
        Scan the current SeaDevice object to find the VEA that has least
        number of vlans bridged. A null could be returned if the SEA doesn't
        have any VEA yet or it has reached its full bridging capacity.

        :returns: VeaDevice object or None.
        """
        ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG, "Enter")
        try:
            # if there are no additional veas, return None
            if not self.additional_veas or len(self.additional_veas) == 0:
                return None

            least_used_vea = self.additional_veas[0]
            vea_8021Q_vlan_counts = len(least_used_vea.addl_vlan_ids)

            for vea in self.additional_veas:
                if len(vea.addl_vlan_ids) < vea_8021Q_vlan_counts:
                    least_used_vea = vea
                    vea_8021Q_vlan_counts = len(vea.addl_vlan_ids)
                else:
                    continue

            ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG, "Exit")
            # found the least used vea
            return least_used_vea
        except KeyError:
            # virt_vea_dict is empty
            ras.trace(LOG, __name__, ras.TRACE_ERROR,
                      ras.vif_get_msg('error', 'SEA_NO_VEA') %
                      {'seaname': self.name})
            return None

    @property
    def pvid(self):
        """
        Derived attribute: Returns the PVID of the _primary_vea
        """
        return self.get_primary_vea().pvid

    @property
    def trunk_pri(self):
        """
        Derived attribute: Returns the trunk priority of the _primary_vea
        """
        return self.get_primary_vea().trunk_pri

    def additional_vlans(self):
        """
        Reports the aggregated additional VLANs of all VEAs of a given SEA
        """
        ret_list = list(self.get_primary_vea().addl_vlan_ids)
        for addl_vea in self.additional_veas:
            ret_list.extend(addl_vea.addl_vlan_ids)
        return ret_list

    def to_dictionary(self):
        """
        Creates a dictionary representation of the object
        """

        additional_vea_names = []
        if self.additional_veas:
            additional_vea_names = [x.name for x in self.additional_veas]

        if self.control_channel is None:
            ctrl_chnl_name = 'None'
        else:
            ctrl_chnl_name = self.control_channel.name
        return {
            'name': self.name,
            'slot': self.slot,
            'state': self.state,
            'primary_vea': self.primary_vea.name,
            'control_channel': ctrl_chnl_name,
            'additional_veas': additional_vea_names,
            'type': 'SharedEthernetAdapter'
        }


class NetworkAssociation(powervm_dto_models.NetworkAssociationDTO):

    """
    Represents a single association between a given Host and its corresponding
    Shared Ethernet Adapter.
    """
    __tablename__ = TBL.network_association
    __table_args__ = ()

    def __init__(self, host_name, neutron_net_id, sea=None):
        """
        Creates a Network Association.  This association dictates which VIOS/
        Shared Ethernet Adapter is to be used for provisioning operations.

        :param host_name: The host that this mapping applies to.  Should be a
                          string that identifies the host.
        :param neutron_net_id: The reference to the Neutron Networks UUID.
                               Should be a string value.
        :param sea: The SharedEthernetAdapter that should be used.  Must reside
                    on the vios previously passed in.  If set to None,
                    indicates that this association should NOT allow VMs to be
                    placed on the Host defined above for the network specified.
        """
        # Invoke super class constructors
        powervm_dto_models.NetworkAssociationDTO.__init__(self,
                                                          host_name,
                                                          neutron_net_id,
                                                          sea)

    def to_dictionary(self):
        """
        Creates a dictionary representation of the object
        """
        return {
            'host_name': self._host_name,
            'neutron_net_id': self.neutron_net_id,
            'sea': self.sea.to_dictionary()
        }

#

"""
    Represents a non-persistent version of the DOM models from
    powervc_nova/db/network.  This should be a temporary set of classes until
    the database DOM objects can handle being created without requiring
    a persistence statement.
"""


class NetworkAssociationNoDB(NetworkAssociation):

    """
    Represents a NetworkAssociation class without the requirement of
    persistence.
    """

    def __init__(self, host_name, neutron_net_id, sea=None):
        NetworkAssociation.__init__(self, host_name, neutron_net_id, sea)

    def save(self, context, session=None):
        pass

    def delete(self, context, session=None):
        pass


class VioServerNoDB(VioServer):

    """
    Represents a VioServerNoDB class without the requirement of persistence.
    """

    def __init__(self, lpar_name, lpar_id, host_name, state, rmc_state):
        VioServer.__init__(self,
                           lpar_name=lpar_name,
                           lpar_id=lpar_id,
                           host_name=host_name,
                           state=state,
                           rmc_state=rmc_state)

    def save(self, context, session=None):
        pass

    def delete(self, context, session=None):
        pass


class VirtualEthernetAdapterNoDB(VirtualEthernetAdapter):

    """
    Represents a VirtualEthernetAdapter class without the requirement of
    persistence.
    """

    def __init__(self, name, vio_server, slot, pvid, is_trunk, trunk_pri,
                 state, ieee_eth, vswitch_name, addl_vlan_ids):
        self._primary_vea_pk_id = 20
        self._pk_id = 22
        VirtualEthernetAdapter.__init__(self, name, vio_server, slot, pvid,
                                        is_trunk, trunk_pri, state, ieee_eth,
                                        vswitch_name, addl_vlan_ids)

    def save(self, context, session=None):
        pass

    def delete(self, context, session=None):
        pass


class SharedEthernetAdapterNoDB(SharedEthernetAdapter):

    """
    Represents a SharedEthernetAdapter class without the requirement of
    persistence.
    """

    def __init__(self, name, vio_server, slot, state, primary_vea,
                 control_channel=None, additional_veas=None):
        self._primary_vea_pk_id = 15
        # Add a local No-DB attribute to override SEA-DTO CtrlChannel logic
        self._control_channel = None
        SharedEthernetAdapter.__init__(self, name, vio_server, slot, state,
                                       primary_vea, control_channel,
                                       additional_veas)

    @property
    def control_channel(self):
        """Override SEA-DTO to a use local No-DB attr instead of cc_pk_id."""
        return self._control_channel

    @control_channel.setter
    def control_channel(self, vea):
        """Override SEA-DTO to a use local No-DB attr instead of cc_pk_id."""
        if self._control_channel != vea:
            if vea is not None:
                if vea in self.additional_veas:
                    errmsg = ("Tried setting SEA's Control-Channel to a VEA "
                              "that is already in SEA's Additional-VEA list.")
                    LOG.error(errmsg)
                    raise ValueError(errmsg)
                elif vea not in self._all_veas:
                    self._all_veas.append(vea)
            if self._control_channel is not None:
                self._all_veas.remove(self._control_channel)
            self._control_channel = vea

    def save(self, context, session=None):
        pass

    def delete(self, context, session=None):
        pass


class No_DB_DOM_Factory(DOM_Factory):

    """
    Used to create the DOM objects internally.  Main purpose is to have the
    ability to create DOM objects without requiring the need for a backing
    database.  Once the DB is robust enough to handle creating objects
    without requiring a degree of persistence, this can be removed.
    """

    def create_network_association(self, host_name, neutron_net_id, sea=None):
        """
        Creates a DOM Network Association object.

        :param host_name: The host that this mapping applies to.  Should be a
                          string that identifies the host.
        :param neutron_net_id: The reference to the Neutron Networks UUID.
                               Should be a string value.
        :param sea: The SharedEthernetAdapter that should be used.  Must reside
                    on the vios previously passed in.  If set to None,
                    indicates that this association should NOT allow VMs to be
                    placed on the Host defined above for the network specified.
        """
        nwkasn = NetworkAssociationNoDB(host_name, neutron_net_id, sea)
        sqla_orm_session.make_transient(nwkasn)
        return nwkasn

    def create_vea(self, name, vio_server, slot, pvid, is_trunk, trunk_pri,
                   state, ieee_eth, vswitch_name, addl_vlan_ids):
        """
        Creates a DOM VirtualEthernetAdapter object.

        :param name: Vea device name
        :param vio_server: Virtual I/O Server this VEA is owned by
        :param slot: VEA's virtual slot number
        :param pvid: untagged vlan id for the VEA
        :param is_trunk: whether this VEA is a bridging VEA or not
        :param state: the current states are Defined and Available
        :param trunk_pri: VEA's trunk priority
        :param ieee_eth: whether the VEA is 8021Q enabled or not
        :param vswitch_name: The name of the IBMPowerVM vswitch_name that VEA
                connects to. The default vswitch_name is ETHERNET0
        :param addl_vlan_ids:  VEA's 8021Q vlan tag list
        """
        vea = VirtualEthernetAdapterNoDB(name, vio_server, slot, pvid,
                                         is_trunk, trunk_pri, state,
                                         ieee_eth, vswitch_name,
                                         addl_vlan_ids)
        sqla_orm_session.make_transient(vea)
        return vea

    def create_sea(self, name, vio_server, slot, state, primary_vea,
                   control_channel=None, additional_veas=None):
        """
        Creates a DOM SharedEthernetAdapter object.

        :param name: Sea device name
        :param vios_server: Virtual I/O Server this SEA is owned by
        :param slot: SEA's virtual slot number
        :param state: the current state are Defined and Available
        :param primary_vea: the preferred VirtualEthernetAdapter
        :param control_channel: the VirtualEthernetAdapter used for management
        :param additional_veas: other VirtualEthernetAdapters on this SEA
        """
        sea = SharedEthernetAdapterNoDB(name, vio_server, slot, state,
                                        primary_vea, control_channel,
                                        additional_veas)
        sqla_orm_session.make_transient(sea)
        return sea

    def create_vios(self, lpar_name, lpar_id, host_name, state, rmc_state):
        """
        Creates a DOM VioServer object.

        :param lpar_name: The LPAR Name for the system as presented on the Host
                          itself.
        :param lpar_id: The LPAR identifier as presented on the Host itself.
        :param host_name: The host that this mapping applies to.  Should be a
                          string that identifies the host.
        :param state: State of the VIOS
        :param rmc_state: State of the RMC connection
        """
        vios = VioServerNoDB(lpar_name=lpar_name,
                             lpar_id=lpar_id,
                             host_name=host_name,
                             state=state,
                             rmc_state=rmc_state)
        sqla_orm_session.make_transient(vios)
        return vios


def shallow_copy_as_ordinary_list(iterable):
    """Return a simple list that is a shallow copy of the given iterable.

    NOTE: This is needed because "copy.copy(x)" returns an SqlAlchemy
    InstrumentedList when the given 'x' is such.  Then, operations on the
    instrumented copy (e.g., remove) cause SqlA-side-effects on the copied
    (managed) objects.
    """
    shallow_copy_list = []
    for x in iterable:
        shallow_copy_list.append(x)
    return shallow_copy_list


POWERVC_V1R2_TABLEMAPPED_CLASSES = [
    powervm_dto_models.HmcDTO,
    powervm_dto_models.HmcHostsDTO,
    powervm_dto_models.HmcHostClustersDTO,
    powervm_dto_models.InstancePowerSpecsDTO,
    VioServer,
    powervm_dto_models.VioServerDTO2,
    SharedEthernetAdapter,
    VirtualEthernetAdapter,
    NetworkAssociation,
    powervm_dto_models.StorageConnectivityGroupDTO,
    powervm_dto_models.FcPortDTO,
    powervm_dto_models.ScgViosAssociationDTO,
    powervm_dto_models.ComputeNodeHealthStatusDTO,
    powervm_dto_models.InstanceHealthStatusDTO,
    powervm_dto_models.HMCHealthStatusDTO,
    powervm_dto_models.OnboardTaskDTO,
    powervm_dto_models.OnboardTaskServerDTO
]
