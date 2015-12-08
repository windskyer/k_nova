# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# =================================================================
# =================================================================

"""
    The purpose of this file is to reconcile the Network Inventory data that
    exists on the physical system with the information stored in the
    database.

    Elements of the DOM will likely need to be removed, others added,
    attributes modified, etc...  The functions here should provide a mechanism
    to reconcile all of that data.
"""

import pprint

import paxes_nova.db.api as db
import paxes_nova.db.network.models as dom
from paxes_nova.virt.ibmpowervm.vif.common import ras

from nova.network import neutronv2
from nova.db import api as nova_db
from nova.openstack.common import lockutils
from nova.openstack.common import log as logging
from nova.db.sqlalchemy import api as session

from paxes_nova import _


LOG = logging.getLogger(__name__)
LOG_NO_NAME = logging.getLogger('')


USED_PORT_THRESHOLD = 20
USED_PORT_COUNT = 0


@lockutils.synchronized('host_seas', 'host-seas-')
def reconcile(context, host_data, dom_factory=dom.DOM_Factory(),
              db_session=None):
    """
    This is the entry method to reconcile the host data from the system with
    that stored in the database.

    :param context: The database context.
    :param host_data: A dictionary of data that represents the latest inventory
                      information on the server.  The data should be in the
                      network DOM format.
    :param dom_factory: Optional factory used to create the DOM objects.  Not
                        required to be set.
    :param db_session: The database session.  Should be started and finalized
                       outside this class.
    """
    if not db_session:
        db_session = session.get_session()

    try:
        with db_session.begin():
            _reconcile_host(context, host_data, dom_factory, db_session)
    except Exception as e:
        _log_before_and_after(context, host_data, db_session)
        msg = ras.vif_get_msg('error', 'HOST_RECONCILE_ERROR')
        ras.function_tracepoint(LOG, __name__, ras.TRACE_ERROR, msg)
        ras.function_tracepoint(
            LOG, __name__, ras.TRACE_EXCEPTION, e.message)
        raise

    # We only want to run the used port clean up rarely, as it is expensive
    global USED_PORT_COUNT
    USED_PORT_COUNT = USED_PORT_COUNT + 1
    if USED_PORT_COUNT >= USED_PORT_THRESHOLD:
        USED_PORT_COUNT = 0
        _neutron_unused_port_cleanup(context)


def _neutron_unused_port_cleanup(context):
    """
    This task periodically runs to check if there are any 'stale' neutron
    ports that need cleanup or not and eventually cleans it up. 'stale'
    ports are those which are not in use by any of the instance and hence
    should be freed up for deploys if they exit

    :param context: The context object.
    """
    LOG.debug('pvc_nova.compute.manager.PowerVCComputeManager '
              '_neutron_unused_port_cleanup: Cleaning up unused neutron '
              'ports...')
    #Get all the running instances from the DB which aren't deleted.
    ports_ids_to_delete = []
    try:
        # Expensive!
        db_instances = nova_db.instance_get_all(context)

        # Get all the neutron ports.  Expensive!
        network_data = neutronv2.get_client(context).list_ports()
        ports = network_data.get('ports', [])

        # We run through the list of ports and see if they have a device_id
        # If the device_id exists, we see if they are in use with an
        # instance or not, if No, then we delete them.
        for port in ports:
            found_server = False
            for instance in db_instances:
                if port.get('device_id', None) is not None and\
                        port['device_id'] == instance['uuid']:
                    found_server = True
                    break

            # Only delete ports that are owned by Compute.  Do NOT
            # delete ports owned by say SCE.
            dev_owner = port.get('device_owner', None)
            owned_by_compute = False
            if dev_owner is not None and dev_owner == 'compute:None':
                owned_by_compute = True

            if not found_server and owned_by_compute:
                ports_ids_to_delete.append(port['id'])
                LOG.info(_('Deleting neutron port with id %(id)s and data '
                           '%(data)s') % {'id': port['id'], 'data': str(port)})
    except Exception as exc:
        LOG.exception(exc)

    # Now iterate the legit candidates for deletion and delete them.
    for port_id in ports_ids_to_delete:
        try:
            neutronv2.get_client(context).delete_port(port_id)
            LOG.warning(_('Cleaning up the unused neutron port with id '
                          '%(port)s') % {'port': port_id})
        except Exception as exc:
            LOG.exception(exc)
    LOG.debug('Exiting neutron port clean up')


