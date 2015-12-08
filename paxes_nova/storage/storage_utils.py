# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# =================================================================
# =================================================================

from nova.openstack.common import jsonutils, log as logging
from powervc_nova import _
from oslo.config import cfg
from webob import exc
import ConfigParser
import time
import six

from nova import db as nova_db
from powervc_nova.storage import exception as stgex
import powervc_nova.objects.storage.dom as storage_dom
from powervc_nova.virt.ibmpowervm.common.constants \
    import CONNECTION_TYPE_NPIV, CONNECTION_TYPE_SSP


LOG = logging.getLogger(__name__)
DISPLAY_NAME = 'display_name'

# Configuration options for host type and manager type
CONF = cfg.CONF
CONF.import_opt('powervm_mgr_type',
                'powervc_nova.virt.ibmpowervm.ivm')


#################################################################
# NOTE: Some of the utility method in this class may make more  #
#       sense to move to an object DOM and some to a general    #
#       ibmpowervm utility as opposed to storage.  (TODO:)      #
#################################################################
def query_scgs(context, sort_type=None, enabled_only=False,
               cluster_provider=None, give_objects=False,
               include_ports=False, deploy_ready_only=False,
               quickdetail=False):
    """
    This method interfaces with a database helper to get a list of
    dictionaries representing the SCGs. It is a convenience method
    that will establish and use a db transaction and will return
    dictionaries or db objects based on give_objects boolean.
    :returns: A list of SCGs. The looked up list of resources
              are converted to their dictionary form by this helper.
    """
    ticks = time.time()
    scg_factory = storage_dom.StorageConnectivityGroupFactory.get_factory()
    filters = {}
    if enabled_only is True:
        filters['enabled'] = True
    if cluster_provider is not None:
        # Check for special value that means we want to filter on SCGs with
        # no cluster association.
        if cluster_provider == "|none|":
            filters['cluster_provider_name'] = None
        else:
            filters['cluster_provider_name'] = cluster_provider

    # Construct and use a transaction.
    txn = scg_factory.get_resource_transaction()
    filtered = []
    with txn.begin(subtransactions=True):
        scgs = scg_factory.find_all_scgs(context, filters=filters,
                                         sort_type=sort_type,
                                         transaction=txn)
        # It is important that the to_dict() method is called within
        # the context of an active transaction so that the vios membership
        # is available on the DOM and can be used to build the host_list
        # nested dictionary structure.
        if include_ports or deploy_ready_only:
            filtered = [scg.to_dict_with_ports(context,
                                               include_ports=include_ports,
                                               transaction=txn)
                        for scg in scgs]
            if deploy_ready_only:
                filtered = [scg for scg in filtered
                            if scg['vios_ready_count'] > 0]
        else:
            filtered = [scg.to_dict(context=context,
                                    skip_extended_lookups=quickdetail,
                                    transaction=txn)
                        for scg in scgs]
    # Now return the compiled list.
    # If give_objects is True, then return DOM objects rather than
    # converted dictionaries. The dict representation can still be
    # accessed from the object.
    if give_objects:
        filt_set = set([x['id'] for x in filtered])
        scgs = [scg for scg in scgs if scg.id in filt_set]
        filtered = scgs

    LOG.debug("query_scgs timing=%.4f." % (time.time() - ticks))
    return filtered


