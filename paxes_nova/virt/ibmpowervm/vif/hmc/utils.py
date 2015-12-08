# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# =================================================================
# =================================================================

"""
IBMPowerVM VIF driver utilities
"""

import logging

from powervc_nova.db.network import models as model
from powervc_nova.virt.ibmpowervm.vif.common import exception as excp
from powervc_nova.virt.ibmpowervm.vif.common import ras
from powervc_nova.virt.ibmpowervm.vif import hmc

from powervc_nova import _

LOG = logging.getLogger(__name__)


def get_k2element_for_sea(k2vios, sea):
    """
    This method returns the K2Element that corresponds to the SEA on the
    specific VIOS passed in.

    :param k2vios: K2Entry for the VIOS we're searching
    :param sea: SharedEthernetAdapter DOM object
    :returns K2Element: K2Element in the K2Response that corresponds to the SEA
                      passed in.  Returns None if not found.
    """
    if not k2vios or not sea:
        ras.trace(LOG, __name__, ras.TRACE_INFO,
                  _('No VIOS or SEA passed in for SEA Gather'))
        return None

    # Now get all the SharedEthernetAdapters
    seas = k2vios.element.findall('./SharedEthernetAdapters/'
                                  'SharedEthernetAdapter')

    # Be sure we found some SEAs in the K2Response
    if seas:
        # Loop through all trunk adapters found and grab adapter attributes
        for s in seas:
            # Device names match?
            if sea.name == s.findtext('DeviceName'):
                # Score!
                return s
            # TODO: Remove once everyone's at 1340A HMC
            if sea.name == s.findtext('InterfaceName'):
                return s

    # If we got here, didn't find it.
    return None


def get_k2entry_for_vios(k2response, vios):
    """
    This method returns the K2Entry in K2Response.entries that corresponds
    to the vios passed in.

    :param k2response: VirtualIOServer K2Response
    :param vios: VioServer DOM object
    :returns K2Entry: K2Entry in the K2Response that corresponds to the VIOS
                      passed in.  Returns None if not found.
    """
    # Be sure they passed in a vios
    if not vios:
        ras.trace(LOG, __name__, ras.TRACE_INFO, _('No VIOS passed in'))
        return None

    # Be sure the response they gave us has entries to loop over
    if not k2response or not k2response.feed or not k2response.feed.entries:
        ras.trace(LOG, __name__, ras.TRACE_INFO, _('Empty K2 response given'))
        return None

    # Loop through all VIOSes in the K2 response
    for entry in k2response.feed.entries:
        # Get the id and name of this VIOS in the response
        lpar_id = int(entry.element.findtext('PartitionID'))
        lpar_name = entry.element.findtext('PartitionName')

        # See if it matches what we're looking for
        if lpar_id == vios.lpar_id and lpar_name == vios.lpar_name:
            return entry

    # If we got here, we didn't find a match.  This really shouldn't
    # happen, but better to be paranoid...
    ras.trace(LOG, __name__, ras.TRACE_ERROR,
              _('Failed to find VIOS in K2 response'))
    return None


def get_k2entry_for_lpar_id(k2response, lpar_id):
    """
    This method returns the K2Entry in K2Response.entries that corresponds
    to the lpar id passed in.

    :param k2response: VirtualIOServer K2Response
    :param lpar_id: The lpar_id passed in
    :returns K2Entry: K2Entry in the K2Response that corresponds to the VIOS
                      passed in.  Returns None if not found.
    """
    return _get_k2entry_for_lpar_tag(k2response, 'PartitionID', lpar_id)


def get_k2entry_for_lpar_uuid(k2response, lpar_uuid):
    """
    This method returns the K2Entry in K2Response.entries that corresponds
    to the lpar UUID passed in.

    :param k2response: VirtualIOServer K2Response
    :param lpar_uuid: The lpar_uuid passed in
    :returns K2Entry: K2Entry in the K2Response that corresponds to the VIOS
                      passed in.  Returns None if not found.
    """
    return _get_k2entry_for_lpar_tag(k2response, 'PartitionUUID', lpar_uuid)