def _log_before_and_after(context, host_data, db_session):
    """
    Logs the DOM info before and after the inventory job to help debug.

    :param context: The database context.
    :param host_data: A dictionary of data that represents the latest inventory
                      information on the server.  The data should be in the
                      network DOM format.
    :param db_session: The database session.  Should be started and finalized
                       outside this class.
    """
    try:
        # Read the new VIOS data, and the old VIOS data from the database
        new_vioses = []
        old_vioses = []
        server_dom = dom.parse_to_host(host_data, dom.No_DB_DOM_Factory())
        for vios in server_dom.vio_servers:
            new_vioses.append(pprint.pformat(vios.to_dictionary()))
        for vios in db.vio_server_find_all(context, server_dom.host_name,
                                           db_session):
            old_vioses.append(pprint.pformat(vios.to_dictionary()))

        # Log the VIOS data.  Use LOG_NO_NAME to improve readability.
        LOG_NO_NAME.error(_('New VIOS data:'))
        LOG_NO_NAME.error(_('host_name:'))
        LOG_NO_NAME.error(server_dom.host_name)
        for vios_string in new_vioses:
            for line in vios_string.split('\n'):
                LOG_NO_NAME.error(line)
        LOG_NO_NAME.error('Old VIOS data:')
        for vios_string in old_vioses:
            for line in vios_string.split('\n'):
                LOG_NO_NAME.error(line)

    except Exception:
        # This method is only invoked to log data during error flows.  If an
        # error occurs during the error flow, that's not much we can do.
        pass


def _cleanup_network_associations(host_dom, net_assns, context, db_session):
    """
    This method will look at all NetworkAssociations on the system and move
    any associated with SEAs that are now gone (if the SEA has a
    failover SEA available).

    :param host_dom: Host DOM from the db.  At this point, it should match the
                     live topology and have been saved to the db.  Thus, we
                     can use it for setting and saving NetworkAssociations.
    :param net_assns: NetworkAssociations that existed before reconcile ran.
    :param context: Context to use for db operations
    :param db_session: Session to use for db operations
    """
    LOG.debug('Enter _cleanup_network_associations')

    # If we have associations to inspect, be sure all SEA chains up to date.
    # Do it outside the loop so we just rebuild them once, rather than every
    # time through the loop.
    if net_assns:
        host_dom.find_sea_chains(rebuild=True)
    else:
        # No associations.  Just return.
        LOG.debug('Exit _cleanup_network_associations, no associations')

    # Look for NetworkAssociations that need to be moved.
    for net_assn in net_assns:
        LOG.debug('Processing neutron network %s' % net_assn.neutron_net_id)

        # See what SEA this NetworkAssociation was originally on.
        orig_sea = net_assn.sea
        if orig_sea is None:
            LOG.debug('No SEA for neutron network %s, skipping.' %
                      net_assn.neutron_net_id)
            continue

        # Get the chain the original SEA is on.  Note, even if the original
        # SEA is no longer available (ie, its VIOS is RMC inactive), it should
        # still be found in a chain since chains include all available and
        # unavailable SEAs.  Note, we have to find the chain by the primary
        # VEA info (pvid, vswitch, num additional VEAs) because it's possible
        # the orig_sea no longer even exists in the dom (ie, the VIOS was
        # deleted altogether, not just rmc inactive).
        sea_chain = host_dom.find_sea_chain_by_pvea_info(orig_sea)

        # Now find the proper SEA to use (ie, highest priority SEA)
        sea_to_use = (sea_chain[0] if sea_chain else None)

        # sea_to_use will be None if the original SEA was deleted and it had
        # no failover SEAs (or it did, but they were all deleted, too).
        if sea_to_use is not None:
            if orig_sea != sea_to_use:
                LOG.info(_('Moving neutron network %(netid)s\'s association '
                           'from SEA %(sea)s on lpar %(lparid)d to SEA '
                           '%(sea_next)s on lpar %(lpar_next)d' %
                           {'netid': net_assn.neutron_net_id,
                            'sea': orig_sea.name,
                            'lparid': net_assns[net_assn],
                            'sea_next': sea_to_use.name,
                            'lpar_next': sea_to_use.vio_server.lpar_id}))
                db.network_association_put_sea(context,
                                               host_dom.host_name,
                                               net_assn.neutron_net_id,
                                               sea_to_use,
                                               db_session)
            else:
                LOG.debug('Neutron network %s already associated properly'
                          % net_assn.neutron_net_id)
        else:
            LOG.warning(_('Unable to find new SEA for neutron network '
                          '%(netid)s that was originally on sea %(sea)s, '
                          'lpar %(lparid)d' %
                          {'netid': net_assn.neutron_net_id,
                           'sea': orig_sea.name,
                           'lparid': net_assns[net_assn]}))

    LOG.debug('Exit _cleanup_network_associations')