def get_scg_by_id(context, scg_id):
    """
    Look up storage connectivity group by id.
    :param context: A context object that is used to authorize
            any DB access.
    :param scg_id: The 'id' of the storage connectivity group to lookup.
    :returns: The SCG object looked up. Otherwise an error is raised.
    """
    # DB facing logic comes next.
    # Now construct and use a transaction.
    scg_factory = storage_dom.StorageConnectivityGroupFactory.get_factory()
    txn = scg_factory.get_resource_transaction()
    with txn.begin(subtransactions=True):
        scg = None
        try:
            scg = scg_factory.find_scg_by_id(context, scg_id, transaction=txn)
        except Exception as e:
            LOG.exception(e)
            LOG.error(_("Exception trying to find a storage connectivity group"
                      " using resource id = %s") % scg_id)

        if scg is None:
            msg = stgex.IBMPowerVCSCGNotFound.msg_fmt % locals()
            ex = stgex.IBMPowerVCSCGNotFound(msg)
            LOG.exception(ex)
            raise ex
        scg_dict = scg.to_dict(context=context, transaction=txn)
        # Since to_dict() is called within the transaction,
        # the hydrated dictionary will be cached on the scg object
        # and future references to it will have the host_list
        # nested dictionary data.
        LOG.debug("Looked up SCG: %s" % scg_dict)
    return scg


def get_fcport_by_id(context, transaction, fc_id):
    """
    Look up fc port by id.
    :param context: A context object that is used to authorize
            any DB access.
    :param fc_id: The 'udid' of the fc port to lookup.
    :returns: The fc port object looked up. Otherwise an error is raised.
    """

    # Regular DB facing logic comes next.
    # Now construct and use a transaction.
    LOG.debug("Look for FC port with id: %s" % fc_id)
    fcport_factory = storage_dom.FcPortFactory.get_factory()
    fcport = fcport_factory.find_fcport_by_id(context, fc_id,
                                              transaction=transaction)
    if fcport is None:
        LOG.error(_("Did not find an FC port with id: %s" % fc_id))
        msg = stgex.IBMPowerVCFCPortNotFound.msg_fmt % locals()
        ex = stgex.IBMPowerVCFCPortNotFound(msg)
        LOG.exception(ex)
        raise ex
    return fcport


def get_volumes_from_image(image, continue_on_error=False, msg_dict=None):
    """
    Retrieve the source volume id(s) and volume_id(s) from an image.
    This method will not lookup or validate the volume id on cinder.
    :param image: the image dictionary structure.
    :param continue_on_error: if False, errors are raised instead of
            continuing, e.g. to next volume in image
    :param msg_dict: A dictionary with a 'messages' key to add info/warning
            messages to
    :returns: A list of dictionaries representing both the source_volid and
              volume_id entries of the image:
              [{'id': <id>, 'is_boot': True/False,
                'image_name': <name>, 'image_id': <image_id>},...]
    """
    ivols = []  # image volumes to return
    boot_vols = []
    attach_vols = []
    img_id = image['id']
    img_name = image['name']
    if ("properties" not in image.keys() or
            "volume_mapping" not in image["properties"].keys()):
        error = _("The image with id '%(img_id)s' is not associated with a "
                  "cinder volume. Ensure the volume has been managed into "
                  "PowerVC and associated with the image.") % locals()
        msg = stgex.IBMPowerVCStorageError.msg_fmt % locals()
        LOG.error(msg)
        if msg_dict:
            msg_dict['messages'].append(msg)
        if continue_on_error:
            return ivols
        else:
            raise stgex.IBMPowerVCStorageError(msg)

    vol_map_str = image["properties"]["volume_mapping"]
    vol_map = jsonutils.loads(vol_map_str)
    LOG.debug("Image id '%(img_id)s' vol_map = %(vol_map)s" % locals())

    # Each volume has either a source_volid or a volume_id.  Collect them all.
    for vol in vol_map:
        if "source_volid" in vol.keys():
            boot_vols.append({'id': vol["source_volid"], 'is_boot': True,
                              'image_name': img_name, 'image_id': img_id})
        elif "volume_id" in vol.keys():
            v_id = vol["volume_id"]
            attach_vols.append({'id': vol["volume_id"], 'is_boot': False,
                                'image_name': img_name, 'image_id': img_id})
        else:
            msg = _("Not a supported volume entry in the image: %s" % vol)
            if msg_dict:
                msg_dict['messages'].append(msg)
            continue

    # Add source_vols first to return list, because the first volume is may
    # have primary significance for determining the SCG or otherwise.
    ivols.extend(boot_vols)
    ivols.extend(attach_vols)
    LOG.debug("Volumes found in image '%s': %s" % (image['name'], ivols))

    # Ensure there is exactly one 'source' volume which is used as
    # the boot volume.  This is a paxes restriction.
    if len(boot_vols) > 1:
        error = _("The image with ID '%(img_id)s' contains more than one "
                  "boot volume. Only one boot volume (source_volid) may "
                  "be associated with the image.") % locals()
        err_msg = stgex.IBMPowerVCStorageError.msg_fmt % locals()
        LOG.error(err_msg)
        if msg_dict:
            msg_dict['messages'].append(err_msg)
        if not continue_on_error:
            raise stgex.IBMPowerVCStorageError(err_msg)
    elif not boot_vols:
        error = _("The image with ID '%(img_id)s' does not contain any "
                  "bootable volumes. Ensure a boot volume has been managed "
                  "into PowerVC and associated with the image.") % locals()
        err_msg = stgex.IBMPowerVCStorageError.msg_fmt % locals()
        LOG.error(err_msg)
        if msg_dict:
            msg_dict['messages'].append(err_msg)
        if not continue_on_error:
            raise stgex.IBMPowerVCStorageError(err_msg)

    return ivols


