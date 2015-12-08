# =================================================================
# =================================================================
from paxes_nova.objects.compute.dom import VioServer
"""
For IBM PowerVC , this set of functions logically extends nova.db.api,
which defines query functions that wrap the database access implementations.
"""
import sys
import uuid

from nova import db, network

from nova.openstack.common import jsonutils, timeutils
from nova.openstack.common import log as logging
LOG = logging.getLogger(__name__)

import nova.db.api as nova_db_api
import nova.db.sqlalchemy.api as nova_db_sa_api
import nova.db.sqlalchemy.models as nova_db_sa_models

#from powervc_nova.objects.network.dom import VirtualEthernetAdapter,\
#    SharedEthernetAdapter, NetworkAssociation
#from powervc_nova.objects.compute.dom import VioServer, VioServerFactory
from paxes_nova.objects.storage.dom import StorageConnectivityGroup, FcPort

import paxes_nova.db.sqlalchemy.models as pvc_models
from paxes_nova.db.sqlalchemy.models import VioServerDTO2,\
    DtoBase_sqla, vios_dto_get_pk_id, Transaction_sqla
# TODO: 3 Remove all dependencies on network.models
import paxes_nova.db.network.models as pvc_adapter_dom

from paxes_nova.network.ibmpowervm import inventory_update
#change for paxes
#from powervc_keystone.encrypthandler import EncryptHandler

from sqlalchemy.orm import subqueryload
from sqlalchemy.orm import subqueryload_all

EAGER_VIO_SEA_ADAPTERS = subqueryload(pvc_adapter_dom.VioServer.sea_list)
EAGER_VIO_VEA_ADAPTERS = subqueryload(pvc_adapter_dom.VioServer.vea_list)
EAGER_SEA_VEA_ADAPTERS = subqueryload(pvc_adapter_dom.SharedEthernetAdapter
                                      ._all_veas)
EAGER_NETWORK_ASSN_SEA = subqueryload(pvc_adapter_dom.NetworkAssociation.sea)


def get_backend():
    """The backend is this module itself."""
    return sys.modules[__name__]


def initialize():
    """Used to Forces the initialization of this database back-end module"""
    pass


def model_query(context, model, *args, **kwargs):
    """ Wrappers the Base Model Query to provide PowerVC-specific logic """
    return nova_db_sa_api.model_query(context, model, *args, **kwargs)


def result_set_as_dict(result_set):
    """Utility to Convert the objects in the Result Set to Dictionaries"""
    new_result_set = list()
    for item in result_set:
        new_result_set.append(jsonutils.to_primitive(item))
    return new_result_set


##################################################
#########   HMC DB API Implementation   ##########
##################################################
def hmc_find_all(context, filters=None, session=None):
    """Retrieves all of the matching HMC's that are in the Database"""
    hmcs = list()
    host = filters.pop('host', None) if filters else None
    query = model_query(context, pvc_models.HmcDTO, session=session)
    # Add in any provided filters to the overall query being performed
    if filters:
        query = query.filter_by(**filters)
    hmc_refs = query.all()
    # If they asked to filter HMC's for a specific Host, do that now
    if host:
        query = model_query(
            context, pvc_models.HmcHostsDTO, session=session)
        hmc_hosts = query.filter_by(host_name=host).all()
        hmc_uuids = [hmc_host.hmc_uuid for hmc_host in hmc_hosts]
        hmc_refs = [hmc for hmc in hmc_refs if hmc['hmc_uuid'] in hmc_uuids]
    # For each HMC returned, query the Hosts associated with it
    for hmc_ref in hmc_refs:
        hmc_uuid = hmc_ref['hmc_uuid']
        hmc_ref = _hmc_decode_values(hmc_ref)
        # Query the list of Host that are associated with this HMC
        hmc_ref['managed_hosts'] = _hmc_get_hosts(context, hmc_uuid, session)
        hmcs.append(hmc_ref)
    return hmcs


def hmc_create(context, values, session=None):
    """Creates a new HMC instance in the Database"""
    hosts = values.pop('managed_hosts', None)
    values = _hmc_encode_values(values)
    hmc_uuid = values.get('hmc_uuid')
    # If we weren't given a session, then we need to create a new one
    if not session:
        session = nova_db_sa_api.get_session()
    # Create a Transaction around the create in the Database
    with session.begin():
        hmc_ref = pvc_models.HmcDTO()
        hmc_ref.update(values)
        hmc_ref.save(session=session)
        # Update the hmc_hosts table with the new list of Hosts
        _hmc_update_hosts(context, hmc_uuid, hosts, session)
    # Query the HMC from the DB, so the returned HMC is fully populated
    return hmc_find_all(context, dict(hmc_uuid=hmc_uuid), session)[0]


def hmc_update(context, hmc_uuid, values, session=None):
    """Updates an existing HMC instance in the Database"""
    hosts = values.pop('managed_hosts', None)
    values = _hmc_encode_values(values)
    # If we weren't given a session, then we need to create a new one
    if not session:
        session = nova_db_sa_api.get_session()
    # Create a Transaction around the update in the Database
    with session.begin():
        query = model_query(context, pvc_models.HmcDTO, session=session)
        hmc_ref = query.filter_by(hmc_uuid=hmc_uuid).first()
        hmc_ref.update(values)
        hmc_ref.save(session=session)
        # Update the hmc_hosts table with the new list of Hosts
        _hmc_update_hosts(context, hmc_uuid, hosts, session)
    # Query the HMC from the DB, so the returned HMC is fully populated
    return hmc_find_all(context, dict(hmc_uuid=hmc_uuid), session)[0]


def hmc_delete(context, hmc_uuid, session=None):
    """Removes an existing HMC instance from the Database"""
    # If we weren't given a session, then we need to create a new one
    if not session:
        session = nova_db_sa_api.get_session()
    # Create a Transaction around the delete in the Database
    with session.begin():
        query1 = model_query(context, pvc_models.HmcDTO, session=session)
        query2 = model_query(
            context, pvc_models.HmcHostsDTO, session=session)
        query1 = query1.filter_by(hmc_uuid=hmc_uuid)
        query2 = query2.filter_by(hmc_uuid=hmc_uuid)
        query1.soft_delete(synchronize_session=False)
        query2.soft_delete(synchronize_session=False)