def _reconcile_host(context, host_data, dom_factory=dom.DOM_Factory(),
                    db_session=None):
    """
    Performs the actual reconciliation at the host level

    :param context: The database context.
    :param host_data: A dictionary of data that represents the latest inventory
                      information on the server.  The data should be in the
                      network DOM format.
    :param dom_factory: Optional factory used to create the DOM objects.  Not
                        required to be set.
    :param db_session: The database session.  Should be started and finalized
                       outside this class.
    """
    if not db_session:
        db_session = session.get_session()

    # Parse the inventory data into a DOM object.  Use the no_db DOM factory
    # as we want to parse into non-DB backed elements to start...
    non_db_fact = dom.No_DB_DOM_Factory()
    server_dom = dom.parse_to_host(host_data, non_db_fact)

    msg = (ras.vif_get_msg('info', 'RECONCILE_HOST_START') %
           {'host': server_dom.host_name})
    ras.trace(LOG, __name__, ras.TRACE_DEBUG, msg)

    # Get the inventory data from the database.
    db_vio_servers = db.vio_server_find_all(context, server_dom.host_name,
                                            db_session)

    # If there are no VIO Servers, the system may be turned off.  It is very
    # unlikely that they are actually removed (all of them at least).
    # Therefore, we flip the SEAs in each VioServer to a state of unavailable
    # and they do not show up in the UI...but are not deleted.
    if len(server_dom.vio_servers) == 0:
        LOG.info(_("Flipping host %s to unavailable due to lack of VioServers"
                   % server_dom.host_name))
        _make_system_unavailable(db_vio_servers, context, db_session)
        return

    # The first step is to find VIO Servers do add/remove/modify.  Those are
    # the three passes that need to be made.
    #
    # We start with the idea that all of the data base items should be removed.
    # From there, we parse down which are still on the system (therefore need
    # to be modified) and then the new adds.
    db_vios_to_del = dom.shallow_copy_as_ordinary_list(db_vio_servers)
    srv_vios_to_add = []
    srv_vios_to_modify = []

    for vio_server in server_dom.vio_servers:
        db_vios = _find_vios(db_vio_servers, vio_server.lpar_id)
        if db_vios:
            srv_vios_to_modify.append(vio_server)
            db_vios_to_del.remove(db_vios)
        else:
            srv_vios_to_add.append(vio_server)

    # Now that we know what to modify/create/delete...loop through each and
    # execute the commands to reconcile
    db_host_dom = dom.Host(server_dom.host_name, db_vio_servers)

    # Save off the network associations first so we can recreate any that
    # need to be later on.
    net_assns = _build_net_assn_dict(
        db.network_association_find_all(context,
                                        db_host_dom.host_name,
                                        db_session))

    for db_vios in db_vios_to_del:
        _remove_vios(db_vios, db_host_dom, context, db_session)
    for server_vios in srv_vios_to_modify:
        _reconcile_vios(_find_vios(db_vio_servers, server_vios.lpar_id),
                        server_vios, context, db_session, dom_factory)
    for server_vios in srv_vios_to_add:
        _add_vios(server_vios, db_host_dom, context, db_session, dom_factory)

    msg = (ras.vif_get_msg('info', 'RECONCILE_HOST_END') %
           {'host': server_dom.host_name})
    ras.trace(LOG, __name__, ras.TRACE_DEBUG, msg)

    # Cleanup NetworkAssociations in case any VIOSes went away or came back.
    _cleanup_network_associations(db_host_dom, net_assns, context, db_session)


