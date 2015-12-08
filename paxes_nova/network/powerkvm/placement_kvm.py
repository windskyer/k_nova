#
# =================================================================
# =================================================================
from nova import db
from nova import network
from nova.openstack.common import log as logging
from oslo.config import cfg
from powervc_nova.network.common import rpc_caller
from powervc_nova.network import placement

LOG = logging.getLogger(__name__)

CONF = cfg.CONF


class IBMPowerVMNetworkPlacementKVM(placement.IBMPowerVMBaseNetworkPlacement):
    """
    PowerKVM-specific implementation for looking up network placement info.
    """

    def __init__(self):
        """
        Constructor
        """
        self.nova_netapi = self._get_nova_netapi()

    def _get_host_placement_all(self, context):
        """
        Get all host placement data from a KVM host.

        :param context: HTTP request context.
        :return: dict of host-network-placement data.
        """
        # Get all the networks up front so we don't have to do it every time
        # through the loop
        all_networks = self._get_all_network_ids(context)

        # Get all the hosts' vswitch info outside the loop to use rpc fanout
        host_doms = rpc_caller.get_host_ovs(context,
                                            self._get_all_host_names(context))

        # Get all network/host combinations
        placement_dict = {'host-network-placement': []}
        for dom in host_doms:
            network_ids = self._get_host_placement_for_host(context,
                                                            dom.host_name,
                                                            all_networks,
                                                            dom)

            placement_dict['host-network-placement'].append(
                {'host_name': dom.host_name,
                 'networks':
                 [{'network_id': net_id} for net_id in network_ids]})

        LOG.debug('host mappings found: %s' % placement_dict)

        return placement_dict

    def _get_host_placement_for_host(self, context, host_name,
                                     all_networks=None, host_dom=None):
        """
        Get host placement data from a specific KVM host.

        :param context: HTTP request context.
        :param host_name: Nova name of a host to constrain placement
        :param all_networks: If passed in, this will be used rather than making
                             a DB call to get them.
        :param host_dom: If passed in, this will be used rather than making a
                    rpc call for the passed in host.
        :return: list of network ids.
        """
        # Do we need to get the networks?
        if all_networks:
            # They passed a list in, saves us a DB call
            LOG.debug('networks passed in')
            network_ids = all_networks
        else:
            # No list given, go to the DB
            network_ids = self._get_all_network_ids(context)
        LOG.debug('unpared network list: %s' % network_ids)

        # Get the dom for this host
        if host_dom:
            # We had one passed in
            LOG.debug('host_dom passed in')
            dom = host_dom
        else:
            # Make a rpc call to get it
            dom = rpc_caller.get_host_ovs(context, [host_name])[0]

        # Get a list of all vswitches on this host
        host_vswitches = []
        for vswitch in dom.ovs_list:
            host_vswitches.append(vswitch.name)
        LOG.debug('vswitches found for host %s: %s' % (host_name,
                                                       host_vswitches))

        # Determine which networks are valid for the specified host
        netid_list = []
        for network_id in network_ids:
            physical_network = self._get_physical_network(context.elevated(),
                                                          network_id)

            # Determine if the current host/network pair is valid
            if physical_network in host_vswitches:
                netid_list.append(network_id)

        LOG.debug('valid networks found for host %s: %s' % (host_name,
                                                            netid_list))
        return netid_list

    def _get_host_placement_for_network(self, context, network_id):
        """
        Get host placement data from all KVM hosts for a specific network.

        :param context: HTTP request context.
        :param network_id: Neutron network ID to constrain placement.
        :return: list of host names.
        """
        # Get all the networks up front so we don't have to do it every time
        # through the loop
        all_networks = self._get_all_network_ids(context)

        # Get all the hosts' vswitch info outside the loop to use rpc fanout
        host_doms = rpc_caller.get_host_ovs(context,
                                            self._get_all_host_names(context))

        # Check all network/host combinations
        host_list = []
        for dom in host_doms:
            # Get all valid networks for this host
            network_ids = self._get_host_placement_for_host(context,
                                                            dom.host_name,
                                                            all_networks,
                                                            dom)

            # If the network we're looking for is in the valid list...
            if network_id in network_ids:
                # This host is good!
                host_list.append(dom.host_name)

        LOG.debug('Valid hosts found for network %s: %s' % (network_id,
                                                            host_list))
        return host_list

    def _get_all_host_names(self, context):
        """
        Finds a list of compute node names.
        :param context: DB context object.
        :returns: A list of compute node names.
        """
        compute_nodes = db.compute_node_get_all(context)
        return_nodes = []
        for compute in compute_nodes:
            if compute.get('service'):
                return_nodes.append(compute['service'].get('host'))

        LOG.debug('Found hosts: %s' % return_nodes)

        return return_nodes

    def _get_all_network_ids(self, context):
        """
        Finds a list of network ids.
        :param context: DB context object.
        :returns: A list of network ids.
        """
        network_dicts = self.nova_netapi.get_all(context)
        network_ids = []
        for network_dict in network_dicts:
            if network_dict.get('id'):
                network_ids.append(network_dict['id'])

        LOG.debug('Found network ids: %s' % network_ids)

        return network_ids

    def _get_physical_network(self, context, network_id):
        """
        Look up the physical_network for a given network id.
        :param context: DB context object.
        :param network_id: A nova network id.
        :returns: A string representing the physical_network column.
        """
        net_info = self.nova_netapi.get(context, network_id)

        LOG.debug('Found %s for network %s' % (net_info, network_id))

        if net_info:
            return net_info.get('provider:physical_network', '')
        return ''

    def _get_nova_netapi(self):
        """
        A method for getting the nova network API, this method can be
        mocked out in tests.
        """
        net_api = network.API()
        return net_api