def scg_insert(context, scg_dict, vios_ids):
    """
    Inserts an SCG into the database given a dictionary of primitive
    properties plus a list of vios ids.
    :param context: The authorization context
    :param scg_dict: The dictionary of key/value pairs to construct the
                     SCG with. The input keys and values must conform
                     to the rules of the SCG APIs, which matches the
                     underlying database SQLAlchemy model.
    :param vios_ids: A list of VIOS resource "id"s that can be looked up
                     and associated with this SCG.
    :returns: The inserted (constructed) SCG DOM.
    """
    LOG.debug("Input dictionary: \n%s" % scg_dict)
    if scg_dict['fc_storage_access'] and 'priority_external' not in scg_dict:
        scg_dict['priority_external'] = 2  # default priority
    if 'cluster_provider_name' in scg_dict and \
            'priority_cluster' not in scg_dict:
        scg_dict['priority_cluster'] = 2  # default priority
    # Now construct and use a transaction
    scg_factory = storage_dom.StorageConnectivityGroupFactory.get_factory()
    txn = scg_factory.get_resource_transaction()
    with txn.begin():
        # Construct the base SCG
        display_name = scg_dict.pop('display_name', 'SCG_DEFAULT_DISPLAY_NAME')
        scg = scg_factory.construct_scg(context, display_name,
                                        auto_create=True, transaction=txn,
                                        **scg_dict)
        LOG.debug("Constructed transaction '%s' and new scg with ID '%s' "
                  "and input properties: %s."
                  % (str(txn), scg.id, scg_dict))
        # replace the SCG vios list in context of a transaction
        scg.replace_vios_list(context, txn, vios_ids)
        # Now persist the SCG object
        scg.save(context=context, transaction=txn)
    # end transaction
    return scg


def http_error_if_ivm():
    host_types = get_supported_host_types()
    mgr_type = CONF.powervm_mgr_type.lower()
    if mgr_type == 'ivm' and 'kvm' not in host_types:
        msg = _("Storage connectivity group based requests are not "
                "supported for PowerVC Express installations (IVM mode).")
        raise exc.HTTPBadRequest(explanation=six.text_type(msg))


def is_kvm_supported():
    """ Return whether KVM is a supported host type. Currently, the general
        PowerVC code base assumes that support is for all KVM or all PowerVM.
    """
    host_types = get_supported_host_types()
    LOG.debug("host_types=%s" % host_types)
    if 'kvm' in host_types:
        return True
    return False