def _build_net_assn_dict(net_assns):
    """
    This method takes in the list of NetworkAssociations and builds a
    dictionary where the key is the NetworkAssociation and the value is the
    lpar_id of the VIOS containing the NetworkAssociation's SEA.  The sole
    purpose of this is so that when a NetworkAssociation is moved, we can
    properly log the former VIOS lpar_id it was on.  If a VIOS is deleted
    during reconciliation, the the net_assn.sea.vio_server is not available,
    and thus net_assn.sea.vio_server.lpar_id is obviously not available.

    :param net_assns: List of NetworkAssociations from the db
    :return: Dictionary mapping NetworkAssociations to lpar_ids.
    """
    LOG.debug('Enter _build_net_assn_dict')

    if not net_assns:
        LOG.debug('Exit _build_net_assn_dict, no NetworkAssociations')
        return {}

    ret_dict = {}
    for net_assn in net_assns:
        # We only care about NetworkAssociations that have an SEA
        if net_assn.sea is not None:
            ret_dict[net_assn] = net_assn.sea.vio_server.lpar_id

    LOG.debug('Exit _build_net_assn_dict with dict: %s' %
              pprint.pformat(ret_dict))
    return ret_dict


def _make_system_unavailable(db_vioses, context, db_session):
    """
    Will flip a host's VioServers/SEAs to unavailable.

    :param db_vioses: The database VioServer objects
    :param context: The context of the object
    :param db_session: The session to do the work in.
    """
    for db_vios in db_vioses:
        db_vios.rmc_state = 'inactive'
        for sea in db_vios.sea_list:
            sea.state = 'inactive'
        _cascade_save(db_vios, context, db_session)


def _find_vios(vios_list, lpar_id):
    """
    Returns the corresponding VioServer in the vios_list for the lpar_id

    :param vios_list: The list of VioServer objects.
    :param lpar_id: The lpar_id to find.

    :return: The VioServer from the vios_list that matches that lpar_id.  If
             one does not exist, None is returned.
    """
    for vio_server in vios_list:
        if vio_server.lpar_id == lpar_id:
            return vio_server
    return None


