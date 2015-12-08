# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# =================================================================
# =================================================================
import copy

from nova.network import neutronv2 as neutron
from nova.network.neutronv2 import api as netapi
from nova.openstack.common import log as logging
from nova.compute import utils as compute_utils
from nova.network import api as nova_net
from nova.network import model as network_model

from oslo.config import cfg

from paxes_nova.virt.ibmpowervm.vif.common import ras
from paxes_nova.virt.ibmpowervm.vif.common import exception as powexc
from paxes_nova.network.ibmpowervm import adapter_mapping as mapping_task
from paxes_nova.network.neutronv2 import ibmpower_api
from paxes_nova.db import api as dom_api
from paxes_nova.db.network import models as dom_model

from paxes_nova import _

LOG = logging.getLogger(__name__)
CONF = cfg.CONF


class PowerVMAPI(ibmpower_api.PowerBaseAPI):

    """
    API wrapper for using POWER VM Neutron plugin.  Must be paired with the
    POWER VM Neutron Plugin.  To utilize, update the nova.conf with the
    following.
        - network_api_class = nova.network.neutronv2.ibmpowervm_api.PowerVMAPI
    """

    def __init__(self):
        netapi.API.__init__(self)
        self.adapter_mapping_task = mapping_task.AdapterMappingTaskClass()

    def _build_network_info_model(self, context, instance, networks=None,
                                  port_ids=None):
        LOG.debug('Enter _build_network_info_model with instance %s and '
                  'networks %s' % (instance['project_id'], networks))

        # Get all the nova networks associated with the neutron network passed
        # in.  If no neutron network was passed in, this will return all nova
        # networks.
        net_infos = []
        if networks is None and port_ids is None:
            # We need to check to see if the cache thinks we're none as well
            network_info = compute_utils.get_nw_info_for_instance(instance)

            # If the network info is an empty list...it may be valid.  However,
            # it is improbable.  We should go query against the master list,
            # which is neutron.
            if network_info is None or len(network_info) == 0:
                net_infos = self._update_instance_nw_cache(instance, context)
        # We didn't get the network infos - this could mean that the cache data
        # on the instance is proper. So let's call neutron's parent API and
        # get network details.
        if net_infos == []:
            net_infos = super(PowerVMAPI,
                              self)._build_network_info_model(context,
                                                              instance,
                                                              networks,
                                                              port_ids)
        LOG.debug('Found net_infos %s' % net_infos)

        # Get the neutron networks related to our instance if the invoker did
        # not pass them in.
        if not networks:
            networks = self._get_available_networks(context,
                                                    instance['project_id'])
            msg = (ras.vif_get_msg
                  ('info', 'NO_NET_FOUND') % {'host': instance['host']})
            ras.function_tracepoint(LOG, __name__, ras.TRACE_INFO, msg)

        # Pare down the nova networks list to only include those that map to
        # the neutron network we're interested in.  This will also add in the
        # vlan id to the nova network object.
        return self._match_network_models(networks, net_infos, context)

    def _update_instance_nw_cache(self, instance, context):
        """
        This method will update the network cache for the instance by gathering
        data from neutron. This method should be only called when the network
        cache is empty for the instance
        :param instance: The instance object passed from the DB
        :param context: The context object
        :return nw_info: This contains the network
        """
        LOG.info(_("The network cache for this instance with uuid: %(uuid)s "
                   "is empty. Will update the information "
                   "from neutron") % {'uuid': instance['uuid']})
        nw_info = network_model.NetworkInfo()
        search_opts = {'tenant_id': instance['project_id'],
                       'device_id': instance['uuid']}
        client = neutron.get_client(context, admin=True)
        data = client.list_ports(**search_opts)
        ports = data.get('ports', [])

        networks = self._get_available_networks(context,
                                                instance['project_id'])
        for port in ports:
            network_IPs = self._nw_info_get_ips(client, port)
            subnets = self._nw_info_get_subnets(context, port, network_IPs)

            devname = "tap" + port['id']
            devname = devname[:network_model.NIC_NAME_LEN]

            network, ovs_interfaceid = self._nw_info_build_network(port,
                                                                   networks,
                                                                   subnets)

            nw_info.append(network_model.VIF(id=port['id'],
                                             address=port['mac_address'],
                                             network=network,
                                             type=port.get('binding:vif_type'),
                                             ovs_interfaceid=ovs_interfaceid,
                                             devname=devname))

            nova_net.update_instance_cache_with_nw_info(self, context,
                                                        instance, nw_info)
        return nw_info

    def _match_network_models(self, networks, net_infos, context):
        """
        Will match the neutron network model into the Nova network model and
        will embed the vSwitch and VLAN ID into the bridge name.

        :param networks: A set of Neutron Networks (the values returned from
                         the REST API)
        :param net_infos: The NOVA Network objects used by the compute layer.
                          These objects will be updated to contain the
                          provider:segmentation_id into the bridge attribute
                          in this object
        :param context: This is the context object to access the DB.
        :returns: The original net_infos, however updated with the bridge
                  which will be vSwitch/VLAN_ID
        """
        LOG.debug(('Enter _match_network_models with networks:\n%s\n\n'
                   'net_infos:\n%s' % (networks, net_infos)))
        # For each element, add in the proper VLAN ID into the bridge
        for network in networks:
            net_info = self._find_net_info_for_network(net_infos, network)
            if net_info is None:
                LOG.debug('Found no net_info for network %s' % network)
                continue

            # Get the VLAN off the network.  If one doesn't exist, default
            # to a value of 1.
            if network.get('provider:segmentation_id') is not None:
                vlanid = int(network['provider:segmentation_id'])
                LOG.info(ras.vif_get_msg('info', 'NET_VLAND_ID') %
                         {'vlan': vlanid})
            else:
                # Since there was no vlan id - setting it to 1
                vlanid = 1
                LOG.info(ras.vif_get_msg('info', 'NO_NET_VLAN'))

            # Set the data for this network into its corresponding net_info
            net_info['vlan'] = ("%(vlan)s" % {"vlan": vlanid})

        LOG.debug('Exiting _match_network_models with net_infos %s' %
                  net_infos)
        return net_infos

    def build_default_network_assn(self, context, host_dom, vlanid, net_id,
                                   vswitch=None):
        """
        Invokes the default network association.  Mostly pass thru, however
        if a SEA can not be found will return None.
        """
        try:
            adpt_mapper = self.adapter_mapping_task
            return adpt_mapper.build_default_network_assn(context, host_dom,
                                                          vlanid, net_id,
                                                          vswitch)
        except powexc.IBMPowerVMValidSEANotFound:
            LOG.info(_("Unable to build default network association.  No valid"
                       " Shared Ethernet Adapters for VLAN ID %(vid)s" %
                       {'vid': str(vlanid)}))
            return None

    def _find_net_info_for_network(self, net_infos, network):
        """
        Finds the matching net_info for the network

        :param net_infos: A set of Neutron Networks (the values returned from
                          the Neutron REST API)
        :param network: The specific NOVA network object to pair to a specific
                        Neutron Network (net_infos)
        :returns: The corresponding Neutron Network (net_info) for the NOVA
                  network.  Will return None if there is not an appropriate
                  Neutron Network for the Nova network.
        """
        for net_info in net_infos:
            if net_info['network']['id'] == network['id']:
                return net_info
        return None

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

        # Determine which vswitches are needed for each network on source_host
        required_vswitches = {}
        if source_host:
            netasc_list = dom_api.network_association_find_all(context,
                                                               source_host)
            for netasc in netasc_list:
                for net_uuid in network_uuids:
                    if netasc.neutron_net_id == net_uuid and\
                            netasc.sea is not None:
                        required_vswitches[net_uuid] = \
                            netasc.sea.get_primary_vea().vswitch_name

        dom_factory = dom_model.DOM_Factory()
        # Check if each host contains a valid network
        for host in hosts_from_db:
            # Keep track of the networks that remaining to be processed.
            networks_to_process = copy.copy(network_uuids)
            if not networks_to_process:
                continue

            # Get the DOM so that we can verify the state of the chains.
            host_dom = dom_model.Host(host,
                                      dom_api.vio_server_find_all(context,
                                                                  host))

            # Walk through each of the network associations that exist for
            # the host and see if it was set to be used.  A 'use this host'
            # indication is if the network associations SEA is not None
            netasc_list = dom_api.network_association_find_all(context, host)
            for net_assn in netasc_list:
                if net_assn.neutron_net_id in network_uuids:
                    # No longer a network to process
                    networks_to_process.remove(net_assn.neutron_net_id)

                    # Check the SEA chain.  At least one SEA in the chain
                    # should be available.  If there are no SEAs, then remove
                    # it as a candidate
                    at_least_one_available = False
                    if net_assn.sea:
                        s_nm = net_assn.sea.name
                        s_lp = net_assn.sea.vio_server.lpar_id
                        sea_chain = host_dom.find_sea_chain_for_sea_info(s_nm,
                                                                         s_lp)
                        for sea in sea_chain:
                            if sea and sea.is_available():
                                at_least_one_available = True
                                break

                    # Check the vswitches.  The same vswitch name must be used
                    # on the source and target host.
                    vswitch_matches = True
                    if net_assn.neutron_net_id in required_vswitches and\
                            net_assn.sea is not None:
                        if(net_assn.sea.get_primary_vea().vswitch_name !=
                           required_vswitches[net_assn.neutron_net_id]):
                            vswitch_matches = False

                    # Check the conditions.  If there isn't one available,
                    # yet the host is still here...remove that host as a
                    # candidate.
                    if((not vswitch_matches or not at_least_one_available) and
                       host in candidate_hosts):
                        candidate_hosts.remove(host)

            # Skip the next, computationally expensive, step if there are no
            # more networks to process.
            if len(networks_to_process) == 0:
                continue

            # There may be some networks that haven't been processed yet.
            # This is typically for when a host was added after the network
            # was created.  In this scenario, we should call down into
            # the adapter mapping task and find the default.  The default
            # may be do not use.
            #
            # We don't do this by default because it's far more expensive
            # than a database operation, and we want the standard flow to be
            # as fast as possible.
            temp_context = context.elevated()
            vios_list = dom_api.vio_server_find_all(temp_context, host)
            host_dom = dom_factory.create_host(host, vios_list)
            for net_id in networks_to_process:
                net_info = self.get(temp_context, net_id)
                if not net_info or\
                        net_info.get('provider:segmentation_id') is None:
                    continue
                vlanid = int(net_info.get('provider:segmentation_id'))
                sea = self.build_default_network_assn(temp_context,
                                                      host_dom,
                                                      vlanid,
                                                      net_id)
                # If the SEA that came back from this is None, we need to
                # remove it as a candidate.  We can be confident at this
                # point though that the network association was created,
                # thus speeding up our flow through next time.
                if sea is None or not sea.is_available():
                    candidate_hosts.remove(host)

        return candidate_hosts
