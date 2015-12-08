# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# =================================================================
# =================================================================

"""
    IBM PowerVM VLAN VIF driver - HMC implementation.

    IBMPowerVMVlanVIFDriver is a IBM PowerVM based VIF driver that manages the
    VLAN bridging capability provided by PowerVM. The current implementation
    is based on IVM and should be used with Neutron ML2 plugin.

    This VIF driver requires the VIOS to have at least one Shared Ethernet
    Adapter(SEA) pre-configured with management vlan as the untagged port
    vlan on the pvid adapter without any additional 8021Q VLAN ids on it.
    Any new VLAN id that adds to the SEA will be plugged into a trunk
    virtual ethernet adapter's addl_vlan_id list as 8021Q VLAN.

    In order to use this VIF driver, this class (IBMPowerVMVlanVIFDriver) needs
    to be instantiated in the PowerVMOperator's __init__ method. Then
    IBMPowerVMVlanVIFDriver's plug or unplug method will bridge or unbridge
    (respectively) a given VLAN used by a VM.
"""

import logging

from powervc_k2 import k2operator as k2
from powervc_nova.virt.ibmpowervm.vif import driver
from powervc_nova.virt.ibmpowervm.vif.common import exception
from powervc_nova.virt.ibmpowervm.vif.common import ras
from powervc_nova.virt.ibmpowervm.vif.hmc import topo_hmc
from powervc_nova.virt.ibmpowervm.vif.hmc import utils
from powervc_nova.virt.ibmpowervm.vif import hmc

from oslo.config import cfg

LOG = logging.getLogger(__name__)

CONF = cfg.CONF
CONF.import_opt('ibmpowervm_comm_timeout', 'powervc_nova.virt.ibmpowervm.hmc')
CONF.import_opt('powervm_mgr', 'powervc_nova.virt.ibmpowervm.ivm')
CONF.import_opt('powervm_mgr_user', 'powervc_nova.virt.ibmpowervm.ivm')
CONF.import_opt('powervm_mgr_passwd', 'powervc_nova.virt.ibmpowervm.ivm')


