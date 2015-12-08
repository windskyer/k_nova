#
# =================================================================
# =================================================================
from nova.openstack.common import log as logging
from oslo.config import cfg
from paxes_nova.db import api as dom_api
from paxes_nova.db.network import models as dom_model
from paxes_nova.network import placement
from paxes_nova.network.ibmpowervm import adapter_mapping

LOG = logging.getLogger(__name__)

CONF = cfg.CONF


class IBMPowerVMNetworkPlacementPVM(placement.IBMPowerVMBaseNetworkPlacement):
    """
    PowerVM-specific implementation for looking up network placement info.
    """

    def __init__(self):
        self.adapter_mapping_task = adapter_mapping.AdapterMappingTaskClass()
        self.host_dom_map = {}

    def _get_host_placement_all(self, context):
        """
        Implemented by child classes.
        :param context: HTTP request context.
        :return: dict of host-network-placement data.
        """
        # Start method by clearing the cache
        self._reset_host_dom_map()

        # Look up host-network-mapping data from DOM task
        host_network_mapping_dict = \
            self.adapter_mapping_task.network_association_find(
                context, None, None,
                filter_closure=self._filter_unavailable_seas)
        host_list = host_network_mapping_dict['host-network-mapping']
        placement_dict = {'host-network-placement': []}
        for host_entry in host_list:
            host_dict = {}
            host_dict['host_name'] = host_entry['host_name']
            host_dict['networks'] = []
            for network_entry in host_entry['networks']:
                network_dict = {}
                network_dict['network_id'] = network_entry['network_id']
                host_dict['networks'].append(network_dict)
            placement_dict['host-network-placement'].append(host_dict)

        # Clear for memory
        self._reset_host_dom_map()

        return placement_dict

    def _get_host_placement_for_host(self, context, host_name):
        """
        Implemented by child classes.
        :param context: HTTP request context.
        :param host_name: Nova name of a host to constrain placement
        :return: list of network ids.
        """
        # Start method by clearing the cache
        self._reset_host_dom_map()

        # Look up host-network-mapping data from DOM task
        host_network_mapping_dict = \
            self.adapter_mapping_task.network_association_find(
                context, None, host_name,
                filter_closure=self._filter_unavailable_seas)
        host_list = host_network_mapping_dict['host-network-mapping']
        id_list = []
        for host_entry in host_list:
            if host_entry['host_name'] == host_name:
                for network_entry in host_entry['networks']:
                    id_list.append(network_entry['network_id'])

        # Clear for memory
        self._reset_host_dom_map()

        return id_list

    def _get_host_placement_for_network(self, context, network_id):
        """
        Implemented by child classes.
        :param context: HTTP request context.
        :param network_id: Neutron network ID to constrain placement.
        :return: list of host names.
        """
        # Start method by clearing the cache
        self._reset_host_dom_map()

        # Look up host-network-mapping data from DOM task
        host_network_mapping_dict = \
            self.adapter_mapping_task.network_association_find(
                context, network_id, None,
                filter_closure=self._filter_unavailable_seas)
        host_list = host_network_mapping_dict['host-network-mapping']
        id_list = []
        for host_entry in host_list:
            for network_entry in host_entry['networks']:
                if network_entry['network_id'] == network_id:
                    id_list.append(host_entry['host_name'])

        # Clear for memory
        self._reset_host_dom_map()

        return id_list

    def _reset_host_dom_map(self):
        """
        Will remove all of the data in the host dom map (cache) because the
        transaction is over
        """
        self.host_dom_map = {}

    def _filter_unavailable_seas(self, context, na_list):
        """
        Removes network / host combinations that have an SEA configured to be
        unavailable.

        :param context: The context that the filtering is done under.
        :param na_list: A list of network association objects to filter.
        :returns: The input parameter, modified to remove unavailable SEAs.
        """
        return_na_list = []
        for na_obj in na_list:
            if not na_obj or not na_obj.sea:
                continue

            # Flip our SEA with the full host SEA chain
            host_dom = self._get_host_dom(context, na_obj.sea)
            s_name = na_obj.sea.name
            s_lpar_id = na_obj.sea.vio_server.lpar_id
            sea_chain = host_dom.find_sea_chain_for_sea_info(s_name, s_lpar_id)

            # Assume that at least one SEA is NOT available
            at_least_one_available = False
            for sea in sea_chain:
                if sea and sea.is_available():
                    at_least_one_available = True
                    break

            # If one of them is available though, then it is a good SEA
            if at_least_one_available:
                return_na_list.append(na_obj)
        return return_na_list

    def _get_host_dom(self, context, sea):
        """
        Maintains a cache of the host dom information.  Is cleared each time
        a filter method is called.  Keeps this information in cache so that
        multiple lookups to the same host are not done.

        :param context: The request to perform DB actions against
        :param sea: The shared ethernet adapter to look up the host dom for
        """
        host_name = sea.vio_server.get_host_name()
        host_dom_cache = self.host_dom_map.get(host_name, None)
        if host_dom_cache is not None:
            return host_dom_cache
        vio_servers = dom_api.vio_server_find_all(context, host_name)
        host_dom = dom_model.Host(host_name, vio_servers)
        self.host_dom_map[host_name] = host_dom
        return host_dom
