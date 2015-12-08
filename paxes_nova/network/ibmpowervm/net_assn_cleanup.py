# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# =================================================================
# =================================================================

"""
    The purpose of this file is to delete network associations
    periodically for the deleted networks
"""
import paxes_nova.db.api as db
from nova.openstack.common import log as logging
from nova.db.sqlalchemy import api as session
from nova import network as net_root

from paxes_nova import _

LOG = logging.getLogger(__name__)


def cleanup_network_associations(context, host_name, db_session=None):
    """
    Removes network associations for deleted networks
    periodically
    :param context: The context for building the data
    :param host_name: The name of the host
    """

    LOG.debug('Entry: network associations cleanup task')
    if not db_session:
        db_session = session.get_session()

    nova_net_api = net_root.API()
    neutron_network_ids = set()
    net_assn_neutron_ids = set()
    # retrieve existing quantum networks
    existing_neutron_networks = nova_net_api.get_all(context)
    for neutron_network in existing_neutron_networks:
        neutron_network_ids.add(neutron_network.get("id"))

    with db_session.begin():
        # retrieve unique network ids from existing network associations
        existing_association_networks = db.\
            network_association_find_distinct_networks(
                context, host_name, db_session)

        for network_in_ass in existing_association_networks:
            net_assn_neutron_ids.add(network_in_ass[0])

        deleted_network_ids = net_assn_neutron_ids - \
            neutron_network_ids

        # delete associations of deleted networks
        for networkid in deleted_network_ids:
            LOG.debug('network associations cleanup'
                      ' for networkid ' + str(networkid))

            net_associations_to_delete = db.\
                network_association_find_all_by_network(
                    context, networkid, db_session)
            for net_association in net_associations_to_delete:
                net_association.delete(context, db_session)
    LOG.debug('Exit: network associations cleanup task')