def _reconcile_vios(db_vios, server_vios, context, db_session, dom_factory):
    """
    Will reconcile the database VioServer object to match that of the Server
    VioServer object.

    :param db_vios: The VioServer object in the database.
    :param server_vios: The VioServer object that represents the current state
                        on the server system.
    :param context: The context for the operations
    :param db_session: The database session to use for this transaction
    :param dom_factory: The factory to use to convert the DOM objects.
    """

    msg = (ras.vif_get_msg('info', 'RECONCILE_VIOS_START') %
           {'host': server_vios.get_host_name(),
            'vios': server_vios.lpar_id})
    ras.trace(LOG, __name__, ras.TRACE_DEBUG, msg)

    db_veas = db_vios.get_all_virtual_ethernet_adapters()
    db_seas = db_vios.get_shared_ethernet_adapters()

    srv_veas = server_vios.get_all_virtual_ethernet_adapters()
    srv_seas = server_vios.get_shared_ethernet_adapters()

    # Have to 'load' these objects due to an issue found in 3749.  By calling
    # these, the attributes are loaded into memory.  If we don't do this
    # then when the VEAs change, they may remove themselves from the SEA.
    for db_sea in db_seas:
        db_sea.primary_vea
        db_sea.control_channel
        db_sea.additional_veas

    # Save the rmc_state of the vios.  This may or may not be different than
    # what's already in the db.
    db_vios.rmc_state = server_vios.rmc_state
    ras.trace(LOG, __name__, ras.TRACE_DEBUG,
              'RMC state for lpar %d (%s): %s, ' % (server_vios.lpar_id,
                                                    server_vios.lpar_name,
                                                    server_vios.rmc_state))

    # If the RMC connection is down, then we'll just update the VIOS's
    # rmc_state in the db, no other reconciliation will occur.  Since rmc_state
    # is taken into account when checking whether an adapter is available, we
    # don't need to update all the adapters.
    if server_vios.rmc_state.lower() == 'active':
        # RMC is up, proceed as normal.

        # The first step to reconciliation is the VEAs, as they are input into
        # the SEAs.
        #
        # We start with the idea that all of the VEAs on the VIOS should be
        # removed.  Then the code will parse out from that delete list and
        # create lists of adapters to add and merge.
        db_veas_to_del = dom.shallow_copy_as_ordinary_list(db_veas)
        srv_veas_to_add = []
        srv_veas_to_modify = []

        for srv_vea in srv_veas:
            db_vea = _find_adapter(db_veas, srv_vea.name)
            if db_vea:
                srv_veas_to_modify.append(srv_vea)
                db_veas_to_del.remove(db_vea)
            else:
                srv_veas_to_add.append(srv_vea)

        # We have sorted how each object should be handled.  Handle these
        # before moving on to the SEAs.
        for db_vea in db_veas_to_del:
            _remove_vea(db_vea, db_vios, context, db_session)
        for server_vea in srv_veas_to_modify:
            _reconcile_vea(_find_adapter(db_veas, server_vea.name),
                           server_vea, db_vios, context, db_session)
        for server_vea in srv_veas_to_add:
            _add_vea(server_vea, db_vios, context, db_session, dom_factory)

        # At this point, we have reconciled the VirtualEthernetAdapters.  Next
        # up is the SharedEthernetAdapters, that contain the
        # VirtualEthernetAdapters.
        db_seas_to_del = dom.shallow_copy_as_ordinary_list(db_seas)
        srv_seas_to_add = []
        srv_seas_to_modify = []

        for srv_sea in srv_seas:
            db_sea = _find_adapter(db_seas, srv_sea.name)
            if db_sea:
                srv_seas_to_modify.append(srv_sea)
                db_seas_to_del.remove(db_sea)
            else:
                srv_seas_to_add.append(srv_sea)

        # Now that we have sorted how each object should be handle (Create,
        # Update, or Delete), execute those actions...
        for db_sea in db_seas_to_del:
            _remove_sea(db_sea, db_vios, context, db_session)
        for server_sea in srv_seas_to_modify:
            _reconcile_sea(_find_adapter(db_seas, server_sea.name),
                           server_sea, db_vios, context, db_session)
        for server_sea in srv_seas_to_add:
            _add_sea(server_sea, db_vios, context, db_session, dom_factory)
    else:
        ras.trace(LOG, __name__, ras.TRACE_WARNING,
                  _('RMC state for lpar %(lpar)d (%(lpar_name)s): %(state)s' %
                    {'lpar': server_vios.lpar_id,
                     'lpar_name': server_vios.lpar_name,
                     'state': server_vios.rmc_state}))

    # Whether reconciled or just set rmc_state, we need to save back to the DB.
    _cascade_save(db_vios, context, db_session)

    msg = (ras.vif_get_msg('info', 'RECONCILE_VIOS_END') %
           {'host': server_vios.get_host_name(),
            'vios': server_vios.lpar_id})
    ras.trace(LOG, __name__, ras.TRACE_DEBUG, msg)


def _find_adapter(adpt_list, adpt_name):
    """
    Will return the adapter in the adpt_list for the adpt_name.  If it does
    not exist, None will be returned.

    :param adpt_list: The list of adapters to search through.
    :param adpt_name: The name of the adapter to find in the list.

    :return: The adapter that corresponds to the adapter name.  If one is not
             in the list, None is returned.
    """
    for adpt in adpt_list:
        if adpt.name == adpt_name:
            return adpt
    return None


def _find_adapter_list(adpt_list, search_adpt_list):
    """
    Will return a list of Adapter objects that are a subset of the adapters
    in the adpt_list.  The subset is the list of Adapters that match a name
    from within the search_adpt_list

    :param adpt_list: The list of adapters to search through.
    :param search_adpt_list: The list of adapters to add to the return list.

    :return: The list of adapters that correspond to the adapter names
    """
    ret_list = []
    for search_adpt in search_adpt_list:
        new_adpt = _find_adapter(adpt_list, search_adpt.name)
        if new_adpt:
            ret_list.append(new_adpt)
    return ret_list