def get_supported_host_types():
    """Helper method to determine what types of Hosts are supported"""
    #For performance we will only retrieve the conf value only once
    if not hasattr(get_supported_host_types, '_host_types'):
        host_types = None
        parser = ConfigParser.RawConfigParser()
        parser.read('/etc/nova/nova.conf')
        #Use the PowerVM-specific property for now
        if parser.has_option('DEFAULT', 'supported_host_types'):
            host_types = parser.get('DEFAULT', 'supported_host_types')
        #Default to powervm for now if the property is not set
        get_supported_host_types._host_types =\
            [host_types] if host_types else ['powervm']
    return get_supported_host_types._host_types


def set_scg_lowest_priority(scg_id, context):
    """
    Fixed for SSPs and deprecated.
    Set the priority value for a particular SCG to be lower than
    any other scg that has priority in external or cluster priority list.
    """
    LOG.debug("Setting scg_id=%s"
              " to lowest priority list priority." % (scg_id))
    scg = get_scg_by_id(context, scg_id)
    scg_dict = scg.to_dict()
    # Construct and use a transaction.
    txn = storage_dom.StorageConnectivityGroupFactory.get_factory().\
        get_resource_transaction()
    with txn.begin():
        if ('fc_storage_access' in scg_dict and
                scg_dict['fc_storage_access']):
            LOG.debug("SCG scg_id=%s should be in external"
                      " priority list so set SCG to lowest in that list."
                      % (scg_id))
            scg_list = query_scgs(context, "external", True,
                                  None, True)
            if scg_list and scg_list[-1].priority_external is not None:
                # Use LOWEST priority in list from last entry
                scg.priority_external = scg_list[-1].priority_external + 1
            else:
                # no list so set priority to 1
                LOG.debug("SCG scg_id=%s only scg in external"
                          " priority list so set SCG to 1." % (scg_id))
                scg.priority_external = 1

        # note in this context "VIOS cluster" is a shared storage pool
        # or a n "SSP"
        if ('vios_cluster' in scg_dict and
                scg_dict['vios_cluster']['provider_name'] is not None):
            provider = scg_dict['vios_cluster']['provider_name']
            LOG.debug("SCG scg_id=%s should be in cluster"
                      " priority list so set SCG to lowest in that list."
                      % (scg_id))
            scg_list = query_scgs(context, "cluster", True,
                                  provider, True)
            if scg_list and scg_list[-1].priority_cluster is not None:
                # use LOWEST priority in list from last entry
                scg.priority_cluster = scg_list[-1].priority_cluster + 1
            else:
                LOG.debug("SCG scg_id=%s only scg in cluster"
                          " priority list so set SCG to 1." % (scg_id))
                # no list so set priority to 1
                scg.priority_cluster = 1

        # Queue up the commit within the transaction
        scg.save(context=context, transaction=txn)


def get_db_wwpns_to_fabric_map(context, wwpns):
    """
    Given a list of physical WWPNs, return a dictionary mapping of
    wwpn to the fabric it is associated with by querying the nova
    database. The fabric is a metadata setting on an FCPort.
    Returns "None" if not associated with "A" or "B". Returns
    python None if the port cannot be found.
    This method assumes that WWPNs are unique in the managed environment.
    """
    fcpport_factory = storage_dom.FcPortFactory.get_factory()
    txn = fcpport_factory.get_resource_transaction()
    wwpn_to_fabric = {}
    with txn.begin():
        # For now we just loop over the wwpns and look up each one.
        # There is a way to query for the list FC ports by wwpn list, but
        # as that is the province of pvc_db.sqlalchemy.api, it is left
        # as a future TODO:
        # http://stackoverflow.com/questions/8603088/sqlalchemy-in-clause
        for wwpn in wwpns:
            if not wwpn:
                continue  # skip if None
            LOG.debug("Look for FC port with wwpn: %s" % wwpn)
            fcs = fcpport_factory.find_all_fcports(
                context, filters={'wwpn': wwpn}, transaction=txn)

            wwpn_to_fabric[wwpn] = fcs[0].fabric if fcs else None
    # end transaction
    return wwpn_to_fabric