def hmc_host_delete(context, host_name, session=None):
    """Removes an existing HMC Host Mapping from the Database"""
    # If we weren't given a session, then we need to create a new one
    if not session:
        session = nova_db_sa_api.get_session()
    # Create a Transaction around the delete in the Database
    with session.begin():
        query = model_query(
            context, pvc_models.HmcHostsDTO, session=session)
        query = query.filter_by(host_name=host_name)
        query.soft_delete(synchronize_session=False)


def _hmc_get_hosts(context, hmc_uuid, session):
    """Returns a list of the names of the Hosts associated with the HMC"""
    query = model_query(
        context, pvc_models.HmcHostsDTO, session=session)
    hmchosts = query.filter_by(hmc_uuid=hmc_uuid).all()
    return [host.host_name for host in hmchosts]


def _hmc_update_hosts(context, hmc_uuid, hosts, session):
    """Updates the list of Hosts for the HMC to be the list provided"""
    # If we weren't given any Hosts, then there is nothing to do
    if hosts is None:
        return
    # Query the list of Host that are associated with this HMC
    db_hosts = _hmc_get_hosts(context, hmc_uuid, session)
    # For each Host provided that doesn't exist, add it to the database
    for host in hosts:
        if host not in db_hosts:
            hmchosts_ref = pvc_models.HmcHostsDTO()
            hmchosts_ref.update(dict(hmc_uuid=hmc_uuid, host_name=host))
            hmchosts_ref.save(session=session)
    # For each Host that is in the DB but not provided, remove from database
    for db_host in db_hosts:
        if db_host not in hosts:
            query = model_query(
                context, pvc_models.HmcHostsDTO, session=session)
            query = query.filter_by(hmc_uuid=hmc_uuid, host_name=db_host)
            query.soft_delete(synchronize_session=False)


def _hmc_encode_values(values):
    """Encrypts any necessary sensitive values for the HMC entry"""
    if values is not None:
        values = values.copy()
        #Make sure to Encrypt the Password before inserting the database
        if values.get('password') is not None:
            values['password'] = EncryptHandler().encode(values['password'])
    return values


def _hmc_decode_values(hmc_ref):
    """Decrypts any sensitive HMC values that were encrypted in the DB"""
    if hmc_ref is not None:
        hmc_ref = jsonutils.to_primitive(hmc_ref)
        #Make sure to DeCrypt the Password after retrieving from the database
        if hmc_ref.get('password') is not None:
            hmc_ref['password'] = EncryptHandler().decode(hmc_ref['password'])
    return hmc_ref


##################################################
#########   Host DB API Implementation   #########
##################################################
def host_delete(context, host_name, session=None):
    """Removes an existing Host instance from the Database"""
    if session is None:
        session = nova_db_sa_api.get_session()
    with session.begin(subtransactions=True):
        nwkasn_list = network_association_find_all(context, host_name,
                                                   session=session)
        for nwkasn in nwkasn_list:
            nwkasn.delete(context, session=session)
        # Delete dependents before host: VioServers
        vios_list = vio_server_find_all(context, host_name, session=session)
        for vios in vios_list:
            vios.delete(context, session=session)
        # Also need to clean up the entry in the HMC Hosts DB Table
        hmc_query = model_query(
            context, pvc_models.HmcHostsDTO, session=session)
        hmc_query = hmc_query.filter_by(host_name=host_name)
        hmc_query.soft_delete(synchronize_session=False)
        # Need to query the Service based on the Host to know what to delete
        query = model_query(context, nova_db_sa_models.Service,
                            session=session)
        svc = query.filter_by(host=host_name).filter_by(topic='compute').\
            first()
        # If the Service did exist, then we will delete it from the Database
        if svc is not None:
            query = model_query(
                context, nova_db_sa_models.ComputeNode, session=session)
            compute_node = query.filter_by(service_id=svc.id).first()
            # If the Compute Node exists, then we will delete it from the DB
            if compute_node is not None:
                nova_db_api.compute_node_delete(context, compute_node.id)
            # Clean up the Service and Compute Host entries from the Database
            nova_db_api.service_destroy(context, svc.id)


def host_cluster_find_all(context, filters=None, session=None):
    """Retrieves all of the matching cluster hosts that are in the Database"""
    query = model_query(context, pvc_models.HmcHostClustersDTO,
                        session=session)
    # Add in any provided filters to the overall query being performed
    if filters:
        query = query.filter_by(**filters)
    return result_set_as_dict(query.all())


def host_cluster_create(context, values):
    """Creates a new cluster host instance in the Database"""
    # If we weren't given a session, then we need to create a new one
    session = nova_db_sa_api.get_session()
    # Create a Transaction around the create in the Database
    with session.begin():
        cluster_ref = pvc_models.HmcHostClustersDTO()
        cluster_ref.update(values)
        cluster_ref.save(session=session)
    #Return the DTO just created
    return jsonutils.to_primitive(cluster_ref)


def host_cluster_delete(context, cluster_id, host_name):
    """Removes an existing cluster host instance from the Database"""
    # If we weren't given a session, then we need to create a new one
    session = nova_db_sa_api.get_session()
    # Create a Transaction around the delete in the Database
    with session.begin():
        query = model_query(context, pvc_models.HmcHostClustersDTO,
                            session=session)
        clusters = query.filter_by(host_name=host_name, cluster_id=cluster_id)
        clusters.soft_delete(synchronize_session=False)


def _get_compute_host_name(context, compute_node_id, session=None):
    """Helper method to retrieve the Host Name for a Compute Node ID"""
    host_name = None
    #First we need to query the Compute Node to figure out the Service
    query = model_query(
        context, nova_db_sa_models.ComputeNode, session=session)
    compute_node = query.filter_by(id=compute_node_id).first()
    #If the Compute Node was found, query the Service for that Host
    if compute_node is not None:
        query = model_query(
            context, nova_db_sa_models.Service, session=session)
        service = query.filter_by(id=compute_node.service_id).first()
        host_name = service.host if service is not None else None
    return host_name


def _get_compute_node_id(context, host_name, session=None):
    """Helper method to retrieve the Compute Node ID for a Host Name"""
    compute_node_id = None
    # We need to query the Service based on the Host to know the Service ID
    query = model_query(context, nova_db_sa_models.Service, session=session)
    svc = query.filter_by(host=host_name).filter_by(topic='compute').first()
    #If the Service was found, query the Compute Node for the Service ID
    if svc is not None:
        query = model_query(
            context, nova_db_sa_models.ComputeNode, session=session)
        compute_node = query.filter_by(service_id=svc.id).first()
        compute_node_id = compute_node.id if compute_node is not None else None
    return compute_node_id