def _add_vea(server_vea, db_vio_server, context, db_session, dom_factory):
    """
    Adds the VirtualEthernetAdapter object to the database.

    :param server_vea: The server side VirtualEthernetAdapter object to merge
                       into the database.
    :param db_vio_server: The VioServer that represents the corresponding
                          VioServer for this object.
    :param context: The context for the operations
    :param db_session: The database session to use for this transaction
    :param dom_factory: The factory to use to convert the DOM objects.
    """
    msg = (ras.vif_get_msg('info', 'RECONCILE_VEA_ADD_START') %
           {'host': db_vio_server.get_host_name(),
            'vios': db_vio_server.lpar_id,
            'adpt': server_vea.name})
    ras.trace(LOG, __name__, ras.TRACE_DEBUG, msg)

    # Copy into a database backed DOM, because the DOM doesn't yet support
    # a single object to represent both DB and non-DB backed elements.
    vea = dom_factory.create_vea(server_vea.name,
                                 db_vio_server,
                                 server_vea.slot,
                                 server_vea.pvid,
                                 server_vea.is_trunk,
                                 server_vea.trunk_pri,
                                 server_vea.state,
                                 server_vea.ieee_eth,
                                 server_vea.vswitch_name,
                                 server_vea.addl_vlan_ids)
    vea.save(context, db_session)

    msg = (ras.vif_get_msg('info', 'RECONCILE_VEA_ADD_END') %
           {'host': db_vio_server.get_host_name(),
            'vios': db_vio_server.lpar_id,
            'adpt': server_vea.name})
    ras.trace(LOG, __name__, ras.TRACE_DEBUG, msg)


def _remove_vea(db_vea, db_vio_server, context, db_session):
    """
    Will remove a VirtualEthernetAdapter from the database.

    :param db_vea: The DOM object that represents the VirtualEthernetAdapter
                   to remove from the database.
    :param db_vio_server: The VioServer that represents the corresponding
                          VioServer for this object.
    :param context: The context for the operations
    :param db_session: The database session to use for this transaction
    """
    msg = (ras.vif_get_msg('info', 'RECONCILE_VEA_DEL_START') %
           {'host': db_vio_server.get_host_name(),
            'vios': db_vio_server.lpar_id,
            'adpt': db_vea.name})
    ras.trace(LOG, __name__, ras.TRACE_DEBUG, msg)

    # Need to remove the VEA from the parent SEA.
    owning_sea = db_vio_server.get_sea_for_vea(db_vea)
    if owning_sea is not None:
        # If it is the primary VEA...just ignore...that means the SEA is also
        # being deleted...
        if owning_sea.primary_vea == db_vea:
            pass
        elif owning_sea.control_channel == db_vea:
            owning_sea.control_channel = None
        elif db_vea in owning_sea.additional_veas:
            owning_sea.additional_veas.remove(db_vea)

    db_vea.delete(context, db_session)
    db_vio_server.remove_adapter(db_vea)

    msg = (ras.vif_get_msg('info', 'RECONCILE_VEA_DEL_END') %
           {'host': db_vio_server.get_host_name(),
            'vios': db_vio_server.lpar_id,
            'adpt': db_vea.name})
    ras.trace(LOG, __name__, ras.TRACE_DEBUG, msg)


def _reconcile_vea(db_vea, server_vea, db_vio_server, context, db_session):
    """
    Will reconcile the database VirtualEthernetAdapter to match that of the
    Server side VirtualEthernetAdapter.

    :param db_vea: The DOM object that represents the VirtualEthernetAdapter to
                   update.
    :param server_vea: The DOM object that represents the master data for the
                       database.
    :param db_vio_server: The VioServer that represents the corresponding
                          VioServer for this object.
    :param context: The context for the operations
    :param db_session: The database session to use for this transaction
    """
    msg = (ras.vif_get_msg('info', 'RECONCILE_VEA_START') %
           {'host': db_vio_server.get_host_name(),
            'vios': db_vio_server.lpar_id,
            'adpt': server_vea.name})
    ras.trace(LOG, __name__, ras.TRACE_DEBUG, msg)

    db_vea.name = server_vea.name
    db_vea.pvid = server_vea.pvid
    db_vea.vio_server = db_vio_server
    db_vea.slot = server_vea.slot
    db_vea.is_trunk = server_vea.is_trunk
    db_vea.state = server_vea.state
    db_vea.trunk_pri = server_vea.trunk_pri
    db_vea.ieee_eth = server_vea.ieee_eth
    db_vea.vswitch_name = server_vea.vswitch_name
    db_vea.addl_vlan_ids = server_vea.addl_vlan_ids

    msg = (ras.vif_get_msg('info', 'RECONCILE_VEA_END') %
           {'host': db_vio_server.get_host_name(),
            'vios': db_vio_server.lpar_id,
            'adpt': server_vea.name})
    ras.trace(LOG, __name__, ras.TRACE_DEBUG, msg)