def check_if_hmc_default_migrate_maps_ok(context, dest_conn):
    """
    Check if letting the HMC choose the LPAR's connectivity mappings
    (VIOSes & ports) should be OK for a migration operation. If not,
    then PowerVC needs to compute the migration mappings to send to the
    HMC migrate function as overrides. PowerVC must choose the
    mappings if the SCG specifies customized attributes or a subset
    of VIOSes members that the HMC would not be aware of.
    :param context:   The authorization context.
    :param dest_conn: The connectivity structure for a migration destination
                      host computed by the get_connectivity_info() internal
                      API.
    :returns: A dictionary that specifies whether PowerVC needs to compute
              the storage mappings by connectivity type. e.g.
                  {'npiv': True, 'ibm_ssp': False}

    Details:
    Since SSPs are supported, it is not sufficient to check whether the
    SCG for the passed connectivity info is auto-defined. The SCG could
    be auto-defined for a particular SSP and include a subset of VIOSes
    on the target host. In that case, the HMC could migrate the VM to that
    host and use ports owned by a VIOS not in the SCG.
    Instead, the prior to-do comment in this method has been implemented.

        npiv case:
        1. Is the SCG's port tag undefined?
        2. Does the dest_conn structure contain a maximal set of VIOSes
           with npiv connectivity for the target host?
        3. For each vios, is every owned FC port enabled?
        4. For each vios, is every owned FC port have no fabric set?

        ssp case:
            1. Does the dest_conn structure contain a maximal set of VIOSes
               with ssp connectivity for the target host?

      If any of these checks are violated, then we will assume that PowerVC
      can do a better/more accurate job of mapping the storage connectivity
      on the destination host, and so False will be set for the
      connectivity type, else True.
    """

    # Use a transaction (cf. db session) to access VIOS FC port assoc's later
    txn = storage_dom.StorageConnectivityGroupFactory.get_factory().\
        get_resource_transaction()
    with txn.begin():
        # First get the maximal connectivity dictionary. This will
        # be the basis for the values we return. But in the npiv case,
        # extra checks are needed.
        conn_eval = eval_maximal_connectivity(context, dest_conn, txn)

        if not conn_eval[CONNECTION_TYPE_NPIV]:
            # The vios membership in the connectivity info is not maximal
            # so we can return right now.
            LOG.debug("Non maximal VIOS connectivity for npiv.")

        elif dest_conn['scg'].port_tag:
            # We do the port tag check as a simpler/more conservative check
            # rather than seeing if every port associated with the
            # db_vioses are in the conn_info structure.
            LOG.debug("SCG has a port tag. Set 'false'.")
            conn_eval[CONNECTION_TYPE_NPIV] = False
        else:
            # Loop over return db vioses for host to check Port settings.
            for db_vios in conn_eval['db_vioses']:
                for fc in db_vios.fcports:
                    if not fc.enabled or fc.fabric == 'A' or fc.fabric == 'B':
                        LOG.debug("Non default settings for FC Port %s."
                                  % fc.wwpn)
                        conn_eval[CONNECTION_TYPE_NPIV] = False
                        break

    del conn_eval['db_vioses']  # this item not needed in dict anymore
    LOG.debug("Returning check_if_hmc_default_migrate_maps_ok: %s" % conn_eval)
    return conn_eval