##################################################
########  PowerSpec DB API Implementation  #######
##################################################
def instance_power_specs_find_all(context, filters=None, session=None):
    """Retrieves all of the matching PowerSpecs that are in the Database"""
    query = model_query(
        context, pvc_models.InstancePowerSpecsDTO, session=session)
    # Add in any provided filters to the overall query being performed
    if filters:
        query = query.filter_by(**filters)
    # Convert the objects in the Result Set to Dictionaries to return
    return result_set_as_dict(query.all())


def instance_power_specs_update(context, instance_uuid, values, session=None):
    """ Updates an existing Server PowerSpecs in the Database """
    # If we weren't given a session, then we need to create a new one
    if not session:
        session = nova_db_sa_api.get_session()
    # Create a Transaction around the update in the Database
    with session.begin():
        query = model_query(
            context, pvc_models.InstancePowerSpecsDTO, session=session)
        specs_ref = query.filter_by(instance_uuid=instance_uuid).first()
        # If there wasn't an existing one found, we will treat this as a create
        if specs_ref is None:
            specs_ref = pvc_models.InstancePowerSpecsDTO()
            specs_ref.update(dict(instance_uuid=instance_uuid))
        specs_ref.update(values)
        specs_ref.save(session=session)
    # Convert the object to a Dictionary before it is returned
    return jsonutils.to_primitive(specs_ref)


def instance_power_specs_delete(context, instance_uuid, session=None):
    """ Removes an existing Server PowerSpecs from the Database """
    # If we weren't given a session, then we need to create a new one
    if not session:
        session = nova_db_sa_api.get_session()
    # Create a Transaction around the delete in the Database
    with session.begin():
        query = model_query(
            context, pvc_models.InstancePowerSpecsDTO, session=session)
        query = query.filter_by(instance_uuid=instance_uuid)
        query.soft_delete(synchronize_session=False)


##################################################
########   VIOS DB API Implementation   ##########
##################################################
def vios_find_all(context, filters=None, transaction=None):
    """Retrieves all of the matching VIOS's that are in the Database"""
    session = Transaction_sqla.find_session(transaction, True)
    query = model_query(context, VIO_SERVER_DTO, session=session)
    # Add in any provided filters to the overall query being performed
    if filters:
        _map_dom_filters(context, filters)
#        if 'host_name' in filters:
#            host = filters.pop('host_name')
#            filters['_compute_node_id'] = _get_compute_node_id(context, host)
        query = query.filter_by(**filters)
    # Convert the objects in the Result Set to Dictionaries to return
    vios_dicts = result_set_as_dict(query.all())
    for vios_dict in vios_dicts:
        _map_dto_dict_for_vios(context, vios_dict, session)
    return vios_dicts


def vios_create(context, values, transaction=None):
    """Creates a new VIOS instance in the Database"""
    session = Transaction_sqla.find_session(transaction, True)
    values = values.copy()
    host = values.pop('host_name', None)
    if values.get('id') is None:
        # 'id' of None comes from new DOM VIOSes: no params/logic to set it
        values.pop('id', None)
    # If we weren't given a session, then we need to create a new one
    if not session:
        session = nova_db_sa_api.get_session()
    # Create a Transaction around the insert in the Database
    with session.begin():
        vios_ref = VIO_SERVER_DTO(values['lpar_name'], values['lpar_id'],
                                  host, values['state'], values['rmc_state'])
        #First we need to get the Compute Node ID for the Host
        values['_compute_node_id'] = _get_compute_node_id(context, host,
                                                          session)
        #Now we can add the attributes to the DTO to Save to the DB
        vios_ref.update(values)
        vios_ref.save(context=context, session=session)
    # Convert the object to a Dictionary before it is returned
    vios_dict = jsonutils.to_primitive(vios_ref)
    _map_dto_dict_for_vios(context, vios_dict, session)
    return vios_dict


def vios_update(context, vios_id, values, transaction=None):
    """Updates an existing VIOS instance in the Database"""
    session = Transaction_sqla.find_session(transaction,
                                            create_if_not_found=True)
    values = values.copy()
    values.pop('host_name', None)
    vios_id_key = VioServer.get_id_attr_name()
    vios_id_from_values = values.pop(vios_id_key, None)
    if (vios_id_from_values is not None and vios_id_from_values != vios_id):
        raise ValueError("Inconsistent ID values for a VIOS: vios_id= '"
                         + str(vios_id) + "' but values['" + vios_id_key
                         + "']= '" + vios_id_from_values + "'")
    filters = _map_dom_filters(context, {vios_id_key: vios_id})
    # If we weren't given a session, then we need to create a new one
    if not session:
        session = nova_db_sa_api.get_session()
    # Create a Transaction around the update in the Database
    with session.begin():
        query = model_query(
            context, VIO_SERVER_DTO, session=session)
        vios_ref = query.filter_by(**filters).first()
        vios_ref.update(values)
        vios_ref.save(context=context, session=session)
    # Convert the object to a Dictionary before it is returned
    vios_dict = jsonutils.to_primitive(vios_ref)
    _map_dto_dict_for_vios(context, vios_dict, session)
    return vios_dict


def vios_delete(context, vios_id, transaction=None):
    """Removes an existing VIOS instance from the Database"""
    session = Transaction_sqla.find_session(transaction,
                                            create_if_not_found=True)
    filters = _map_dom_filters(context, {'id': vios_id})
    with session.begin(subtransactions=True):
        query = model_query(context, VIO_SERVER_DTO, session=session)
        vios_dto = query.filter_by(**filters).first()
        vios_dto.delete(context, session=session)
    ####
#    session = Transaction_sqla.find_session(transaction, True)
#    filters = _map_dom_filters(context, {'id': vios_id})
#    # If we weren't given a session, then we need to create a new one
#    if not session:
#        session = nova_db_sa_api.get_session()
#    # Create a Transaction around the delete in the Database
#    with session.begin():
#        query = model_query(
#            context, VIO_SERVER_DTO, session=session)
#        query = query.filter_by(**filters).delete(synchronize_session=False)


