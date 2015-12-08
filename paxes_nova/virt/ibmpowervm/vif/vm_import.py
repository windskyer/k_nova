# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# =================================================================
# =================================================================

import logging

from nova import context
from nova.db.sqlalchemy import api as db_session

from paxes_nova.db import api as db_api
from paxes_nova.db.network import models as dom_model
from paxes_nova.virt.ibmpowervm.vif.common import ras

from paxes_nova import _

from oslo.config import cfg

LOG = logging.getLogger(__name__)
CONF = cfg.CONF


class ImportVMNetwork(object):
    """
    This class is an 'interface' that will define how a compute nodes
    Virtual Machines should be imported.  There will be multiple
    implementations for each type of managed host.
    """

    def list_ports(self, lpar_id):
        """
        Will query the endpoint and determine the network configuration of the
        VM.

        :param lpar_id: The lpar identifier
        :returns: A list of dictionary objects which represents the networking
                  information.  Each dictionary will contain a 'mac_address'
                  and a 'provider:segmentation_id' - which represents the VLAN.
        """
        raise NotImplementedError()

    def interogate_lpar(self, lpar_id):
        """
        Will perform validation of the LPAR to determine whether or not it
        can be imported.

        :param lpar_id: The LPAR ID
        :returns: A list of reasons why the LPAR may not be imported.
        """
        # Store for all the warnings
        warnings_list = []

        # List all the adapters
        adapters = self.list_ports(lpar_id)

        # For each adapter, do interrogation.
        for adapter in adapters:
            # Validate it IS a Virtual Ethernet Adapter.
            if not self._is_veth_adapter(adapter):
                msg = ras.msg('info', 'IMPORT_NON_VIRTUAL_PORT')
                warnings_list.append(msg)
                continue

            # Validate that it is not a Qbg adapter
            if self._is_qbg_veth(adapter):
                msg = ras.msg('info', 'IMPORT_QBG_PORT')
                warnings_list.append(msg)
                continue

            # Validate that it has only a single VLAN (no addl_vlans)
            if not self._has_only_pvid(adapter):
                msg = ras.msg('info', 'IMPORT_ADDL_VLANS')
                warnings_list.append(msg)
                continue

            # Validate the adapter is not on an orphaned VLAN
            if self._is_orphan_vlan(adapter):
                msg = ras.msg('info', 'IMPORT_ORPHAN_VLANS')
                warnings_list.append(msg)
                continue

        return warnings_list

    def _get_orphan_pairs(self):
        """
        Uses the DOM topology to get a list of all VLANs/VSwitches which are
        orphaned.

        :return: List of VSwitch/VLAN ID pairs.  The list will look like:
                 [ 'ETHERNET0:12', 'ETHERNET1:43']
        """
        # Read the VIOS adapters from the database
        session = db_session.get_session()
        orphan_pairs = []
        with session.begin():
            vios_list = db_api.vio_server_find_all(context.get_admin_context(),
                                                   CONF.host, session)
            host = dom_model.Host(CONF.host, vios_list)
            for vios in vios_list:
                orphan_veas = vios.get_orphan_virtual_ethernet_adapters()
                for vea in orphan_veas:
                    pair_string = '%(vs)s:%(vlan)d' % {'vs': vea.vswitch_name,
                                                       'vlan': vea.pvid}
                    if not host.is_vlan_vswitch_on_sea(vea.pvid,
                                                       vea.vswitch_name):
                        orphan_pairs.append(pair_string)

                    for addl_vlan in vea.addl_vlan_ids:
                        pair_string = ('%(vs)s:%(vlan)d' %
                                       {'vs': vea.vswitch_name,
                                        'vlan': addl_vlan})
                        if not host.is_vlan_vswitch_on_sea(addl_vlan,
                                                           vea.vswitch_name):
                            orphan_pairs.append(pair_string)

        return orphan_pairs

    def _is_veth_adapter(self, adapter):
        """
        Determines whether or not the adapter is a Virtual Ethernet Adapter.

        :param adapter: The adapter object.  Will vary by platform.
        :returns: True if it is a Virtual Ethernet Adapter.
        """
        raise NotImplementedError()

    def _is_qbg_veth(self, adapter):
        """
        Determines whether or not the adapter is a Qbg Virtual Ethernet
        Adapter.

        :param adapter: The adapter.
        :returns: True if it is a Qbg enabled adapter.
        """
        raise NotImplementedError()

    def _has_only_pvid(self, adapter):
        """
        Determines whether or not the adapter only has a PVID (no additional
        VLANs).

        :param adapter: The adapter.
        :returns: True if it has only a PVID.  False if it has 'additional'
                  VLANs
        """
        raise NotImplementedError()

    def _is_orphan_vlan(self, adapter):
        """
        Determines whether or not the adapter has a PVID that does not have an
        associated SEA, and thus is considered an "orphan".

        :param adapter: The adapter.
        :returns: True if the adapters PVID is an orphan, otherwise False.
        """
        raise NotImplementedError()