def _get_k2entry_for_lpar_tag(k2response, lpar_key, lpar_id):
    """
    This method returns the K2Entry in K2Response.entries that corresponds
    to the lpar id passed in.

    :param k2response: VirtualIOServer K2Response
    :param lpar_key: The key to search for within the lpar
    :param lpar_id: The lpar_id passed in
    :returns K2Entry: K2Entry in the K2Response that corresponds to the VIOS
                      passed in.  Returns None if not found.
    """
    # Be sure they passed in an id
    if not lpar_id:
        ras.trace(LOG, __name__, ras.TRACE_INFO, _('No lpar_id passed in'))
        return None

    # Be sure the response they gave us has entries to loop over
    if not k2response or not k2response.feed or not k2response.feed.entries:
        ras.trace(LOG, __name__, ras.TRACE_INFO, _('Empty K2 response given'))
        return None

    # Loop through all VIOSes in the K2 response
    for entry in k2response.feed.entries:
        # Get the id and name of this VIOS in the response
        k2_lpar_id = entry.element.findtext(lpar_key)

        # See if it matches what we're looking for
        if str(lpar_id) == k2_lpar_id:
            return entry

    # If we got here, we didn't find a match.  This really shouldn't
    # happen, but better to be paranoid...
    ras.trace(LOG, __name__, ras.TRACE_ERROR,
              _('Failed to find VIOS in K2 response'))
    return None


def build_veas_from_K2_vios_response(k2response, vios, vswitch_map, k2_oper,
                                     dom_factory=model.No_DB_DOM_Factory()):
    """
    This method takes in a VirtualIOServer K2Response and builds the
    TrunkAdapters into VirtualEthernetAdapter DOM objects, which are added to
    the passed in VioServer DOM object.

    :param k2response: VirtualIOServer K2Response
    :param vios: VioServer DOM object to add VEAs to
    :param vswitch_map: The map of vswitches on the CEC
    :param k2_oper: The k2 operator
    :param dom_factory: Factory used to create the DOM objects, optional.
    :returns veas: List of VirtualEthernetAdapter DOM objects built from
                   the passed in TrunkAdapters list
    """
    entry = get_k2entry_for_vios(k2response, vios)
    return _build_veas_from_K2_response(entry.element, vios, vswitch_map,
                                        k2_oper, dom_factory)


def build_veas_from_K2_sea_k2_entry(sea_k2entry, vios, vswitch_map, k2_oper,
                                    dom_factory=model.No_DB_DOM_Factory()):
    """
    This method takes in a SharedEthernetAdapter K2Entry and builds the
    TrunkAdapters into VirtualEthernetAdapter DOM objects, which are added to
    the passed in VioServer DOM object.

    :param sea_k2entry: SEA's K2 Entry
    :param vios: The VioServer object
    :param vswitch_map: The map of vswitches on the CEC
    :param k2_oper: The k2 operator
    :param dom_factory: Factory used to create the DOM objects, optional.
    :returns veas: List of VirtualEthernetAdapter DOM objects built from
                   the passed in TrunkAdapters list
    """
    return _build_veas_from_K2_response(sea_k2entry, vios,
                                        vswitch_map, k2_oper,
                                        dom_factory)


