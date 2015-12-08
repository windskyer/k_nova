#
# =================================================================
# =================================================================
from oslo.config import cfg
CONF = cfg.CONF


class IBMPowerVMBaseNetworkPlacement(object):
    """
    Serves as a base class for host-agnostic placement/deploy data.
    """

    def get_placement(self, context, host_name, network_id, list_only):
        """
        Get the placement dictionary, optionally constrained by a host or
        network.
        :param context: HTTP request context.
        :param host_name: Nova name of a host to optionally constrain
                          placement.
        :param network_id: Neutron network ID to optionally constrain
                           placement.
        :param list_only: If true, only a list of IDs will be returned.
        :returns: Dict of placement data.
        """
        # Handle the case where a host_name was provided - return network ids
        if host_name and not network_id:
            id_list = self._get_host_placement_for_host(context, host_name)
            if list_only == 'true':
                placement_dict = {'host-network-placement': id_list}
            else:
                host_dict = {}
                host_dict['host_name'] = host_name
                host_dict['networks'] = []
                for network_id in id_list:
                    host_dict['networks'].append({'network_id': network_id})
                placement_dict = {'host-network-placement':
                                  [host_dict]}
        # Handle the case where a network was provided - return host names
        elif network_id and not host_name:
            id_list = self._get_host_placement_for_network(context, network_id)

            if list_only == 'true':
                placement_dict = {'host-network-placement': id_list}
            else:
                placement_dict = {'host-network-placement': []}
                for host_id in id_list:
                    host_dict = {}
                    host_dict['host_name'] = host_id
                    host_dict['networks'] = []
                    host_dict['networks'].append({'network_id': network_id})
                    placement_dict['host-network-placement'].append(host_dict)

        # Handle the case where nothing was provided - return all data
        elif not host_name and not network_id:
            placement_dict = self._get_host_placement_all(context)

        return placement_dict

    def _get_host_placement_all(self, context):
        """
        Implemented by child classes.
        :param context: HTTP request context.
        :return: dict of host-network-placement data.
        """
        raise NotImplementedError('_get_host_placement_all not implemented')

    def _get_host_placement_for_host(self, context, host_name):
        """
        Implemented by child classes.
        :param context: HTTP request context.
        :param host_name: Nova name of a host to constrain placement
        :return: list of network ids.
        """
        raise NotImplementedError('_get_host_placement_for_host not '
                                  'implemented')

    def _get_host_placement_for_network(self, context, network_id):
        """
        Implemented by child classes.
        :param context: HTTP request context.
        :param network_id: Neutron network ID to constrain placement.
        :return: list of host names.
        """
        raise NotImplementedError('_get_host_placement_for_network not '
                                  'implemented')
