# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# =================================================================
# =================================================================

import logging

from powervc_nova.virt.ibmpowervm.vif import vm_import
from powervc_nova.virt.ibmpowervm.vif.common import ras
from powervc_nova.virt.ibmpowervm.vif.hmc import topo_hmc
from powervc_nova.virt.ibmpowervm.vif.hmc import utils
from powervc_nova.virt.ibmpowervm.vif import hmc

from oslo.config import cfg

LOG = logging.getLogger(__name__)

CONF = cfg.CONF
CONF.import_opt('ibmpowervm_comm_timeout', 'powervc_nova.virt.ibmpowervm.hmc')


class ImportVMNetworkHMC(vm_import.ImportVMNetwork):
    """
    HMC Implementation of the VM Import function.
    """

    def __init__(self, operator, mtms=None, k2resp=None):
        """
        ImportVMNetworkHMC constructor

        :param operator: Communication channel to interface with PowerVM host
        """
        self._k2op = operator

        # Initialize some cached k2responses
        self._k2entry_ms = None

        if mtms:
            self.mtms = mtms
        else:
            self.mtms = CONF.host

        # Centralized topology service for IVM
        self._topo = topo_hmc.IBMPowerVMNetworkTopoHMC(host_name=self.mtms,
                                                       operator=self._k2op,
                                                       mtms=self.mtms)
        # Need the latest vswitch data
        self._topo._repopulate_vswitch_data()

        if not k2resp:
            self.k2resp = self._k2op.read(
                rootType='ManagedSystem',
                rootId=self._topo._find_managed_system(),
                childType='LogicalPartition',
                timeout=hmc.K2_READ_SEC,
                xag=[None])
        else:
            self.k2resp = k2resp

    def list_ports(self, lpar_id):
        """
        Will query the endpoint and determine the network configuration of the
        VM.

        :param lpar_id: The lpar identifier
        :returns: A list of dictionary objects which represents the networking
                  information.  Each dictionary will contain a 'mac_address'
                  and a 'provider:segmentation_id' - which represents the VLAN.
        """
        # A return list of dictionary objects.
        ret_adapter_list = []

        # Get the adapter information for this LPAR, note that UUIDs are
        # passed in
        logical_partition = utils.get_k2entry_for_lpar_uuid(self.k2resp,
                                                            lpar_id).element
        cna_links = logical_partition.findall('./ClientNetworkAdapters/link')
        orphaned_pairs = self._get_orphan_pairs()

        for cna_link in cna_links:
            try:
                vea = self._k2op.readbyhref(href=cna_link.get('href') +
                                            '?group=None',
                                            timeout=hmc.K2_READ_SEC)
            except Exception:
                LOG.info(_('Read failed for K2 Client Network Adapter (CNA) '
                           'link, skipping the element.'))
                continue

            # If we didn't get an entry back for some reason...
            if not vea.entry:
                ras.trace(LOG, __name__, ras.TRACE_WARNING,
                          'Failed to obtain href link from vea.entry')
                continue
            elem = vea.entry.element

            # Elements needed for import
            adapter_dict = {}
            adapter_dict['slot_num'] = elem.find('VirtualSlotNumber')\
                                           .gettext()
            adapter_dict['mac_address'] = elem.find('MACAddress').gettext()
            adapter_dict['provider:segmentation_id'] = elem.find('PortVLANID')\
                                                           .gettext()
            adapter_dict['status'] = 'Available'
            adapter_dict['physical_network'] = 'default'

            k2_vswitch_id = elem.findtext('VirtualSwitchID')
            k2_vswitch = self._topo.vswitch_map.get(k2_vswitch_id)
            adapter_dict['vswitch'] = k2_vswitch.element.findtext('SwitchName')

            # Elements for internal disection
            adapter_dict['tagged'] = elem.find('TaggedVLANSupported').gettext()
            adapter_dict['is_veth'] = True

            qbg_type = elem.findtext('VirtualStationInterfaceTypeID')
            if qbg_type is not None and len(qbg_type) > 0:
                adapter_dict['is_qbg'] = True
            else:
                adapter_dict['is_qbg'] = False

            # Determine whether the adapter is orphaned
            orphan = False
            vlanid = int(adapter_dict['provider:segmentation_id'])
            pair = '%(vs)s:%(vlan)d' % {'vs': adapter_dict['vswitch'],
                                        'vlan': vlanid}
            if pair in orphaned_pairs:
                orphan = True
            adapter_dict['is_orphan'] = orphan

            ret_adapter_list.append(adapter_dict)

        # Need to check for Host Ethernet Adapters...  K2 has a bug in some
        # versions where we need to check for Port and Ports...
        s = './HostEthernetAdapterLogicalPort/HostEthernetAdapterLogicalPort'
        adpts = logical_partition.findall(s)
        s = './HostEthernetAdapterLogicalPorts/HostEthernetAdapterLogicalPort'
        adpts.extend(logical_partition.findall(s))
        s = ('./PartitionIOConfiguration/ProfileIOSlots/ProfileIOSlot/'
             'AssociatedIOSlot/RelatedIOAdapter')
        adpts.extend(logical_partition.findall(s))
        if len(adpts) > 0:
            # Make a fake adapter...to get it to fail
            adapter_dict = {}
            adapter_dict['is_veth'] = False
            adapter_dict['addl_vlan_ids'] = []
            adapter_dict['slot_num'] = -1
            adapter_dict['lpar_id'] = lpar_id
            adapter_dict['mac_address'] = ""
            adapter_dict['provider:segmentation_id'] = -1

            # Only need one adapter to tell this is bad.
            ret_adapter_list.append(adapter_dict)

        return ret_adapter_list

    def _is_veth_adapter(self, adapter):
        """
        Determines whether or not the adapter is a Virtual Ethernet Adapter.

        :param adapter: The adapter object.  Will vary by platform.
        :returns: True if it is a Virtual Ethernet Adapter.
        """

        # Set via _list_net_adapters method
        return adapter['is_veth']

    def _is_qbg_veth(self, adapter):
        """
        Determines whether or not the adapter is a Qbg Virtual Ethernet
        Adapter.

        :param adapter: The adapter.
        :returns: True if it is a Qbg enabled adapter.
        """
        return adapter['is_qbg']

    def _has_only_pvid(self, adapter):
        """
        Determines whether or not the adapter only has a PVID (no additional
        VLANs).

        :param adapter: The adapter.
        :returns: True if it has only a PVID.  False if it has 'additional'
                  VLANs
        """
        return adapter['tagged'] == 'false'

    def _is_orphan_vlan(self, adapter):
        """
        Determines whether or not the adapter has a PVID that does not have an
        associated SEA, and thus is considered an "orphan".

        :param adapter: The adapter.
        :returns: True if the adapters PVID is an orphan, otherwise False.
        """
        return adapter['is_orphan']
