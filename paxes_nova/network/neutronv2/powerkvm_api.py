# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# =================================================================
# =================================================================
import copy

from nova.openstack.common import log as logging
from oslo.config import cfg

from paxes_nova.network.common import rpc_caller
from paxes_nova.network.neutronv2 import ibmpower_api

from paxes_nova import _

LOG = logging.getLogger(__name__)
CONF = cfg.CONF


class PowerKVMAPI(ibmpower_api.PowerBaseAPI):
    """
    API wrapper for using POWER KVM Neutron plugin.  To utilize, update the
    nova.conf with the following.
      network_api_class powervc_nova.network.neutronv2.powerkvm_api.PowerKVMAPI
    """

    def _get_viable_hosts(self, context, instance_uuid, hosts_from_db,
                          network_uuids, source_host=None):
        """
        Find the viable hosts that match the network requirements.

        :param context: A context object used to authorize the DB access.
        :param instance_uuid: The instance UUID that will be used to get the
                              network requirements of an instance.
        :param hosts_from_db: A list of compute nodes
        :param network_uuids: An list of requested networks for the deployment.
        :param source_host: When this API is invoked for LPM, the host that
                            is being migrated from.  This is needed to check
                            vswitch config on the source host.
        :returns: A dictionary of hosts with the hostid and the hostname
        """
        candidate_hosts = copy.copy(hosts_from_db)
        temp_context = context.elevated()

        # Check each host
        for host in hosts_from_db:

            # Get a list of all vswitches on this host
            dom = rpc_caller.get_host_ovs(context, [host])[0]
            vswitches = []
            for vswitch in dom.ovs_list:
                vswitches.append(vswitch.name)

            # Check each network
            for net_id in network_uuids:

                # Retrieve the physical_network attribute
                net_info = self.get(temp_context, net_id)
                if not net_info or net_info.get('provider:physical_network')\
                        is None:
                    continue
                physical_network = net_info.get('provider:physical_network')

                # Check if this host/network combo is valid
                if physical_network not in vswitches:
                    candidate_hosts.remove(host)

        return candidate_hosts