def _build_veas_from_K2_response(k2_entry, vios, vswitch_map, k2_oper,
                                 dom_factory=model.No_DB_DOM_Factory()):
    """
    This method takes in a VirtualIOServer K2Response and builds the
    TrunkAdapters into VirtualEthernetAdapter DOM objects, which are added to
    the passed in VioServer DOM object.

    :param k2_entry: K2 entry to searched for TrunkAdapters and
                     ClientNetworkAdapters
    :param vios: VioServer DOM object that owns the adapters in k2_entry
    :param vswitch_map: The map of vswitches on the CEC
    :param k2_oper: The k2 operator
    :param dom_factory: Factory used to create the DOM objects, optional.
    :returns veas: List of VirtualEthernetAdapter DOM objects built from
                   the passed in TrunkAdapters list
    """
    vea_list = []

    # If VIOS not found, return empty list
    if not k2_entry:
        ras.trace(LOG, __name__, ras.TRACE_ERROR,
                  ras.vif_get_msg('error', 'VIOS_UNKNOWN'))
        return vea_list

    # First, get all the VirtualEthernetAdapters (TrunkAdapters)
    net_adapters = k2_entry.findall('./TrunkAdapters/TrunkAdapter')
    if not net_adapters:
        net_adapters = []

    # A VIOS can have 'Client Network Adapters' - which are adapters with
    # a single VLAN that are not yet part of a SEA.  We need to take these
    # into account as they are also VEAs, just with a special situation.
    cnas = k2_entry.findall('./ClientNetworkAdapters/link')
    if cnas:
        uri = cnas[0].get('href').rsplit('/', 1)[0]
        if uri:
            # Get all CNAs in one K2 call
            k2_cna_resp = k2_oper.readbyhref(href=uri + '?group=None',
                                             timeout=hmc.K2_READ_SEC)

            # If we found some
            if k2_cna_resp and k2_cna_resp.feed and k2_cna_resp.feed.entries:
                # Add each to the adapters list
                for entry in k2_cna_resp.feed.entries:
                    net_adapters.append(entry.element)

    # Loop through all trunk adapters found and grab adapter attributes
    for adapter in net_adapters:
        # Device name
        if 'ClientNetworkAdapter' == adapter.tag:
            name = 'CNA_' + adapter.findtext('LocationCode')
        else:
            name = adapter.findtext('DeviceName')
            # TODO: Remove once everyone's at 1340A HMC
            if name is None:
                name = adapter.findtext('InterfaceName')

        if not name:
            ras.trace(LOG, __name__, ras.TRACE_WARNING, 'Missing name')

        # Slot number
        slot = adapter.findtext('VirtualSlotNumber')
        if slot:
            slot = int(slot)
        else:
            # Set to an invalid value so constructor blows up
            slot = 0
            ras.trace(LOG, __name__, ras.TRACE_WARNING, 'Missing slot')

        # Port VLAN ID
        pvid = adapter.findtext('PortVLANID')
        if pvid:
            pvid = int(pvid)
        else:
            # Set to an invalid value so constructor blows up
            pvid = 0
            ras.trace(LOG, __name__, ras.TRACE_WARNING, 'Missing pvid')

        # This is a TrunkAdapter..so by definition is_trunk is true.
        if 'ClientNetworkAdapter' == adapter.tag:
            is_trunk = False
            trunk_priority = 1
        else:
            # Trunk adapter case.
            is_trunk = True
            trunk_priority = adapter.findtext('TrunkPriority')
            if trunk_priority:
                trunk_priority = int(trunk_priority)
            else:
                # Set to an invalid value so constructor blows up
                trunk_priority = 0
                ras.trace(LOG, __name__, ras.TRACE_WARNING,
                          'Missing trunk_priority')

        # State (we'll have to map from K2 terminology to ours)
        state = adapter.findtext('VariedOn')
        if state and state == 'true':
            state = 'Available'
        else:
            if not state:
                ras.trace(LOG, __name__, ras.TRACE_WARNING, 'Missing state')
            state = 'Defined'

        # 8021Q enabled
        ieee_eth = adapter.findtext('TaggedVLANSupported')
        if ieee_eth and ieee_eth == 'true':
            ieee_eth = True
        else:
            if not ieee_eth:
                ras.trace(LOG, __name__, ras.TRACE_WARNING,
                          _('Missing ieee_eth'))
            ieee_eth = False

        # Addl vlans
        addl_vlan_ids = adapter.findtext('TaggedVLANIDs')
        if addl_vlan_ids:
            # VirtualEthernetAdapter requires list of ints
            addl_vlan_ids = map(int, addl_vlan_ids.split(' '))
        else:
            # VirtualEthernetAdapter requires empty list, not None
            addl_vlan_ids = []

        # vswitch name
        vswitch_id = adapter.findtext('VirtualSwitchID')
        vswitch_name = find_vswitch_name_for_id(vswitch_id, vswitch_map)
        if vswitch_name and vswitch_name != '':
            vswitch_name = vswitch_name
        else:
            # Use a default value
            ras.trace(LOG, __name__, ras.TRACE_DEBUG,
                      'Using default virtual switch name. '
                      'Found %s.' % vswitch_name)
            vswitch_name = 'ETHERNET0'

        # Build and add the DOM object to the return list.  This
        # should raise an exception if there is something wrong with
        # the configuration of this VEA.
        try:
            vea = dom_factory.create_vea(name=name,
                                         vio_server=vios,
                                         slot=slot,
                                         pvid=pvid,
                                         is_trunk=is_trunk,
                                         trunk_pri=trunk_priority,
                                         state=state,
                                         ieee_eth=ieee_eth,
                                         vswitch_name=vswitch_name,
                                         addl_vlan_ids=addl_vlan_ids)
            vea_list.append(vea)
        except Exception as e:
            # Took an exception creating the VEA.  Log the failing VEA info
            # and proceed
            ras.trace(LOG, __name__, ras.TRACE_WARNING,
                      _('Failed to create VEA DOM. (%s)' % e))
            ras.trace(LOG, __name__, ras.TRACE_WARNING,
                      _('Invalid VEA attributes - tag: %(tag)s, name: '
                        '%(name)s, slot: %(slot)d, pvid: %(pvid)d, is_trunk: '
                        '%(is_trunk)s, trunk_priority: %(trunk_priority)d, '
                        'state: %(state)s, ieee_eth: %(ieee_eth)s, '
                        'addl_vlan_ids: %(addl_vlan_ids)s' %
                        {'tag': adapter.tag,
                         'name': name,
                         'slot': slot,
                         'pvid': pvid,
                         'is_trunk': is_trunk,
                         'trunk_priority': trunk_priority,
                         'state': state,
                         'ieee_eth': ieee_eth,
                         'addl_vlan_ids': addl_vlan_ids}))

    # Return what we found (which could be nothing, but that's bad)
    ras.trace(LOG, __name__, ras.TRACE_DEBUG,
              'Found %d VEAs' % len(vea_list))
    return vea_list