def _add_sea(server_sea, db_vio_server, context, db_session, dom_factory):
    """
    Adds the SharedEthernetAdapter object to the database.

    :param server_sea: The server side SharedEthernetAdapter object to merge
                       into the database.
    :param db_vio_server: The VioServer that represents the corresponding
                          VioServer for this object.
    :param context: The context for the operations
    :param db_session: The database session to use for this transaction
    :param dom_factory: The factory to use to convert the DOM objects.
    """
    msg = (ras.vif_get_msg('info', 'RECONCILE_SEA_ADD_START') %
           {'host': db_vio_server.get_host_name(),
            'vios': db_vio_server.lpar_id,
            'adpt': server_sea.name})
    ras.trace(LOG, __name__, ras.TRACE_DEBUG, msg)

    # Swap in the Server (non-DB backed) VEAs with the real DB backed VEAs
    db_veas = db_vio_server.get_all_virtual_ethernet_adapters()
    db_pvea = _find_adapter(db_veas, server_sea.primary_vea.name)
    db_ctl = None
    if server_sea.control_channel:
        db_ctl = _find_adapter(db_veas, server_sea.control_channel.name)

    db_addl_veas = []
    if server_sea.additional_veas:
        db_addl_veas = _find_adapter_list(db_veas,
                                          server_sea.additional_veas)

    dom_factory.create_sea(server_sea.name,
                           db_vio_server,
                           server_sea.slot,
                           server_sea.state,
                           db_pvea,
                           db_ctl,
                           db_addl_veas)

    msg = (ras.vif_get_msg('info', 'RECONCILE_SEA_ADD_END') %
           {'host': db_vio_server.get_host_name(),
            'vios': db_vio_server.lpar_id,
            'adpt': server_sea.name})
    ras.trace(LOG, __name__, ras.TRACE_DEBUG, msg)


def _remove_sea(db_sea, db_vio_server, context, db_session):
    """
    Will remove a SharedEthernetAdapter from the database.

    :param db_sea: The DOM object that represents the SharedEthernetAdapter
                   to remove from the database.
    :param db_vio_server: The VioServer that represents the corresponding
                          VioServer for this object.
    :param context: The context for the operations
    :param db_session: The database session to use for this transaction
    """
    msg = (ras.vif_get_msg('info', 'RECONCILE_SEA_DEL_START') %
           {'host': db_vio_server.get_host_name(),
            'vios': db_vio_server.lpar_id,
            'adpt': db_sea.name})
    ras.trace(LOG, __name__, ras.TRACE_DEBUG, msg)

    db_sea.delete(context, db_session)
    db_vio_server.remove_adapter(db_sea)

    msg = (ras.vif_get_msg('info', 'RECONCILE_SEA_DEL_END') %
           {'host': db_vio_server.get_host_name(),
            'vios': db_vio_server.lpar_id,
            'adpt': db_sea.name})
    ras.trace(LOG, __name__, ras.TRACE_DEBUG, msg)


def _reconcile_sea(db_sea, server_sea, db_vio_server, context, db_session):
    """
    Will reconcile the database SharedEthernetAdapter to match that of the
    Server side SharedEthernetAdapter.

    :param db_sea: The DOM object that represents the SharedEthernetAdapter to
                   update.
    :param server_sea: The DOM object that represents the master data for the
                       database.
    :param db_vio_server: The VioServer that represents the corresponding
                          VioServer for this object.
    :param context: The context for the operations
    :param db_session: The database session to use for this transaction
    """
    msg = (ras.vif_get_msg('info', 'RECONCILE_SEA_START') %
           {'host': db_vio_server.get_host_name(),
            'vios': db_vio_server.lpar_id,
            'adpt': db_sea.name})
    ras.trace(LOG, __name__, ras.TRACE_DEBUG, msg)

    db_sea.name = server_sea.name
    db_sea.vio_server = db_vio_server
    db_sea.slot = server_sea.slot
    db_sea.state = server_sea.state

    current_db_veas = db_vio_server.get_all_virtual_ethernet_adapters()

    db_sea.primary_vea = _find_adapter(current_db_veas,
                                       server_sea.primary_vea.name)

    if server_sea.control_channel:
        db_sea.control_channel = _find_adapter(current_db_veas,
                                               server_sea.control_channel.name)
    else:
        db_sea.control_channel = None

    db_sea.additional_veas = _find_adapter_list(current_db_veas,
                                                server_sea.additional_veas)

    msg = (ras.vif_get_msg('info', 'RECONCILE_SEA_END') %
           {'host': db_vio_server.get_host_name(),
            'vios': db_vio_server.lpar_id,
            'adpt': db_sea.name})
    ras.trace(LOG, __name__, ras.TRACE_DEBUG, msg)