# TODO: 2 TO BE REMOVED IN SPRINT 3; cf. ViosAssocDTO.convert_dto_dict_to_dom
def _map_dom_filters(context, filters):
    """Convert DOM-level query-filters into their DTO-level counterparts.

    :param filters: a dictionary of DOM bindings that will (logically)
    scope query results for a DOM type.

    :returns: a dictionary of DTO-level bindings that corresponds to the given
    logical scope
    """
    vios_id = filters.pop('id', None)
    if vios_id:
        if VioServerDTO2.ID_SEPARATOR not in vios_id:
            filters['lpar_id'] = vios_id
        else:
            vios_id = vios_id.split(VioServerDTO2.ID_SEPARATOR)
            filters['host_name'] = vios_id[0]
            filters['lpar_id'] = vios_id[1]

    host_name = filters.pop('host_name', None)
    if host_name:
        host_id = pvc_models.host_find_id_by_name(context, host_name)
        filters['_compute_node_id'] = host_id
    return filters


def _map_dto_dict_for_vios(context, vios_dict, session):
    """Convert attributes from the DTO-level to their DOM counterparts.

    :param vios_dict: a dictionary of DTO bindings that corresponds to a DOM
    object

    :returns: a dictionary of DOM-level attribute bindings
    """
    compute_id = vios_dict.pop('_compute_node_id', None)
    vios_dict['host_name'] = _get_compute_host_name(context, compute_id,
                                                    session)
    vios_dict['id'] = (vios_dict['host_name'] + VioServerDTO2.ID_SEPARATOR
                       + str(vios_dict['lpar_id']))


#Since the Network VIOS DOM is Mapped rather than DTO, need to use that for now
NETWORK_DOM = 'paxes_nova.db.network.models'
__import__(NETWORK_DOM)
VIO_SERVER_DTO = getattr(sys.modules[NETWORK_DOM], 'VioServer')
#VIO_SERVER_DTO = pvc_models.VioServerDTO


##################################################
#########   SCG DB API Implementation   ##########
##################################################
def scg_find_all(context, filters=None, transaction=None,
                 query_modifier_fn=None):
    """Retrieves all of the matching SCG's that are in the Database

    :query_modifier_fn: an optional query transformation that applies query
    features beyond the simple "filters" constraints.
    """
#    return pvc_models.StorageConnectivityGroupDTO.find_all_doms(
#        context, filters=filters, transaction=transaction,
#        query_modifier_fn=query_modifier_fn)
    ####
    if transaction is None:
        transaction = Transaction_sqla()
        session = transaction.session
    else:
        session = Transaction_sqla.find_session(transaction,
                                                create_if_not_found=True)
    query = model_query(
        context, pvc_models.StorageConnectivityGroupDTO, session=session)
    # Add in any provided filters to the overall query being performed
    if filters:
        query = query.filter_by(**filters)
    if query_modifier_fn is not None:
        query = query_modifier_fn(query)
    # Convert the objects in the Result Set to Dictionaries to return
    scg_dicts = result_set_as_dict(query.all())
    for scg_dict in scg_dicts:
        scg_dict['vios_ids'] = _scg_get_vios_ids(context, scg_dict['_pk_id'])
    return scg_dicts


def scg_create(context, values, transaction=None):
    """Creates a new SCG instance in the Database"""
#    return pvc_models.StorageConnectivityGroupDTO.create_dom(context, values,
#                                                             transaction)
    #### TEMP BRIDGE
    txn = transaction
    session = None
    if transaction is not None and hasattr(transaction, 'session'):
        session = transaction.session
    #### TEMP BRIDGE
    ####
    vios_ids = values.pop('vios_ids', [])
    # Add the UUID Identifier if it isn't passed in
    if not values.get('id'):
        values['id'] = str(uuid.uuid4())
    # If we weren't given a session, then we need to create a new one
    if not session:
        session = nova_db_sa_api.get_session()
        txn = Transaction_sqla(session)
    # Create a Transaction around the insert in the Database
    with session.begin():
        scg_ref = pvc_models.StorageConnectivityGroupDTO(context)
        scg_ref.update(values)
        scg_ref.save(context=context, session=session)
        #Now we need to update the list of SCG to VIOS Associations
        if vios_ids:
            vioses = vios_find_all(context, transaction=txn)
            for vios in vioses:
                if vios['id'] in vios_ids:
                    assoc_ref = pvc_models.ScgViosAssociationDTO(context)
                    assoc_ref.update(dict(_scg_pk_id=scg_ref._pk_id))
                    assoc_ref.update(dict(_vios_pk_id=vios['_pk_id']))
                    assoc_ref.save(context=context, session=session)
    # Convert the object to a Dictionary before it is returned
    scg_dict = jsonutils.to_primitive(scg_ref)
    scg_dict['vios_ids'] = vios_ids
    return scg_dict


def scg_update(context, scg_id, values, transaction=None):
    """Updates an existing SCG instance in the Database"""
#    import pdb
#    pdb.set_trace()
#    return pvc_models.StorageConnectivityGroupDTO.update_dom_by_id(
#        context, StorageConnectivityGroup, scg_id, values,
#        transaction=transaction)
    ################
    # If we weren't given a transaction, then we need to create a new one
    if transaction is None:
        transaction = Transaction_sqla()
    session = Transaction_sqla.find_session(transaction,
                                            create_if_not_found=True)
    vios_dom_ids = values.pop('vios_ids', None)
    # Create a Transaction around the update in the Database
    with session.begin():
        query = model_query(context, pvc_models.StorageConnectivityGroupDTO,
                            session=session)
        scg_dto = query.filter_by(id=scg_id).first()
        if vios_dom_ids is not None:
            # TODO: 5 Move logic to SCG.hydrate_assoc'ns
            _update_scg_vioses(context, scg_dto, vios_dom_ids, session)
        scg_dto.update(values)
        scg_dto.save(context=context, session=session)
    # Convert the object to a Dictionary before it is returned
    scg_dict = jsonutils.to_primitive(scg_dto)
    scg_dict['vios_ids'] = _scg_get_vios_ids(context, scg_dto._pk_id,
                                             transaction)
    return scg_dict