def _find_seas_from_bridge(k2_vios_entry, k2_net_bridges):
    """
    Will query the K2 Bridges and find all of the associated SEAs on that
    Bridge list for this VIOS.  This is needed because K2 only stores the
    full data for the SEA's on the NetworkBridge, and no longer on the
    VirtualIOServer object itself.

    :param k2_vios_entry: The K2Entry representing the VIOS
    :param k2_net_bridges: The K2 NetworkBridge response
    :returns sea_list: A list of K2 Entry's for the SEAs on this VIOS
    :returns cc_map: A dictionary mapping ControlChannelInterfaceNames to
                     ControlChannelIDs.  The format of the dictionary is:
                       { '<ifname>,<vswitchid>': <controlchannelid> }
                       Or, with real data:
                       { 'ent6,0': 4094 }
    :returns pvid_map: A dictionary mapping SEA device names to their pvids.
                       It looks as follows:
                       {'ent11': 1, 'ent12': 2}
    """
    sea_list = []
    cc_map = {}
    pvid_map = {}
    vios_uuid = k2_vios_entry.element.findtext('PartitionUUID')

    for entry in k2_net_bridges.feed.entries:
        seas = entry.element.findall('./SharedEthernetAdapters/'
                                     'SharedEthernetAdapter')
        # Grab the NetworkBridge's pvid.  This will be the SEA's pvid since
        # legacy VIOS code allows the SEA to have a pvid that doesn't match
        # the primary adapter's pvid.  We need the primary adapters pvid, not
        # a value that the VIOS could allow a user to configure with something
        # completely different.
        nb_pvid = entry.element.findtext('PortVLANID')
        if nb_pvid is not None:
            nb_pvid = int(nb_pvid)
        for sea in seas:
            assigned_vio = sea.find('AssignedVirtualIOServer').get('href')
            # Found an SEA for this VIOS
            if assigned_vio.endswith(vios_uuid):
                # Save off the SEA K2Element
                sea_list.append(sea)

                # Create entry in the pvid map
                sea_name = sea.findtext('DeviceName')
                # TODO: Remove once everyone's at 1340A HMC
                if sea_name is None:
                    sea_name = sea.findtext('InterfaceName')
                if sea_name is not None:
                    pvid_map[sea_name] = nb_pvid

                # Save off Control Channel info, if this SEA has one.
                ctrl_ifname = sea.findtext('ControlChannelInterfaceName')
                if ctrl_ifname:
                    # Control Channel ID comes from the overall NetworkBridge
                    ctrl_id = entry.element.findtext('ControlChannelID')

                    # The control channel is also tied to a vswitch, so find
                    # the vswitch of this SEA by looking at its TrunkAdapters
                    vswitch_id = None
                    trunk_adapters = sea.findall('./TrunkAdapters/'
                                                 'TrunkAdapter')
                    if trunk_adapters:
                        vswitch_id = trunk_adapters[0].findtext('VirtualSwitch'
                                                                'ID')
                    # Should always find it, but check out of paranoia
                    if ctrl_id is not None:
                        # Make the key <ifname>,<vswitchid> so the user of
                        # this map can exactly match based on ifname and
                        # vswitchid.
                        cc_map[ctrl_ifname + ',' + vswitch_id] = int(ctrl_id)
                    else:
                        ras.trace(LOG, __name__, ras.TRACE_WARNING,
                                  'Found ControlChannelInterfaceName %s, '
                                  'but no ControlChannelID.' % ctrl_ifname)
    return sea_list, cc_map, pvid_map