def _add_vios(server_vios, host, context, db_session, dom_factory):
    """
    Adds the VioServer object to the database.

    :param server_vios: The server side VioServer object to merge into the
                        database.
    :param host: The host for the VioServer
    :param context: The context for the operations
    :param db_session: The database session to use for this transaction
    :param dom_factory: The factory to use to convert the DOM objects.
    """
    msg = (ras.vif_get_msg('info', 'RECONCILE_VIOS_ADD_START') %
           {'host': server_vios.get_host_name(),
            'vios': server_vios.lpar_id})
    ras.trace(LOG, __name__, ras.TRACE_DEBUG, msg)

    # Copy into a database backed DOM, because the DOM doesn't yet support
    # a single object to represent both DB and non-DB backed elements.
    db_vio = dom_factory.create_vios(lpar_name=server_vios.lpar_name,
                                     lpar_id=server_vios.lpar_id,
                                     host_name=server_vios.get_host_name(),
                                     state=server_vios.state,
                                     rmc_state=server_vios.rmc_state)

    # The VioServer passed in is a no-DB VioServer object.  First must convert
    # that over to the proper DB backed object and save it.
    db_vio.save(context, db_session)
    host.vio_servers.append(db_vio)

    # Cascade down into the adapters.
    for vea in server_vios.get_all_virtual_ethernet_adapters():
        _add_vea(vea, db_vio, context, db_session, dom_factory)
    for sea in server_vios.get_shared_ethernet_adapters():
        _add_sea(sea, db_vio, context, db_session, dom_factory)

    _cascade_save(db_vio, context, db_session)

    msg = (ras.vif_get_msg('info', 'RECONCILE_VIOS_ADD_END') %
           {'host': server_vios.get_host_name(),
            'vios': server_vios.lpar_id})
    ras.trace(LOG, __name__, ras.TRACE_DEBUG, msg)


def _remove_vios(db_vios, host, context, db_session):
    """
    Will remove a VioServer from the database.

    :param db_vios: The DOM object that represents the VioServer to remove from
                    the database.
    :param host: The host for the VioServer
    :param context: The context for the operations
    :param db_session: The database session to use for this transaction
    """
    msg = (ras.vif_get_msg('info', 'RECONCILE_VIOS_DEL_START') %
           {'host': db_vios.get_host_name(),
            'vios': db_vios.lpar_id})
    ras.trace(LOG, __name__, ras.TRACE_DEBUG, msg)

    db_vios.delete(context, db_session)
    host.vio_servers.remove(db_vios)

    msg = (ras.vif_get_msg('info', 'RECONCILE_VIOS_DEL_END') %
           {'host': db_vios.get_host_name(),
            'vios': db_vios.lpar_id})
    ras.trace(LOG, __name__, ras.TRACE_DEBUG, msg)


def _cascade_save(db_vios, context, db_session):
    """
    The save semantics throw errors (until Issue 3749 is resolved) that causes
    the objects to require a specific order when saved.  This method handles
    the saving of the objects.
    """
    msg = (ras.vif_get_msg('info', 'RECONCILE_SAVE_START') %
           {'host': db_vios.get_host_name(),
            'vios': db_vios.lpar_id})
    ras.trace(LOG, __name__, ras.TRACE_DEBUG, msg)

    # Loop through the VEAs...
    for vea in db_vios.get_all_virtual_ethernet_adapters():
        db_session.add(vea)

    # Loop through the SEAs
    for sea in db_vios.get_shared_ethernet_adapters():
        db_session.add(sea)
        sea.primary_vea  # If not called...may set pvea to None?
        sea.control_channel
        sea.additional_veas

    # Save the VioServer
    # Removing 'add': 5278 fix causes InvalidRequestError--SEA already deleted
    #db_session.add(db_vios)
    db_session.flush()

    msg = (ras.vif_get_msg('info', 'RECONCILE_SAVE_END') %
           {'host': db_vios.get_host_name(),
            'vios': db_vios.lpar_id})
    ras.trace(LOG, __name__, ras.TRACE_DEBUG, msg)
