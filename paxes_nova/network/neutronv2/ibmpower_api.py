#
#
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import nova
from nova import db
from nova.network.neutronv2 import api as netapi
from nova.openstack.common import log as logging
from nova.network import neutronv2
from oslo.config import cfg
from nova import exception
from nova.openstack.common import excutils
from paxes_nova import _

LOG = logging.getLogger(__name__)
CONF = cfg.CONF


class PowerBaseAPI(netapi.API):

    """
    Base class for API wrapper to abstract out function shared by Power VM
    and Power KVM.
    """

    def get_viable_hosts(self, context, instance_uuid,
                         requested_networks=None, source_host=None):
        """
        Find the viable hosts that match the network requirements.

        :param context: A context object that is used to authorize
                        the DB access.
        :param instance_uuid: The instance UUID that will be used to
                        get the network requirements of an instance.
        :param requested_networks: An optional list of requested networks for
                        the deployment.  If set, will only place on hosts that
                        have valid targets for that host.  If not set, then
                        all hosts will be allowed.

                        Format of this should be:
                        [['1d4dff20-ed6d-47ff-a7b0-5f62bbf5b635',
                          '9.114.181.199', None]]
                        Note that this is a list of list.  The first element
                        of the embedded list is the UUID of the network.
        :param source_host: When this API is invoked for LPM, the host that
                            is being migrated from.  This is needed to check
                            vswitch config on the source host.
        :returns: A dictionary of hosts with the hostid and the hostname
        """
        # Parse the requested networks to a UUID list
        network_uuids = []
        if requested_networks:
            for requested_network in requested_networks:
                if len(requested_network) > 1:
                    network_uuids.append(requested_network[0])
        else:
            instance = db.instance_get_by_uuid(context, instance_uuid)
            nw_infos = []
            if instance is not None:
                nw_infos = self.get_instance_nw_info(context, instance)
            for nw_info in nw_infos:
                network_uuids.append(nw_info['network']['id'])

        # Get all compute hosts from the database
        hosts_from_db = self._get_compute_nodes_from_DB(context)

        # Determine which hosts are viable using a hypervisor-specific method
        candidate_hosts = self._get_viable_hosts(context, instance_uuid,
                                                 hosts_from_db, network_uuids,
                                                 source_host)

        # Convert the viable hosts to the format expected by the scheduler
        viable_hosts = {}
        for candidate_host in candidate_hosts:
            host_name = {'host': candidate_host}
            viable_hosts[candidate_host] = host_name
        return viable_hosts

    def _get_compute_nodes_from_DB(self, context):
        """
        This returns a list of compute nodes after querying the Nova DB

        :param context: A context object that is used to authorize
                        the DB access.
        :returns: A set of compute nodes.
        """
        context = context.elevated()
        compute_nodes = db.compute_node_get_all(context)
        return_nodes = []
        for compute in compute_nodes:
            service = compute['service']
            if not service:
                LOG.warn(_("No service for compute ID %s" % compute['id']))
                continue
            host = service['host']
            return_nodes.append(host)
        return return_nodes

    def _create_port(self, port_client, instance, network_id, port_req_body,
                     fixed_ip=None, security_group_ids=None,
                     available_macs=None, dhcp_opts=None):
        """Attempts to create a port for the instance on the given network.

        :param port_client: The client to use to create the port.
        :param instance: Create the port for the given instance.
        :param network_id: Create the port on the given network.
        :param port_req_body: Pre-populated port request. Should have the
            device_id, device_owner, and any required neutron extension values.
        :param fixed_ip: Optional fixed IP to use from the given network.
        :param security_group_ids: Optional list of security group IDs to
            apply to the port.
        :param available_macs: Optional set of available MAC addresses to use.
        :param dhcp_opts: Optional DHCP options.
        :returns: ID of the created port.
        :raises PortLimitExceeded: If neutron fails with an OverQuota error.
        """
        # Check to see if the port is in use already, if there is a fixed ip
        if fixed_ip is not None and network_id is not None:
            search_opts = {'network_id': network_id,
                           'fixed_ips': 'ip_address=%s' % fixed_ip}
            existing_ports = port_client.list_ports(**search_opts)

            # If there are any ports, then the IP is already in use on this
            # network.
            if existing_ports is not None and\
                    len(existing_ports.get('ports')) > 0:
                i_uuid = existing_ports.get('ports')[0].get('device_id')
                raise nova.exception.FixedIpAlreadyInUse(address=fixed_ip,
                                                         instance_uuid=i_uuid)

        # Need to change the available macs to be an ordered list, with the
        # lowest MAC as the last element.  This is needed because most AE's
        # will assume lowest mac to eth0, and highest mac to the last adapter.
        # The set of macs passed in pops in no determinate order...so we flip
        # to a list, reorder and then pass in the new list.
        ordered_macs = None
        if available_macs:
            ordered_macs = list(available_macs)
            ordered_macs.sort(reverse=True)

        # Create the actual port.
        port = super(PowerBaseAPI, self)._create_port(port_client, instance,
                                                      network_id,
                                                      port_req_body, fixed_ip,
                                                      security_group_ids,
                                                      ordered_macs, dhcp_opts)

        # Find the used mac...and remove it from the original set
        if available_macs:
            mac_to_remove = None
            for mac in available_macs:
                if mac not in ordered_macs:
                    mac_to_remove = mac
                    break
            available_macs.remove(mac_to_remove)

        return port

    def _get_network_all(self, context):
        """
        This returns a list of networks after querying the Neutron DB
        wrote a getter here for the super class wrapper, we will remove
        this method once, we find a better way of mocking a super class
        object in pymox.

        :param context: A context object that is used to authorize
                        the DB access.
        :returns: A set of networks.
        """
        context = context.elevated()
        return super(PowerBaseAPI, self).get_all(context)

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
        raise NotImplementedError('_get_viable_hosts not implemented')

    def validate_networks(self, context, requested_networks, num_instances):
        """Validate that the tenant can use the requested networks.

        Return the number of instances than can be successfully allocated
        with the requested network configuration.
        """
        LOG.debug(_('validate_networks() for %s'),
                  requested_networks)
        LOG.debug("This function is added as a temporary work around to "
                  " fix PowerVC STG Defect 19419 and this OpenStack community "
                  "bug: https://bugs.launchpad.net/nova/+bug/1304409. "
                  "This function should be removed when the community bug "
                  "is fixed and picked up by PowerVC.")
        neutron = neutronv2.get_client(context)
        ports_needed_per_instance = 0

        if not requested_networks:
            nets = self._get_available_networks(context, context.project_id,
                                                neutron=neutron)
            if len(nets) > 1:
                # Attaching to more than one network by default doesn't
                # make sense, as the order will be arbitrary and the guest OS
                # won't know which to configure
                msg = _("Multiple possible networks found, use a Network "
                        "ID to be more specific.")
                raise exception.NetworkAmbiguous(msg)
            else:
                ports_needed_per_instance = 1

        else:
            net_ids = []

            for (net_id, _i, port_id) in requested_networks:
                if port_id:
                    try:
                        port = neutron.show_port(port_id).get('port')
                    except neutronv2.exceptions.NeutronClientException as e:
                        if e.status_code == 404:
                            port = None
                        else:
                            with excutils.save_and_reraise_exception():
                                LOG.exception(_("Failed to access port %s"),
                                              port_id)
                    if not port:
                        raise exception.PortNotFound(port_id=port_id)
                    if port.get('device_id', None):
                        raise exception.PortInUse(port_id=port_id)
                    if not port.get('fixed_ips'):
                        raise exception.PortRequiresFixedIP(port_id=port_id)
                    net_id = port['network_id']
                else:
                    ports_needed_per_instance += 1

                if net_id in net_ids:
                    raise exception.NetworkDuplicated(network_id=net_id)
                net_ids.append(net_id)

            # Now check to see if all requested networks exist
            nets = self._get_available_networks(context,
                                                context.project_id, net_ids,
                                                neutron=neutron)

            if len(nets) != len(net_ids):
                requsted_netid_set = set(net_ids)
                returned_netid_set = set([net['id'] for net in nets])
                lostid_set = requsted_netid_set - returned_netid_set
                id_str = ''
                for _id in lostid_set:
                    id_str = id_str and id_str + ', ' + _id or _id
                raise exception.NetworkNotFound(network_id=id_str)

        # Note(PhilD): Ideally Nova would create all required ports as part of
        # network validation, but port creation requires some details
        # from the hypervisor.  So we just check the quota and return
        # how many of the requested number of instances can be created

        ports = neutron.list_ports(tenant_id=context.project_id)['ports']
        quotas = neutron.show_quota(tenant_id=context.project_id)['quota']
        if quotas.get('port') == -1:
            # Unlimited Port Quota
            return num_instances
        else:
            free_ports = quotas.get('port') - len(ports)
            ports_needed = ports_needed_per_instance * num_instances
            if free_ports >= ports_needed:
                return num_instances
            else:
                return free_ports // ports_needed_per_instance