def build_seas_from_K2_response(k2response, vios, k2_net_bridges, vswitch_map,
                                k2_oper,
                                dom_factory=model.No_DB_DOM_Factory()):
    """
    This method takes in a VirtualIOServer K2Response and builds the K2
    SharedEthernetAdapters into SharedEthernetAdapter DOM objects.  Each SEA's
    VEAs (trunk adapters in K2-speak) will also be built and all adapters (both
    SEAs and VEAs) will be added to the passed in VioServer object.

    :param k2response: VirtualIOServer K2Response
    :param vios: VioServer DOM object to add VEAs to
    :param k2_net_bridges: The NetworkBridge K2Response
    :param vswitch_map: The map of vswitches on the CEC
    :param k2_oper: The k2 operator
    :param dom_factory: Factory used to create the DOM objects, optional.
    :returns sea_list: List of SharedEthernetAdapter DOM objects
    """
    sea_list = []

    entry = get_k2entry_for_vios(k2response, vios)

    # If VIOS not found, return empty list
    if not entry:
        ras.trace(LOG, __name__, ras.TRACE_ERROR,
                  ras.vif_get_msg('error', 'VIOS_UNKNOWN'))
        return sea_list

    # Now get all the SharedEthernetAdapters
    seas, ctrl_chan_map, sea_pvid_map = _find_seas_from_bridge(entry,
                                                               k2_net_bridges)

    # Be sure we found some SEAs in the K2Response
    if seas:
        # Loop through all trunk adapters found and grab adapter attributes
        for sea in seas:
            # Device name
            name = sea.findtext('DeviceName')
            # TODO: Remove once everyone's at 1340A HMC
            if name is None:
                name = sea.findtext('InterfaceName')

            # If we got no interface name, we can't proceed.
            if not name:
                raise excp.IBMPowerVMInvalidHostConfig(attr='DeviceName')

            # Find all VEAs and add them to this SEA
            # First, we need the SEA's pvid so we can identify the
            # primary VEA
            sea_pvid = sea_pvid_map.get(name)

            primary_vea = None
            slot = 0
            control_channel = None
            additional_veas = []
            vswitch_name = None
            # Now get all VEAs under this SEA
            for vea in build_veas_from_K2_sea_k2_entry(sea, vios,
                                                       vswitch_map, k2_oper):
                # Grab the vswitch name.  We may need this for the control
                # channel lookup below.
                vswitch_name = vea.vswitch_name
                if vea.pvid == sea_pvid:
                    # Found the primary VEA
                    primary_vea = vea
                    slot = vea.slot
                else:
                    # Must just be an additional VEA
                    additional_veas.append(vea)

            # If this SEA has a control channel, find the right VEA for it.
            ctrl_chan_ifname = sea.findtext('ControlChannelInterfaceName')
            if ctrl_chan_ifname:
                # Look in the map for the <devicename,vswitchid> key that
                # matches this SEA's control channel info.
                find_key = ctrl_chan_ifname + ',' + \
                    find_vswitch_id_for_name(vswitch_name, vswitch_map)
                ctrl_chan_id = int(ctrl_chan_map.get(find_key, 0))

                # We know that topo_hmc._populate_adapters_into_vios()
                # already built the VIOS DOM object with ALL VEAs in it...
                # so spin through and find the CNAs to find our control
                # channel.
                for vea in vios.get_all_virtual_ethernet_adapters():
                    # If this is a CNA (non-trunk adapter) and the pvid
                    # matches the control channel id and it's on the same
                    # vswitch as this SEA
                    if not vea.is_trunk and vea.pvid == ctrl_chan_id and \
                            vea.vswitch_name == vswitch_name:
                        # We've got our match!
                        control_channel = vea
                        # Break out of the loop
                        break

                # If we get here, we expect to have found the control
                # channel.  Log if we don't.
                if control_channel is None:
                    ras.trace(LOG, __name__, ras.TRACE_WARNING,
                              _("Didn't find control channel for device "
                                "%(dev)s, vswitch %(vswitch)s, and id "
                                "%(c_id)d)" % {'dev': ctrl_chan_ifname,
                                               'vswitch': vswitch_name,
                                               'c_id': ctrl_chan_id}))

            # Build and add the DOM object to the return list.  This
            # should raise an exception if there is something wrong with
            # the configuration of this SEA.
            sea_list.append(
                dom_factory.create_sea(name=name,
                                       vio_server=vios,
                                       slot=slot,
                                       state='Available',
                                       primary_vea=primary_vea,
                                       control_channel=control_channel,
                                       additional_veas=additional_veas))

            ras.trace(LOG, __name__, ras.TRACE_DEBUG,
                      'Built SEA %s on VIOS %s' % (name, vios.name))

    # Return what we found (which could be nothing, but that's bad)
    ras.trace(LOG, __name__, ras.TRACE_DEBUG,
              'Found %d SEAs' % len(sea_list))
    return sea_list