def eval_maximal_connectivity(context, conn_info, transaction=None):
    """
    Check that the obtained connectivity information contains the
    maximal set of VIOSes for the target host. In other words, for the
    valid VIOSes listed in the conn_info structure, do they represent the
    full set of registered VIOSes for the target host (npiv conn) or
    the full set of registered VIOSes that are members of the cluster
    (ssp conn)? The returned dictionary will contain a True value per the
    connection type if the connectivity is 'maximal', else False.

    :param context: The authorization context.
    :param conn_info: The connectivity information structure obtained for
                      a given SCG and a host.
    :returns: A maximal evaluation dictionary. It is of this form:
                    {'npiv': True/False,
                     'ibm_ssp': True/False
                     'db_vioses': [<list of DTOs>]}
    """
    LOG.debug("Entry. Computed connectivity_info: %s" % conn_info)
    # Get DB VIOSes for host
    vios_factory = storage_dom.VioServerFactory.get_factory()
    db_vioses = vios_factory.find_all_vioses_by_host(
        context, conn_info['target_host'], transaction=transaction)
    scg_cluster = conn_info['scg'].cluster_provider_name
    meval = {CONNECTION_TYPE_NPIV: True, CONNECTION_TYPE_SSP: True,
             'db_vioses': db_vioses}
    ssp_conn_vioses = [v['lpar_id'] for v in conn_info['vios_list']
                       if CONNECTION_TYPE_SSP in v['connection-types']]
    fc_conn_vioses = [v['lpar_id'] for v in conn_info['vios_list']
                      if CONNECTION_TYPE_NPIV in v['connection-types']]

    for db_vios in db_vioses:
        if CONNECTION_TYPE_NPIV in conn_info['connection-types'] and \
                (db_vios.lpar_id not in fc_conn_vioses):
            meval[CONNECTION_TYPE_NPIV] = False
            LOG.debug("Non-maximal: db_vios.lpar_id=%d, db_vios.lpar_"
                      "name=%s." % (db_vios.lpar_id, db_vios.lpar_name))
        if CONNECTION_TYPE_SSP in conn_info['connection-types'] and \
                db_vios.cluster_provider_name == scg_cluster and \
                (db_vios.lpar_id not in ssp_conn_vioses):
            meval[CONNECTION_TYPE_SSP] = False
            LOG.debug("Non-maximal: db_vios.lpar_id=%d, db_vios.lpar_"
                      "name=%s." % (db_vios.lpar_id, db_vios.lpar_name))
        if not meval[CONNECTION_TYPE_NPIV] and not meval[CONNECTION_TYPE_SSP]:
            break
    LOG.debug("Maximal connectivity eval: %s." % meval)
    return meval


def get_compute_nodes_from_DB(context, msg_dict=None):
    """
    This returns a list of compute nodes after querying the Nova DB.

    :param context: A context object that is used to authorize
                    the DB access.
    :returns: A list of compute nodes that are in service
    """
    context = context.elevated()  # What is the purpose of elevation?
    compute_nodes = nova_db.compute_node_get_all(context)
    return_nodes = []
    for compute in compute_nodes:
        service = compute['service']
        if not service:
            msg = _("No service entry for compute ID %s.") % compute['id']
            LOG.warn(msg)
            if msg_dict:
                msg_dict['messages'].append(msg)
            continue
        return_nodes.append(service["host"])
        #  host_to_send = {'db_id': compute['id'],
        #                  'host_name': service['host'],
        #                  'hyp_hostname': compute['hypervisor_hostname']}
    LOG.debug("db_hosts: %s" % return_nodes)
    return return_nodes


def is_rmc_state_allowed(vios_ref, rmc_state):
    """ Use this helper method to return True if the RMC state of a
        VIOS is one of the allowed states that will not prevent a VIOS
        from being flagged as storage-ready for SCGs. Not hanging this
        method off the storage VIOS DOM yet as it can be used with live
        K2 data in addition to DB values.

        Current impl accepts ['active', 'busy']
        A busy state may mean that a VIOS is not ready for storage
        connectivity, but that will be determined at attach time rather
        than when an SCG is evaluated through public APIs.
    """
    if rmc_state == 'busy':
        LOG.info(_("The Virtual I/O Server '%(vios)s' has an RMC state of "
                   "'busy', which may impact its connectivity capability "
                   "for storage operations.") % dict(vios=vios_ref))
    return (rmc_state in ['active', 'busy'])