def _update_scg_vioses(context, scg_dto, vios_dom_ids, session):
    """Make the given SCG-DTO associate exactly to the identified VIOSes."""
    with session.begin(subtransactions=True):
        vios_pk_ids = []
        for dom_id in vios_dom_ids:
            vios_pk_ids.append(_get_vios_pk_id(context, dom_id, session))
        # Disassociate removed VIOSes
        current_vioses = list(scg_dto.vios_list)
        for vios in current_vioses:
            if vios._pk_id not in vios_pk_ids:
                scg_dto.vios_list.remove(vios)
        current_pkids = [vios._pk_id for vios in scg_dto.vios_list]
        # Add new VIOSes
        for pkid in vios_pk_ids:
            if pkid not in current_pkids:
                query = model_query(context, VIO_SERVER_DTO, session=session)
                vios_dto = query.filter_by(_pk_id=pkid).first()
                scg_dto.vios_list.append(vios_dto)


def _get_vios_pk_id(context, vios_dom_id, session):
    """Returns the DTO _pk_id of the VIOS identified by the DOM VIOS-ID"""
    # Invert the saved mapping
    pk2dom_map = DtoBase_sqla._vios_id_pk2dom_map
#    vios_id_to_pk_id_map = {val: key for key, val in pk2dom_map.iteritems()}
    vios_id_to_pk_id_map = {}
    for key, value in pk2dom_map.iteritems():
        vios_id_to_pk_id_map[value] = key
    if vios_dom_id in vios_id_to_pk_id_map:
        return vios_id_to_pk_id_map[vios_dom_id]
    else:
        filters = {'id': vios_dom_id}
        _map_dom_filters(context, filters)
        query = model_query(context, VIO_SERVER_DTO)
        vios_dto = query.filter_by(**filters).first()
        DtoBase_sqla._vios_id_pk2dom_map[vios_dto._pk_id] = vios_dom_id
        return vios_dto._pk_id


def scg_delete(context, scg_id, transaction=None):
    """Removes an existing SCG instance from the Database"""
    pvc_models.StorageConnectivityGroupDTO.delete_dom_by_id(
        context, StorageConnectivityGroup, scg_id, transaction=transaction)


def _scg_get_vios_ids(context, scg_pk_id, transaction=None):
    """Helper method to retrieve the VIOS ID's for the given SCG"""
    vioses = vios_find_all(context, transaction=transaction)
    session = Transaction_sqla.find_session(transaction,
                                            create_if_not_found=True)
    query = model_query(
        context, pvc_models.ScgViosAssociationDTO, session=session)
    assocs = query.filter_by(_scg_pk_id=scg_pk_id).all()
    vios_pk_ids = [assoc._vios_pk_id for assoc in assocs]
    return [vios['id'] for vios in vioses if vios['_pk_id'] in vios_pk_ids]


##################################################
########  FCPort DB API Implementation   #########
##################################################
# TODO: 1 BEGIN: TEMP FOR SPRINT 2; REMOVE IN SPRINT 3
def vios_dto_get_pk_id_TEMP_VIOS_DTO(context, vios_id, session=None):
    """Returns the identified VioServerDTO2 or exception if not exactly one.
    """
    return vios_dto_get_pk_id(context, vios_id, session=session,
                              vios_dto_class=pvc_adapter_dom.VioServer)


def _UPDATE_VIOS_ID_MAP(context, vios_pk_id=None):
    if vios_pk_id and vios_pk_id in DtoBase_sqla._vios_id_pk2dom_map:
        return
    query = model_query(context, VIO_SERVER_DTO)
    if vios_pk_id is not None:
        query = query.filter_by(_pk_id=vios_pk_id)
    vios_dtos = query.all()
    for vios_dto in vios_dtos:
        DtoBase_sqla._vios_id_pk2dom_map[vios_dto._pk_id] = \
            (vios_dto.host_name + VioServerDTO2.ID_SEPARATOR
             + str(vios_dto.lpar_id))
# END: TEMP FOR SPRINT 2; REMOVE IN SPRINT 3


def fcport_find_all(context, filters=None, transaction=None):
    """Retrieves all of the matching FCPorts that are in the Database"""
# TODO: 2 BEGIN: TEMP FOR SPRINT 2; REMOVE IN SPRINT 3
    vios_pk_id = None
    session = Transaction_sqla.find_session(transaction, True)
    if filters:
        vios_id = filters.pop('vios_id', None)
        if vios_id is not None:
            vios_pk_id = vios_dto_get_pk_id_TEMP_VIOS_DTO(context, vios_id,
                                                          session)
            filters['_vios_pk_id'] = vios_pk_id
    # Update the VIOS_ID_MAP if necessary
    _UPDATE_VIOS_ID_MAP(context, vios_pk_id)
# END: TEMP FOR SPRINT 2; REMOVE IN SPRINT 3
    return pvc_models.FcPortDTO.find_all_doms(context, filters=filters,
                                              transaction=transaction)


def fcport_create(context, values, transaction=None):
    """Creates a new FCPort instance in the Database"""
# TODO: 2 BEGIN: TEMP FOR SPRINT 2; REMOVE IN SPRINT 3
    session = Transaction_sqla.find_session(transaction, True)
    vios_id = values.pop('vios_id', None)
    if vios_id:
        vios_pk_id = vios_dto_get_pk_id_TEMP_VIOS_DTO(context, vios_id,
                                                      session)
        values['_vios_pk_id'] = vios_pk_id
        # Update the VIOS_ID_MAP if necessary
        _UPDATE_VIOS_ID_MAP(context, vios_pk_id)
# END: TEMP FOR SPRINT 2; REMOVE IN SPRINT 3
    return pvc_models.FcPortDTO.create_dom(context, values,
                                           transaction=transaction)


def fcport_update(context, port_id, values, transaction=None):
    """Updates an existing FCPort instance in the Database"""
# TODO: 2 BEGIN: TEMP FOR SPRINT 2; REMOVE IN SPRINT 3
    session = Transaction_sqla.find_session(transaction, True)
    vios_id = values.pop('vios_id', None)
    if vios_id:
        vios_pk_id = vios_dto_get_pk_id_TEMP_VIOS_DTO(context, vios_id,
                                                      session)
        values['_vios_pk_id'] = vios_pk_id
        # Update the VIOS_ID_MAP if necessary
        _UPDATE_VIOS_ID_MAP(context, vios_pk_id)
# END: TEMP FOR SPRINT 2; REMOVE IN SPRINT 3
    filters = {FcPort.get_id_attr_name(): port_id}
    return pvc_models.FcPortDTO.update_dom(context, filters, values,
                                           transaction=transaction)