def find_vswitch_name_for_id(k2_id, vswitch_map):
    """
    Returns the vSwitch name for a given vSwitch K2 ID

    :param k2_id: The K2 vSwitch ID
    :param vswitch_map: The vSwitch map from the HMC Topo
    :returns: vSwitch Name
    """
    k2_obj = vswitch_map.get(k2_id)
    if not k2_obj:
        return None
    return k2_obj.element.findtext('SwitchName')


def find_vswitch_map(oper, mgd_sys_uuid):
    """
    Returns the mapping of vswitch names to IDs

    :param oper: The operator to interact with the HMC.
    :param mgd_sys_uuid: The unique identifier of the managed system.
    :returns: vswitch_map
    """
    if not oper:
        return None

    vswitch_map = {}
    k2_vswitches = oper.read(rootType='ManagedSystem',
                             rootId=mgd_sys_uuid,
                             childType='VirtualSwitch',
                             timeout=hmc.K2_READ_SEC,
                             xag=[None])
    for k2_vswitch in k2_vswitches.feed.entries:
        vswitch_id = k2_vswitch.element.findtext('SwitchID')
        vswitch_map[vswitch_id] = k2_vswitch
    return vswitch_map


def _find_managed_system(oper, mtms):
    """
    Finds the system uuid for this specific server that the HMC is
    managing.

    :param oper: The HMC operator for interacting with the HMC.
    :param mtms: The machine type/model/serial for the host on the
                 HMC to look up.
    :return: The UUID of the K2 managed system
    :return: The K2 object for the managed system
    """

    machine, model, serial = parse_mtm(mtms)
    k2resp = oper.read(rootType='ManagedSystem',
                       suffixType='search',
                       suffixParm='(MachineType==%s&&Model==%s&&'
                       'SerialNumber==%s)' % (machine, model, serial),
                       timeout=hmc.K2_MNG_SYS_READ_SEC,
                       xag=[None])
    xpath = './MachineTypeModelAndSerialNumber/SerialNumber'
    entries = k2resp.feed.findentries(xpath, serial)
    if entries is None:
        return None

    # Confirm same model and type
    machine_xpath = './MachineTypeModelAndSerialNumber/MachineType'
    model_xpath = './MachineTypeModelAndSerialNumber/Model'
    for entry in entries:
        entry_machine = entry.element.findtext(machine_xpath)
        entry_model = entry.element.findtext(model_xpath)
        if entry_machine == machine and entry_model == model:
            managed_system_uuid = entry.properties['id']
            k2_managed_system = entry
            return managed_system_uuid, k2_managed_system


def find_vswitch_id_for_name(name, vswitch_map):
    """
    Returns the vSwitch K2 id for a given vSwitch Name

    :param name: The vSwitch Name
    :param vswitch_map: The vSwitch map from the HMC Topo
    :returns: vSwitch K2 ID
    """
    for item in vswitch_map.iteritems():
        vswitch_id = item[0]
        vswitch_k2_obj = item[1]
        vswitch_k2_name = vswitch_k2_obj.element.findtext('SwitchName')

        if vswitch_k2_name == name:
            return vswitch_id
    return None


def find_vswitch_uri_for_id(k2_id, vswitch_map):
    """
    Returns the vSwitch href URI for a given K2 id.
    :param k2_id: The K2 vSwitch ID
    :param vswitch_map: The vSwitch map from the HMC Topo
    :returns: vSwitch URI
    """
    k2_obj = vswitch_map.get(k2_id)
    if not k2_obj:
        return None
    return k2_obj.properties['link']


def parse_mtm(mtm_serial):
    """
    Parses a mtm_serial string to a result of machine, model, serial
    """
    mtm, serial = mtm_serial.split('_', 1)
    model = mtm[4:7]
    machine = mtm[0:4]
    return machine, model, serial
