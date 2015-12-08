# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# =================================================================
# =================================================================

import logging

from nova.virt.powervm import operator
from nova.virt.powervm import common as pvmcom

from paxes_nova.virt.ibmpowervm.vif import vm_import
from paxes_nova.virt.ibmpowervm.vif import topo
from paxes_nova.virt.ibmpowervm.vif.ivm import utils

from oslo.config import cfg

LOG = logging.getLogger(__name__)

CONF = cfg.CONF


class ImportVMNetworkIVM(vm_import.ImportVMNetwork):
    """
    IVM Implementation of the VM Import function.
    """

    def __init__(self, operator=None):
        """
        ImportVMNetworkIVM constructor

        :param operator: PowerVMOperator to interface with PowerVM host
        """
        if operator:
            self._pvmops = operator
        else:
            pvm_conn = pvmcom.Connection(CONF.powervm_mgr,
                                         CONF.powervm_mgr_user,
                                         CONF.powervm_mgr_passwd,
                                         CONF.powervm_mgr_port)
            self._pvmops = operator.IVMOperator(pvm_conn)

    def list_ports(self, lpar_id):
        """
        Will query the endpoint and determine the network configuration of the
        VM.

        :param lpar_id: The lpar identifier
        :returns: A list of dictionary objects which represents the networking
                  information.  Each dictionary will contain a 'mac_address'
                  and a 'provider:segmentation_id' - which represents the VLAN.
        """
        # Get the adapter information for this LPAR
        cmd = utils.get_veth_list_for_lpar(lpar_id)
        adapter_infos = self._pvmops.run_vios_command(cmd)
        orphaned_pairs = self._get_orphan_pairs()

        # A return list of dictionary objects.
        ret_adapter_list = []

        # Collect data about each adapter assigned to the LPAR
        for adapter_info in adapter_infos:
            values = adapter_info.split(",")
            adapter_dict = {}
            adapter_dict['slot_num'] = values.pop(0)
            adapter_dict['lpar_id'] = values.pop(0)
            adapter_dict['lpar_name'] = values.pop(0)
            adapter_dict['mac_address'] = values.pop(0)
            adapter_dict['provider:segmentation_id'] = values.pop(0)

            adapter_dict['vswitch'] = 'ETHERNET0'

            # Fix up the addl vlans, remove the double quote characters
            addlvlans = values
            if len(addlvlans) == 1 and addlvlans[0] == 'none':
                addlvlans = []
            else:
                addlvlans = map(lambda x: int(x.replace('\"', '')), addlvlans)
            adapter_dict['addl_vlan_ids'] = addlvlans

            # These adapters are guaranteed to be veth
            adapter_dict['is_veth'] = True

            # Required, but non-relevant, Openstack attributes
            adapter_dict['status'] = 'Available'
            adapter_dict['physical_network'] = 'default'

            # Determine whether the adapter is orphaned
            if('provider:segmentation_id' in adapter_dict and
               adapter_dict['provider:segmentation_id'] and
               adapter_dict['provider:segmentation_id'] != 'none'):
                orphan = False
                vlanid = int(adapter_dict['provider:segmentation_id'])
                pair = '%(vs)s:%(vlan)d' % {'vs': adapter_dict['vswitch'],
                                            'vlan': vlanid}
                if pair in orphaned_pairs:
                    orphan = True
                adapter_dict['is_orphan'] = orphan
            else:
                adapter_dict['is_orphan'] = False

            ret_adapter_list.append(adapter_dict)

        # Check for physical adapters assigned to the LPAR
        cmd = utils.get_slot_descriptions(lpar_id)
        slot_descriptions = self._pvmops.run_vios_command(cmd)
        for slot_desc in slot_descriptions:
            if slot_desc.lower().count('ethernet controller') > 0:
                # If there was an 'ethernet' in the slot, we have some physical
                # adapter in here.  We know its not a virtual adapter because
                # the command was not run against virtualio
                #
                # Make a fake adapter...
                adapter_dict = {}
                adapter_dict['is_veth'] = False
                adapter_dict['addl_vlan_ids'] = []
                adapter_dict['slot_num'] = -1
                adapter_dict['lpar_id'] = lpar_id
                adapter_dict['mac_address'] = ""
                adapter_dict['provider:segmentation_id'] = -1

                # Only need one adapter to tell this is bad.
                ret_adapter_list.append(adapter_dict)
                break

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
        return False

    def _has_only_pvid(self, adapter):
        """
        Determines whether or not the adapter only has a PVID (no additional
        VLANs).

        :param adapter: The adapter.
        :returns: True if it has only a PVID.  False if it has 'additional'
                  VLANs
        """
        # only has a PVID if it is a length of 0
        return len(adapter['addl_vlan_ids']) == 0

    def _is_orphan_vlan(self, adapter):
        """
        Determines whether or not the adapter has a PVID that does not have an
        associated SEA, and thus is considered an "orphan".

        :param adapter: The adapter.
        :returns: True if the adapters PVID is an orphan, otherwise False.
        """
        return adapter['is_orphan']