def fcport_delete(context, port_id, transaction=None):
    """Removes an existing FCPort instance from the Database"""
    pvc_models.FcPortDTO.delete_dom_by_id(context, FcPort, port_id,
                                          transaction=transaction)


##################################################
#########   SEA DB API Implementation   ##########
##################################################
def sea_find_all_new(context, filters=None, session=None):
    """Retrieves all of the matching SCG's that are in the Database"""
    query = model_query(
        context, pvc_models.SharedEthernetAdapterDTO, session=session)
    # Add in any provided filters to the overall query being performed
    if filters:
        query = query.filter_by(**filters)
    # Convert the objects in the Result Set to Dictionaries to return
    return result_set_as_dict(query.all())


def sea_create(context, values, session=None):
    """Creates a new SCG instance in the Database"""
    # If we weren't given a session, then we need to create a new one
    if not session:
        session = nova_db_sa_api.get_session()
    # Create a Transaction around the insert in the Database
    with session.begin():
        sea_ref = pvc_models.SharedEthernetAdapterDTO()
        sea_ref.update(values)
        sea_ref.save(session=session)
    # Convert the object to a Dictionary before it is returned
    return jsonutils.to_primitive(sea_ref)


def sea_update(context, sea_id, values, session=None):
    """Updates an existing SCG instance in the Database"""
    # If we weren't given a session, then we need to create a new one
    if not session:
        session = nova_db_sa_api.get_session()
    # Create a Transaction around the update in the Database
    with session.begin():
        query = model_query(
            context, pvc_models.SharedEthernetAdapterDTO, session=session)
        sea_ref = query.filter_by(_pk_id=sea_id).first()
        sea_ref.update(values)
        sea_ref.save(session=session)
    # Convert the object to a Dictionary before it is returned
    return jsonutils.to_primitive(sea_ref)


def sea_delete(context, sea_id, session=None):
    """Removes an existing SCG instance from the Database"""
    # If we weren't given a session, then we need to create a new one
    if not session:
        session = nova_db_sa_api.get_session()
    # Create a Transaction around the delete in the Database
    with session.begin():
        query = model_query(
            context, pvc_models.SharedEthernetAdapterDTO, session=session)
        query = query.filter_by(_pk_id=sea_id)
        query.delete(synchronize_session=False)


##################################################
#########   VEA DB API Implementation   ##########
##################################################
def vea_find_all(context, filters=None, session=None):
    """Retrieves all of the matching VEA's that are in the Database"""
    query = model_query(
        context, pvc_models.VirtualEthernetAdapterDTO, session=session)
    # Add in any provided filters to the overall query being performed
    if filters:
        query = query.filter_by(**filters)
    # Convert the objects in the Result Set to Dictionaries to return
    return result_set_as_dict(query.all())

# TODO: 3 FUTURE USE IN SPRINT 3
#def vea_find_all2(context, filters=None):
#    """Retrieves all of the matching VEA's that are in the Database"""
#    query = model_query(
#        context, pvc_models.VirtualEthernetAdapterDTO)
#    # Add in any provided filters to the overall query being performed
#    if filters:
#        _map_dom_filters(context, filters)
#        query = query.join(VIO_SERVER_DTO).filter_by(**filters)
#    # Convert the objects in the Result Set to Dictionaries to return
#    return result_set_as_dict(query.all())


def vea_create(context, values, session=None):
    """Creates a new VEA instance in the Database"""
    # If we weren't given a session, then we need to create a new one
    if not session:
        session = nova_db_sa_api.get_session()
    # Create a Transaction around the insert in the Database
    with session.begin():
        vea_ref = pvc_models.VirtualEthernetAdapterDTO()
        vea_ref.update(values)
        vea_ref.save(session=session)
    # Convert the object to a Dictionary before it is returned
    return jsonutils.to_primitive(vea_ref)


def vea_update(context, vea_id, values, session=None):
    """Updates an existing VEA instance in the Database"""
    # If we weren't given a session, then we need to create a new one
    if not session:
        session = nova_db_sa_api.get_session()
    # Create a Transaction around the update in the Database
    with session.begin():
        query = model_query(
            context, pvc_models.VirtualEthernetAdapterDTO, session=session)
        vea_ref = query.filter_by(_pk_id=vea_id).first()
        vea_ref.update(values)
        vea_ref.save(session=session)
    # Convert the object to a Dictionary before it is returned
    return jsonutils.to_primitive(vea_ref)


def vea_delete(context, vea_id, session=None):
    """Removes an existing VEA instance from the Database"""
    # If we weren't given a session, then we need to create a new one
    if not session:
        session = nova_db_sa_api.get_session()
    # Create a Transaction around the delete in the Database
    with session.begin():
        query = model_query(
            context, pvc_models.VirtualEthernetAdapterDTO, session=session)
        query = query.filter_by(_pk_id=vea_id)
        query.delete(synchronize_session=False)


##################################################
### Network Association DB API Implementation  ###
##################################################
def network_assoc_find_all(context, filters=None, session=None):
    """Retrieves all of the matching Network Associations that are in the DB"""
    query = model_query(
        context, pvc_models.NetworkAssociationDTO, session=session)
    # Add in any provided filters to the overall query being performed
    if filters:
        query = query.filter_by(**filters)
    # Convert the objects in the Result Set to Dictionaries to return
    return result_set_as_dict(query.all())


def network_assoc_create(context, values, session=None):
    """Creates a new Network Association instance in the Database"""
    # If we weren't given a session, then we need to create a new one
    if not session:
        session = nova_db_sa_api.get_session()
    # Create a Transaction around the insert in the Database
    with session.begin():
        network_ref = pvc_models.NetworkAssociationDTO()
        network_ref.update(values)
        network_ref.save(session=session)
    # Convert the object to a Dictionary before it is returned
    return jsonutils.to_primitive(network_ref)


def network_assoc_update(context, network_id, values, session=None):
    """Updates an existing Network Association instance in the Database"""
    # If we weren't given a session, then we need to create a new one
    if not session:
        session = nova_db_sa_api.get_session()
    # Create a Transaction around the update in the Database
    with session.begin():
        query = model_query(
            context, pvc_models.NetworkAssociationDTO, session=session)
        network_ref = query.filter_by(neutron_net_id=network_id).first()
        network_ref.update(values)
        network_ref.save(session=session)
    # Convert the object to a Dictionary before it is returned
    return jsonutils.to_primitive(network_ref)