class IBMPowerVMVlanVIFDriverHMC(driver.IBMPowerVMBaseVIFDriver):
    """
    IBM PowerVM Vlan VIF driver, HMC implementation
    """

    def __init__(self, host_name, operator=None, mtms=None):
        """
        IBMPowerVMVlanVIFDriver's constructor.

        :param host_name: The name of the host machine for nova, not to be
                          confused with the networking concept of hostname.
        :param operator: K2Operator to use for K2 calls
        :param mtms: The MTMS for the managed system
        """
        # If we didn't get an operator passed in
        if not operator:
            # This should only happen when a VLAN is being moved from one
            # SEA to another.  It is NOT invoked via the host.py.  However,
            # we do know that the host.py was invoked before us, so we can
            # use that operator.
            self._k2op = hmc.K2_OPER
        else:
            # Use the one passed in
            self._k2op = operator

        if mtms:
            self.mtms = mtms
        else:
            self.mtms = CONF.host

        # Centralized topology service for IVM
        self._topo = topo_hmc.IBMPowerVMNetworkTopoHMC(host_name=host_name,
                                                       operator=self._k2op,
                                                       mtms=self.mtms)

        # Centralized cached data
        self._supports_dynamic_vlans = None

        # Call parent constructor
        super(IBMPowerVMVlanVIFDriverHMC, self).__init__()

    def _find_k2_bridge(self, sea):
        """
        Queries K2 to find the appropriate NetworkBridge that manages the
        SEA.

        :param sea: The DOM object representing the Shared Ethernet Adapter.
        :returns: The K2 Entry that represents the SEA.  If one can not be
                  found, None will be returned.
        """
        seas_vio_href = self._topo.vio_server_map[sea.vio_server.lpar_id]
        seas_vio_uuid = seas_vio_href.split('/').pop()

        k2_net_bridges = self._k2op.read(
            rootType='ManagedSystem',
            rootId=self._topo._find_managed_system(),
            childType='NetworkBridge',
            timeout=hmc.K2_RMC_READ_SEC,
            xag=[None])
        for bridge in k2_net_bridges.feed.entries:
            # Check the SEA's backing it to see if their names match the one
            # sent in.
            search = './SharedEthernetAdapters/SharedEthernetAdapter'
            k2_br_seas = bridge.element.findall(search)
            for k2_sea in k2_br_seas:
                dev_name = k2_sea.findtext('DeviceName')
                # TODO: Remove once everyone's at 1340A HMC
                if dev_name is None:
                    dev_name = k2_sea.findtext('InterfaceName')
                vio_href = k2_sea.find('AssignedVirtualIOServer').get('href')
                vio_uuid = vio_href.split('/').pop()
                if seas_vio_uuid == vio_uuid and dev_name == sea.name:
                    m_sys = self._topo._find_managed_system()
                    # Note the read here is done with age 0.  We need an
                    # absolutely current etag.
                    return self._k2op.read(rootType='ManagedSystem',
                                           rootId=m_sys,
                                           childType='NetworkBridge',
                                           childId=bridge.properties['id'],
                                           timeout=hmc.K2_RMC_READ_SEC,
                                           xag=[None])

        return None

    def _update_vnet_tag(self, k2_virt_net, new_tag):
        """
        Will validate the the tagged network entry is set properly and is
        updated in the network.  If the tagged entry is set properly, no
        changes are made.

        :param k2_virt_net: The K2 Entry for the Virtual Network
        :param new_tag: The Boolean that states whether or not the network
                        should be tagged or not.
        """
        k2_tag_element = k2_virt_net.element.find('TaggedNetwork')
        old_tag = k2_tag_element.gettext()

        if old_tag.lower() != str(new_tag).lower():
            k2_tag_element.settext(str(new_tag).lower())

            # The TaggedNetwork Attribute can not be 'updated' regardless
            # of what the schema says.  K2 will throw an error.  They
            # will only support this flow by first deleting the virtual
            # network and then creating a new one.
            try:
                self._k2op.delete(rootType='ManagedSystem',
                                  rootId=self._topo._find_managed_system(),
                                  childType='VirtualNetwork',
                                  childId=k2_virt_net.properties['id'],
                                  timeout=hmc.K2_WRITE_SEC)
            except Exception as e:
                # This should be very rare and only occur in the case where
                # there were existing client lpars with the vlan we're
                # plugging.  If we disallowed those vlans, we'd break VM
                # import, so instead just tolerate this.
                ras.trace(LOG, __name__, ras.TRACE_WARNING,
                          'Unable to flip TaggedNetwork for VirtualNetwork '
                          '%(net)s due to exception: %(excpt)s' %
                          {'net': k2_virt_net.properties['id'],
                           'excpt': e})
                return

            # Do the update of the Virtual Network
            self._k2op.create(element=k2_virt_net.element,
                              rootType='ManagedSystem',
                              rootId=self._topo._find_managed_system(),
                              childType='VirtualNetwork',
                              timeout=hmc.K2_WRITE_SEC)

    def _find_or_build_k2_vnet(self, k2_virt_nets, vlan_id, vswitch_id,
                               tagged, force_tag_update=False):
        """
        This method will find, and if not found - build, the appropriate K2
        VirtualNetwork object for the provided input.

        :param k2_virt_nets: The K2 entity that has the feed of all the
                             Virtual Networks in the system.
        :param vlan_id: The VLAN of the network.
        :param vswitch_id: The virtual switch within the system.  This is the
                           K2 ID (usually a number) for the vSwitch.
        :param tagged: Returns whether or not the network should be tagged.
        :param force_tag_update: If set to true, will force a tag network
                                 update
        """
        for k2_virt_net in k2_virt_nets.feed.entries:
            k2_elem = k2_virt_net.element
            k2_vlan = k2_elem.findtext('NetworkVLANID')
            k2_vswitchid = k2_elem.findtext('VswitchID')

            if k2_vlan == str(vlan_id) and k2_vswitchid == vswitch_id:
                # We found the appropriate virtual network, however we need
                # to check that the tag is correct.  If not, we need to update
                # the tag on the network.
                if force_tag_update:
                    self._update_vnet_tag(k2_virt_net, tagged)

                # Simply return the update vnet
                return k2_virt_net

        # Didn't find it...try to build one
        attrs = []
        vswitch_uri = utils.find_vswitch_uri_for_id(vswitch_id,
                                                    self._topo.vswitch_map)
        attrs.append(k2.K2Element('AssociatedSwitch',
                                  attrib={'href': vswitch_uri,
                                          'rel': 'related'}))

        vswitch_name = utils.find_vswitch_name_for_id(vswitch_id,
                                                      self._topo.vswitch_map)
        # Network name has some constraints...
        net_name = '%(vlan)d-%(vswitch)s' % {'vlan': vlan_id,
                                             'vswitch': vswitch_name}
        net_name = net_name.replace('(Default)', '')

        attrs.append(k2.K2Element('NetworkName', text=net_name))
        attrs.append(k2.K2Element('NetworkVLANID', text=str(vlan_id)))
        attrs.append(k2.K2Element('TaggedNetwork', text=str(tagged).lower()))

        # Create the element...
        new_vnet = k2.K2Element('VirtualNetwork',
                                attrib={'schemaVersion': 'V1_0'},
                                children=attrs)
        ras.trace(LOG, __name__, ras.TRACE_DEBUG,
                  'Creating vnet %s' % net_name)
        self._k2op.create(element=new_vnet,
                          rootType='ManagedSystem',
                          rootId=self._topo._find_managed_system(),
                          childType='VirtualNetwork',
                          timeout=hmc.K2_WRITE_SEC)
        ras.trace(LOG, __name__, ras.TRACE_DEBUG,
                  'Created vnet %s' % net_name)

        # Recurse to find it now
        ras.trace(LOG, __name__, ras.TRACE_DEBUG,
                  'Getting ManagedSystem/VirtualNetwork')
        k2_virt_nets = self._k2op.read(
            rootType='ManagedSystem',
            rootId=self._topo._find_managed_system(),
            childType='VirtualNetwork',
            timeout=hmc.K2_READ_SEC,
            xag=[None])
        ras.trace(LOG, __name__, ras.TRACE_DEBUG,
                  'Got ManagedSystem/VirtualNetwork')
        return self._find_or_build_k2_vnet(k2_virt_nets, vlan_id, vswitch_id,
                                           tagged)

    def _find_vswitch_id(self, sea):
        """
        Returns the K2 VSwitch ID for the given Shared Ethernet Adapter.

        :param sea: DOM object that represents the SEA
        :returns: The K2 VSwitch ID that represents the appropriate vSwitch.
        """
        vswitch_name = sea.get_primary_vea().vswitch_name
        return utils.find_vswitch_id_for_name(vswitch_name,
                                              self._topo.vswitch_map)

    def _find_trunk_adapter(self, pvid, lpar_id, vswitch_id, k2_bridge_entry):
        """
        Finds the appropriate TrunkAdapter (represents a VEA) for the provided
        input.

        :param pvid: The primary VLAN id of the Trunk Adapter
        :param lpar_id: The LPAR identiier that the Trunk Adapter is on.
        :param vswitch_id: The K2 vSwitch identifier for the Trunk Adapter.
        :param k2_bridge_entry: The K2 NetworkBridge that contains all of the
                                trunk adapters to search through.
        :returns: The K2 Trunk Adapter that matches the input.  If one can
                  not be found, None is returned.
        """
        search = './LoadGroups/LoadGroup'
        k2_ld_grps = k2_bridge_entry.element.findall(search)
        for k2_ld_grp in k2_ld_grps:
            # Make sure the PVID matches
            port_vid = k2_ld_grp.findtext('PortVLANID')
            if str(pvid) != port_vid:
                continue

            # Need to go into the trunk adapters now...
            k2_tr_adpts = k2_ld_grp.findall('./TrunkAdapters/TrunkAdapter')
            for k2_tr_adpt in k2_tr_adpts:
                # Make sure the switch ids match...if not break completely
                k2_vswitch_id = k2_tr_adpt.findtext('VirtualSwitchID')
                if k2_vswitch_id != vswitch_id:
                    break

                # Next, check the lpar id
                lpar_phys_loc = 'V%(lpid)s-' % {'lpid': lpar_id}
                trunk_phys_loc = k2_tr_adpt.findtext('LocationCode')
                if trunk_phys_loc.find(lpar_phys_loc) >= 0:
                    return k2_tr_adpt

        return None

    def _find_load_grp(self, pvid, lpar_id, vswitch_id, k2_bridge_entry):
        """
        Finds the appropriate LoadGroup for the input.

        :param pvid: The primary VLAN id of the Load Group
        :param lpar_id: The LPAR identiier that the Load Group is on.
        :param vswitch_id: The K2 vSwitch identifier for the Load Group.
        :param k2_bridge_entry: The K2 NetworkBridge that contains all of the
                                Load Groups to search through.
        :returns: The K2 Load Group that matches the input.  If one can
                  not be found, None is returned.
        """
        search = './LoadGroups/LoadGroup'
        k2_ld_grps = k2_bridge_entry.element.findall(search)
        for k2_ld_grp in k2_ld_grps:
            # Make sure the PVID matches
            port_vid = k2_ld_grp.findtext('PortVLANID')
            if str(pvid) != port_vid:
                continue

            # Need to go into the trunk adapters now...
            k2_tr_adpts = k2_ld_grp.findall('./TrunkAdapters/TrunkAdapter')
            for k2_tr_adpt in k2_tr_adpts:
                # Make sure the switch ids match...if not break completely
                k2_vswitch_id = k2_tr_adpt.findtext('VirtualSwitchID')
                if k2_vswitch_id != vswitch_id:
                    break

                # Next, check the lpar id
                lpar_phys_loc = 'V%(lpid)s-' % {'lpid': lpar_id}
                trunk_phys_loc = k2_tr_adpt.findtext('LocationCode')
                if trunk_phys_loc.find(lpar_phys_loc) >= 0:
                    return k2_ld_grp

        return None

    def _update_vnets_from_ld_grps(self, k2_net_bridge):
        """
        Will update the K2 Network Bridge Entries 'Virtual Networks' attribute
        to match that of the LoadGroups contained within it.  This is required
        because the Network Bridge requires consistency between the two but
        PowerVC is managing it at the Load Group level.

        :param k2_net_bridge: The entity to perform the consistency check
                              against.
        """
        search = './LoadGroups/LoadGroup/VirtualNetworks/link'
        required_vnets = k2_net_bridge.element.findall(search)
        k2_bridge_elem = k2_net_bridge.element
        removes = k2_bridge_elem.find('VirtualNetworks')
        if removes:
            k2_bridge_elem.remove(removes)
        k2_bridge_elem.append(k2.K2Element('VirtualNetworks',
                                           children=required_vnets))

    def _create_vea_on_vios(self, vios, sea, slotnum, port_vlan_id,
                            addl_vlan_ids):
        """
        This method will create the 802.1Q VirtualEthernetAdapter on the
        VIOS and return the device name of the newly created adapter.  A
        IBMPowerVMFailToAddDataVlan should be raised if unable to create the
        new VEA.

        :param vios: VioServer DOM object representing VIOS this is being
                     created on.
        :param sea: SharedEthernetAdapter DOM object that owns the new VEA
        :param slotnum: Virtual slot number to create the new VEA in.
        :param port_vlan_id: pvid to set on the new VEA
        :param addl_vlan_ids: Additional vlan ids to set on the new VEA
        :returns vea_devname: Device name of the newly created VEA
        :returns slot_number: Slot number the VEA was created in.
        """
        ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG, "Enter")

        k2_virt_nets = self._k2op.read(
            rootType='ManagedSystem',
            rootId=self._topo._find_managed_system(),
            childType='VirtualNetwork',
            timeout=hmc.K2_READ_SEC,
            xag=[None])
        k2_vswitch_id = self._find_vswitch_id(sea)

        # Build all of the desired K2 Virtual Networks
        desired_k2_vnets = []
        pvid_k2_net = self._find_or_build_k2_vnet(k2_virt_nets, port_vlan_id,
                                                  k2_vswitch_id, False, False)
        desired_k2_vnets.append(pvid_k2_net.properties.get('link'))

        for addl_vlan_id in addl_vlan_ids:
            net = self._find_or_build_k2_vnet(k2_virt_nets, addl_vlan_id,
                                              k2_vswitch_id, True, True)
            desired_k2_vnets.append(net.properties.get('link'))

        k2_vnet_elems = []
        for desired_k2_vnet in desired_k2_vnets:
            attributes = {'href': desired_k2_vnet, 'rel': 'related'}
            k2_vnet_elems.append(k2.K2Element('link', attrib=attributes))

        # At this point, we have all the VirtualNetworks we want added.
        # Need to make a new LoadGroup to represent it.
        attrs = []
        attrs.append(k2.K2Element('PortVLANID', text=str(port_vlan_id)))
        attrs.append(k2.K2Element('VirtualNetworks',
                                  children=k2_vnet_elems))

        new_load_grp = k2.K2Element('LoadGroup',
                                    attrib={'schemaVersion': 'V1_0'},
                                    children=attrs)

        k2resp = self._find_k2_bridge(sea)
        k2_bridge = k2resp.entry
        k2_bridge.element.find('LoadGroups').append(new_load_grp)

        self._update_vnets_from_ld_grps(k2_bridge)

        k2resp = self._k2op.update(data=k2_bridge.element,
                                   etag=k2resp.headers['etag'],
                                   rootType='ManagedSystem',
                                   rootId=self._topo._find_managed_system(),
                                   childType='NetworkBridge',
                                   childId=k2_bridge.properties['id'],
                                   timeout=hmc.K2_RMC_WRITE_SEC,
                                   xag=[None])

        # Be sure to refresh the cached data.
        self._invalidate_k2_cache()

        # Workaround for K2 bug (SW225510)!
        # TODO: Remove when fixed.
        k2resp = self._k2op.read(rootType='ManagedSystem',
                                 rootId=self._topo._find_managed_system(),
                                 childType='NetworkBridge',
                                 childId=k2_bridge.properties['id'],
                                 timeout=hmc.K2_RMC_READ_SEC,
                                 xag=[None])

        trunk_adpt = self._find_trunk_adapter(port_vlan_id,
                                              sea.vio_server.lpar_id,
                                              k2_vswitch_id, k2resp.entry)

        # TODO if trunk_adpt is None, throw an error
        # Get the appropriate data to pass back
        dev_name = trunk_adpt.findtext('DeviceName')
        # TODO: Remove once everyone's at 1340A HMC
        if dev_name is None:
            dev_name = trunk_adpt.findtext('InterfaceName')
        slot_num = int(trunk_adpt.findtext('VirtualSlotNumber'))
        return dev_name, slot_num

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
        ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG, "Enter")

        k2_virt_nets = self._k2op.read(
            rootType='ManagedSystem',
            rootId=self._topo._find_managed_system(),
            childType='VirtualNetwork',
            timeout=hmc.K2_READ_SEC,
            xag=[None])
        k2_vswitch_id = self._find_vswitch_id(sea)

        # Build all of the desired K2 Virtual Networks
        desired_k2_vnets = []
        pvid_k2_net = self._find_or_build_k2_vnet(k2_virt_nets,
                                                  vea.pvid,
                                                  k2_vswitch_id,
                                                  False, False)
        desired_k2_vnets.append(pvid_k2_net.properties.get('link'))

        for addl_vlan_id in vea.addl_vlan_ids:
            net = self._find_or_build_k2_vnet(k2_virt_nets, addl_vlan_id,
                                              k2_vswitch_id, True, False)
            desired_k2_vnets.append(net.properties.get('link'))

        k2_vnet_elems = []
        for desired_k2_vnet in desired_k2_vnets:
            attributes = {'href': desired_k2_vnet, 'rel': 'related'}
            k2_vnet_elems.append(k2.K2Element('link', attrib=attributes))

        k2resp = self._find_k2_bridge(sea)
        k2_bridge = k2resp.entry
        k2_load_grp = self._find_load_grp(vea.pvid, sea.vio_server.lpar_id,
                                          k2_vswitch_id, k2_bridge)

        # If the load group is None here, we find that this happens when an
        # SEA is set up for failover, but the failover SEA isn't sync'd
        # properly.
        if k2_load_grp is None:
            raise exception.IBMPowerVMIncorrectLoadGroup()

        # Update the K2 Load Group with your new Virtual Networks children
        replaced_grps = k2_load_grp.find('VirtualNetworks')
        if replaced_grps:
            k2_load_grp.remove(replaced_grps)
        k2_load_grp.append(k2.K2Element('VirtualNetworks',
                                        children=k2_vnet_elems))

        self._update_vnets_from_ld_grps(k2_bridge)

        k2resp = self._k2op.update(data=k2_bridge.element,
                                   etag=k2resp.headers['etag'],
                                   rootType='ManagedSystem',
                                   rootId=self._topo._find_managed_system(),
                                   childType='NetworkBridge',
                                   childId=k2_bridge.properties['id'],
                                   timeout=hmc.K2_RMC_WRITE_SEC,
                                   xag=[None])

        # Be sure to refresh the cached data.
        self._invalidate_k2_cache()

    def _delete_vea_on_vios(self, vios, vea, sea, virt_adpts_list):
        """
        This method will remove the given VEA from the given SEA on the
        system and delete it.

        :param vios: VioServer DOM object representing VIOS this is being
                     removed from.
        :param vea: VirtualEthernetAdapter DOM object
        :param sea: SharedEthernetAdapter DOM object
        :param virt_adpts_list: List of virtual adapters that should remain
                                on the SEA.  Ignored for HMC
        """
        ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG, "Enter")

        k2resp = self._find_k2_bridge(sea)
        k2_bridge = k2resp.entry
        k2_vswitch_id = self._find_vswitch_id(sea)
        k2_load_grp = self._find_load_grp(vea.pvid, sea.vio_server.lpar_id,
                                          k2_vswitch_id, k2_bridge)
        if k2_load_grp:
            k2_bridge.element.find('LoadGroups').remove(k2_load_grp)

        self._update_vnets_from_ld_grps(k2_bridge)

        ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG, 'Setting VIOS')
        self._k2op.update(data=k2_bridge.element,
                          etag=k2resp.headers['etag'],
                          rootType='ManagedSystem',
                          rootId=self._topo._find_managed_system(),
                          childType='NetworkBridge',
                          childId=k2_bridge.properties['id'],
                          timeout=hmc.K2_RMC_WRITE_SEC,
                          xag=[None])
        ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG, 'Set VIOS')

        # Be sure to refresh the cached data.
        self._invalidate_k2_cache()

    def _find_veas(self, vea_dom, k2vios, k2sea):
        """
        Will return a pair of data.  The first is the k2_vios_vea which
        represents the k2element for the VEA on the k2_vios TrunkAdapters
        list.  The second is the k2_sea_vea, which represents the k2element
        for the VEA on the k2sea TrunkAdapters list.

        :param vea_dom: The dom level element to search against.
        :param k2vios: The k2element representing the VIOS
        :param k2sea: The k2element representing the owning SEA
        """
        pvid = vea_dom.pvid
        vswitch_map = self._topo.vswitch_map
        vswitch_id = utils.find_vswitch_id_for_name(vea_dom.vswitch_name,
                                                    vswitch_map)

        # Find the VEA we're trying to delete
        k2vea_on_sea = None
        trunk_list = k2sea.findall('./TrunkAdapters/TrunkAdapter')
        for adapter in trunk_list:
            if pvid == int(adapter.findtext('PortVLANID')) and\
                    vswitch_id == adapter.findtext('VirtualSwitchID'):
                k2vea_on_sea = adapter
                break

        k2vea_on_vios = None
        for adapter in k2vios.element.findall('./TrunkAdapters/TrunkAdapter'):
            if pvid == int(adapter.findtext('PortVLANID')) and\
                    vswitch_id == adapter.findtext('VirtualSwitchID'):
                k2vea_on_vios = adapter
                break
        return k2vea_on_vios, k2vea_on_sea

    def _find_first_avail_slot(self, vios):
        """
        This is not needed on HMC because the HMC will pick a slot for us.
        Furthermore, if there are no available slots, the _create_vea_on_vios()
        will raise an exception, so we don't really need this method to do
        anything. Just return a valid value so the invoker proceeds.
        """
        ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG, "Enter")

        return 10

    def _find_vlan_users(self, vios, vlanid, vswitch=None):
        """
        This method will search for the given vlanid on the system and return
        two things.  First, the pvid of the SEA on the VIOS containing the
        vlanid.  Second, the number of users of that vlanid total (ie, client
        LPARs in addition to VIOS).

        :param vios: VIOS dom object we're operating on
        :param vlanid: VLANID to search for
        :param vswitch: vswitch name to associate with this vlanid
        :returns pvid: port vlanid of the SEA on the VIOS containing the
                       vlanid.  Will be 0 if not found.
        :returns num_users: Total number of adapters found with this vlanid.
        """
        ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG, "Enter")

        pvid = 0
        num_users = 0

        # Find the VIOS's VEA (if any) that has this vlanid configured on it.
        # Note that this is coming from the database, not the live system.
        # We assume the database is up to date and if we had a race condition
        # where two processes were unplugging the same vlanid (and this is
        # protected by a lock, so only one runs at a time) and the first guy
        # totally removed the vlanid from the system, the database would
        # have been updated and the second unplug will see no evidence of that
        # vlanid on the VIOS reflected in the database at this point.
        for vea in vios.get_all_virtual_ethernet_adapters():
            if vea.is_vlan_configured(vlanid, vswitch):
                # Found a user of the vlanid
                num_users += 1
                # Remember pvid of adapter containing vlanid
                pvid = vea.pvid
                break

        # Now, get all client lpar adapters to see if any of them have the
        # vlanid configured.  This will be an actual K2 call to the HMC...
        # which means if there are a lot of LPARs, this will be SLOOOOWWW....
        # Apparently this will be coming from the database at some point, so
        # we got that going for us.  Note, we don't cache the LogicalPartition
        # K2Response because client LPARs come and go.
        ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG,
                                'Getting LPARs')
        k2response = self._k2op.read(
            rootType='ManagedSystem',
            rootId=self._topo._find_managed_system(),
            childType='LogicalPartition',
            timeout=hmc.K2_READ_SEC,
            xag=[None])
        ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG, 'Got LPARs')

        # If we found no client LPARs, just return
        if not k2response.feed or not k2response.feed.entries:
            ras.trace(LOG, __name__, ras.TRACE_INFO,
                      ras.msg('info', 'NO_CLIENT_LPAR') % self._topo.host_name)
            return pvid, num_users

        # For each LPAR
        for lpar in k2response.feed.entries:
            # Build a link to all the ClientNetworkAdapters.
            cna_links = lpar.element.findall('./ClientNetworkAdapters/link')

            # If there were some ClientNetworkAdapters
            if cna_links:
                # Get the link for the next K2 call.  Just use the first
                # CNA link found because we're going to strip the UUID to get
                # all adapters in one K2 call anyways.
                href = cna_links[0].getattrib()['href']

                # Strip the UUID
                href = href[0:href.rfind('/')] + '?group=None'

                # Get the adapters
                ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG,
                                        'Getting client adapters')
                k2_adapter_response = self._k2op.readbyhref(
                    href=href,
                    timeout=hmc.K2_READ_SEC)
                ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG,
                                        'Got client adapters')

                # Be sure we found some client adapters
                if not k2_adapter_response.feed or \
                        not k2_adapter_response.feed.entries:
                    ras.trace(LOG, __name__, ras.TRACE_INFO,
                              ras.msg('info', 'NO_CLIENT_ADAPTERS') %
                              {'lpar': lpar.element.findtext('PartitionName'),
                               'lparid': lpar.element.findtext('PartitionID')})
                    # Move on to the next client lpar
                    continue

                # Loop through all adapters and look for our vlanid
                for adapter in k2_adapter_response.feed.entries:
                    # Get the client adapter's PVID
                    client_pvid = adapter.element.findtext('PortVLANID')
                    # If we found a pvid and it matches our vlanid
                    if client_pvid and int(client_pvid) == vlanid:
                        # Bump the users count
                        num_users += 1

                    # I don't think we have additional vlans to worry about,
                    # but just in case...
                    addl_vlans = adapter.element.findtext('TaggedVLANIDs')
                    # If we found some additional vlans
                    if addl_vlans:
                        # Make a string list
                        addl_vlans = addl_vlans.split(',')
                        # Now search for our vlanid.  Our vlanid is an int,
                        # so we use list comprehension to convert the list of
                        # strings to a list of ints
                        if vlanid in [str(x) for x in addl_vlans]:
                            num_users += 1

        return pvid, num_users

    def _supports_dynamic_update(self, vios=None):
        """
        This method returns whether the VIOS supports dynamically updating VLAN
        ids on VEAs or not.  On HMC, this depends on the system's capabilities.

        :param vios: VioServer DOM object representing VIOS running against
        :returns boolean: True if dynamic updates supported, False otherwise
        """
        ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG, "Enter")

        if self._supports_dynamic_vlans is None:
            # Older firmware cannot dynamically update a VEA with vlans (boo).
            # We cache the ManagedSystem K2Response because that shouldn't
            # ever change.
            ras.trace(LOG, __name__, ras.TRACE_DEBUG,
                      ras.msg('info', 'K2_GET_MS'))

            # Make the K2 call to get the ManagedSystem response
            ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG,
                                    'Getting ManagedSystem')
            k2entry_ms = self._topo._find_k2_managed_system()
            ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG,
                                    'Got ManagedSystem')

            # Now check to see if the system supports dynamic vlan additions
            caps = k2entry_ms.element.find('AssociatedSystem'
                                           'Capabilities')
            dynamic_lpar = caps.findtext('VirtualEthernetAdapterDynamic'
                                         'LogicalPartitionCapable')

            # Convert and return what we found as a boolean
            self._supports_dynamic_vlans = (dynamic_lpar == 'true')
        return self._supports_dynamic_vlans

    def _update_sea(self, sea_dev, virt_adpts_list):
        """
        This method does nothing for HMC because VEAs (TrunkAdapters) are
        implicitly attached to the SEA when they're created.
        """
        ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG, "Enter")

        pass

    def _invalidate_k2_cache(self):
        """
        A change to say a NetworkBridge will cause changes to a
        VirtualIOServer.  The cache mechanisms don't allow for this granularity
        and therefore we need to flush the cache with a new read.
        """
        # The only reason we can do this is because we KNOW that it is a
        # topo_hmc
#        self._topo._set_default_k2_read_age(0)
#        self._topo.get_current_config()
#
#        # Set the topo back to the default
#        self._topo._set_default_k2_read_age(-1)

        # For now pass the method, next release remove...
        pass