def network_assoc_delete(context, network_id, session=None):
    """Removes an existing Network Association instance from the Database"""
    # If we weren't given a session, then we need to create a new one
    if not session:
        session = nova_db_sa_api.get_session()
    # Create a Transaction around the delete in the Database
    with session.begin():
        query = model_query(
            context, pvc_models.NetworkAssociationDTO, session=session)
        query = query.filter_by(neutron_net_id=network_id)
        query.delete(synchronize_session=False)


#**************************************************#
#    Old DB API's, Remove when Switched Over       #
#**************************************************#
####################################################
#########   VIOS DB API Implementation    ##########
####################################################
def vio_server_find_all(context, host_name=None, session=None):
    """API implementation using SQL-Alchemy; see API for design-level info."""
    # LOG.warn('IN: vio_server_find_all: host_name =' + str(host_name))
    query = model_query(context, pvc_adapter_dom.VioServer, session=session)
    # LOG.warn('sea_find_all query =' + str(query))

    # If we don't have a session, make sure that the options for load
    # are eager.
    if not session:
        query = query.options(subqueryload_all('sea_list._all_veas'))
        query = query.options(EAGER_VIO_VEA_ADAPTERS)

    if not host_name:
        vios_list = query.all()
    else:
        host_id = pvc_models.host_find_id_by_name(context, host_name)
        vios_list = query.filter_by(_compute_node_id=host_id).all()
    return vios_list


####################################################
#########  Adapter DB API Implementation  ##########
####################################################
def host_reconcile_network(context, host_networking_dict):
    """
    Given a dictionary representation of the networking resources for a host,
    update the currently stored host state to match it.
    """
    LOG.info('pvc_nova.db.sqlalchemy.api: host_reconcile_network, '
             'host_networking_dict len()= ' + str(len(host_networking_dict)))
    inventory_update.reconcile(context, host_networking_dict)


def sea_put(context, name, vio_server, slot, state, primary_vea,
            control_channel, additional_veas, session=None):
    """API implementation using SQL-Alchemy; see API for design-level info."""
    primary_vea.save(context, session)
    sea = pvc_adapter_dom.SharedEthernetAdapter(name,
                                                vio_server,
                                                slot,
                                                state,
                                                primary_vea,
                                                control_channel,
                                                additional_veas)
    sea.save(context, session)
    return sea


def sea_find_all(context, host_name=None, session=None):
    """API implementation using SQL-Alchemy; see API for design-level info."""
    # LOG.warn('IN: sea_find_all host_id =' + str(host_id))
    query = model_query(
        context, pvc_adapter_dom.SharedEthernetAdapter, session=session)

    # If we don't have a session, make sure that the options for load
    # are eager.
    if not session:
        query = query.options(EAGER_SEA_VEA_ADAPTERS)

    if not host_name:
        sea_list = query.all()
    else:
        host_id = pvc_models.host_find_id_by_name(context, host_name)
        sea_list = query.join(pvc_adapter_dom.VioServer) \
                        .filter_by(_compute_node_id=host_id).all()
    return sea_list


# TODO: 3 FUTURE USE IN SPRINT 3
#def sea_find_all2(context, filters=None):
#    """API implementation using SQL-Alchemy; see API for design-level info."""
#    query = model_query(context, pvc_models.SharedEthernetAdapterDTO)
#
#    # If we don't have a session, make sure that the options for load
#    # are eager.
#    query = query.options(EAGER_SEA_VEA_ADAPTERS)
#
#    if not filters:
#        sea_list = query.all()
#    else:
#        _map_dom_filters(context, filters)
#        sea_list = query.join(VIO_SERVER_DTO).filter_by(**filters).all()
#    return sea_list


def network_association_find(context, host_name, neutron_network_id,
                             session=None):
    """API implementation using SQL-Alchemy; see API for design-level info."""
    query = model_query(
        context, pvc_adapter_dom.NetworkAssociation, session=session)
    host_id = pvc_models.host_find_id_by_name(context, host_name)

    # If we don't have a session, make sure that the options for load
    # are eager.
    if not session:
        query = query.options(subqueryload_all('sea._all_veas'))
        query = query.options(subqueryload_all('sea.vio_server'))
        query = query.options(EAGER_NETWORK_ASSN_SEA)

    if host_id:
        return query.filter_by(_compute_node_id=host_id,
                               neutron_net_id=neutron_network_id).first()
    else:
        return None


def network_association_find_all_by_network(context, neutron_network_id,
                                            session=None):
    """API implementation using SQL-Alchemy; see API for design-level info."""
    query = model_query(
        context, pvc_adapter_dom.NetworkAssociation, session=session)

    # If we don't have a session, make sure that the options for load
    # are eager.
    if not session:
        query = query.options(subqueryload_all('sea._all_veas'))
        query = query.options(subqueryload_all('sea.vio_server'))
        query = query.options(EAGER_NETWORK_ASSN_SEA)

    return query.filter_by(neutron_net_id=neutron_network_id).all()


def network_association_find_all(context, host_name=None, session=None):
    """API implementation using SQL-Alchemy; see API for design-level info."""
    query = model_query(
        context, pvc_adapter_dom.NetworkAssociation, session=session)

    # If we don't have a session, make sure that the options for load
    # are eager.
    if not session:
        query = query.options(subqueryload_all('sea._all_veas'))
        query = query.options(subqueryload_all('sea.vio_server'))
        query = query.options(EAGER_NETWORK_ASSN_SEA)

    if not host_name:
        nwk_asn_list = query.all()
    else:
        host_id = pvc_models.host_find_id_by_name(context, host_name)
        # TODO: 04 Verify wrt RAS/exception idiom(s)
        if not host_id or host_id <= 0:
            LOG.warn('network_association_find_all: Failed to find ID '
                     'for host_name = ' + host_name)
            nwk_asn_list = []
        else:
            nwk_asn_list = query.filter_by(_compute_node_id=host_id).all()
    return nwk_asn_list


def network_association_find_distinct_networks(context,
                                               host_name=None,
                                               session=None):
    """API implementation using SQL-Alchemy; see API for design-level info."""
    query = model_query(
        context, pvc_adapter_dom.NetworkAssociation.neutron_net_id,
        session=session,
        base_model=pvc_adapter_dom.NetworkAssociation).\
        distinct(pvc_adapter_dom.NetworkAssociation.neutron_net_id)

    if not host_name:
        nwk_list = query.all()
    else:
        host_id = pvc_models.host_find_id_by_name(context, host_name)
        if not host_id or host_id <= 0:
            LOG.warn('network_association_find_distinct_networks: ID not found'
                     'for host_name = ' + host_name)
            nwk_list = []
        else:
            nwk_list = query.filter_by(_compute_node_id=host_id).all()

    return nwk_list


def network_association_put_no_sea(context, host_name, neutron_network_id,
                                   session=None):
    """API implementation using SQL-Alchemy; see API for design-level info."""
    return network_association_put_sea(context, host_name, neutron_network_id,
                                       None, session)


def network_association_put_sea(context, host_name, neutron_network_id, sea,
                                session=None):
    """API implementation using SQL-Alchemy; see API for design-level info."""
    nwkasn = network_association_find(context, host_name, neutron_network_id,
                                      session)
    if nwkasn is None:
        nwkasn = pvc_adapter_dom.NetworkAssociation(host_name,
                                                    neutron_network_id, sea)
    else:
        nwkasn.sea = sea
    nwkasn.save(context, session)
    return nwkasn


def instances_find(context, host_name, neutron_network_id, ports=None):
    """
    A list of all of the Virutal Machines for a given host name and neutron
    network id.
    """
    # Find all instances on the host
    instances_on_host = db.instance_get_all_by_host(context, host_name)

    if not ports:
        # Find all the instances using the network
        search_opts = {'network_id': neutron_network_id}
        network_data = network.API().list_ports(context, **search_opts)
        ports = network_data.get('ports', [])

    # Loop through and find the matches
    ret_instances = []
    for instance_on_host in instances_on_host:
        for port in ports:
            if port['device_id'] == instance_on_host['uuid']:
                ret_instances.append(port['device_id'])
                break

    return ret_instances


def find_all_scgs_with_any_cluster(context, filters=None, transaction=None):
    """
    Return SCGs matching the filters AND having a non-None cluster-provider.

    This creates a special condition (filter), which will be applied to an
    otherwise normal query.
    """
    dto_type = pvc_models.StorageConnectivityGroupDTO
    nun = None  # kludge to avoid PEP-8's E711
    if (filters is None or len(filters) == 0):
        query_modifier_fn = lambda query: \
            query.filter(dto_type.cluster_provider_name != nun)
            # E711 NOTE: In v0.7 SA's filter can't use "is None"
    else:
        query_modifier_fn = lambda query: \
            query.filter(dto_type.cluster_provider_name != nun).\
            filter_by(**filters)
    # TODO 4 Need to translate transaction into session somehwere...
    return scg_find_all(context, filters=filters, transaction=None,
                        query_modifier_fn=query_modifier_fn)


##################################################
###### On-board Task DB API Implementation #######
##################################################
def onboard_task_get_all(context, host, session=None):
    """Retrieves all of the On-board Tasks from the DB."""
    query = model_query(
        context, pvc_models.OnboardTaskDTO, session=session)
    return query.filter_by(host=host).all()


def onboard_task_get(context, task_id, session=None):
    """Retrieves one of the On-board Tasks from the DB."""
    query = model_query(
        context, pvc_models.OnboardTaskDTO, session=session)
    result = query.filter_by(id=task_id).first()
    #If we got a Task back, we need to append the Server records to it
    if result:
        result['servers'] = []
        #Query the onboard_task_servers table to get the associated records
        query2 = model_query(
            context, pvc_models.OnboardTaskServerDTO, session=session)
        result2 = query2.filter_by(task_id=task_id).all()
        #Loop through each returned row and add it to the primary object
        for server in result2:
            result['servers'].append(server)
    return result


def onboard_task_create(context, host, session=None):
    """Creates the given On-board Task in the DB."""
    task_ref = pvc_models.OnboardTaskDTO()
    task_ref.update(dict(host=host))
    task_ref.save(session=session)
    return task_ref


def onboard_task_update(context, task_id, values, session=None):
    """Updates the given On-board Task in the DB."""
    values = dict([(k, v) for k, v in values.iteritems() if v is not None])
    status = values.get('status', '')
    #If this is a final status, then set the end date/time
    if status == 'completed' or status == 'failed':
        values['ended'] = timeutils.utcnow()
    if not session:
        session = nova_db_sa_api.get_session()
    with session.begin():
        query = model_query(
            context, pvc_models.OnboardTaskDTO, session=session)
        task_ref = query.filter_by(id=task_id).first()
        task_ref.update(values)
        task_ref.save(session=session)
    return task_ref


def onboard_task_delete(context, task_id, session=None):
    """Deletes the given On-board Task from the DB."""
    if not session:
        session = nova_db_sa_api.get_session()
    with session.begin():
        #We need to cleanup both the Task and Task Server tables
        query1 = model_query(
            context, pvc_models.OnboardTaskDTO, session=session)
        query2 = model_query(
            context, pvc_models.OnboardTaskServerDTO, session=session)
        #Filter both tables on the Task ID that was given as input
        query1 = query1.filter_by(task_id=task_id)
        query2 = query2.filter_by(task_id=task_id)
        #Make sure to delete from the Task Server table first
        query2.soft_delete(synchronize_session=False)
        query1.soft_delete(synchronize_session=False)


def onboard_task_server_create(context, task_id,
                               svr_uuid, values, session=None):
    """Create the Server record for the given On-board Task"""
    values = dict(values)
    values['task_id'] = task_id
    values['server_uuid'] = svr_uuid
    task_ref = pvc_models.OnboardTaskServerDTO()
    task_ref.update(values)
    task_ref.save(session=session)
    return task_ref


def onboard_task_server_update(context, task_id,
                               svr_uuid, values, session=None):
    """Updates the Server record for the given On-board Task"""
    values = dict([(k, v) for k, v in values.iteritems() if v is not None])
    if not session:
        session = nova_db_sa_api.get_session()
    with session.begin():
        query = model_query(
            context, pvc_models.OnboardTaskServerDTO, session=session)
        task_ref = query.filter_by(task_id=task_id,
                                   server_uuid=svr_uuid).first()
        task_ref.update(values)
        task_ref.save(session=session)
    return task_ref
