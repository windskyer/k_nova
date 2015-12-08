#
# =================================================================
# =================================================================
"""
For IBM PowerVC:
This is a set of query functions for persistent Nova resources
"""

import copy
from nova.openstack.common.db import api as common_db
from nova.db import api as db_api

_BACKEND_MAPPING = {'sqlalchemy': 'paxes_nova.db.sqlalchemy.api'}
IMPL = common_db.DBAPI('sqlalchemy',
                       backend_mapping=_BACKEND_MAPPING, lazy=True)

"""
QUERY VERBS (get, find, find_all, create, update, delete, etc.):
'x_get(x_id)'
    Returns exactly one 'x' object or raises an exception if it does not exist
'x_find(params...)'
    Returns zero or one 'x' object. If the arguments (search criteria)
    match more than one 'x' object, then this returns an arbitrary one of them
'x_find_all(params...)'
    Returns a list of zero or more 'x' objects that match the given arguments
'x_create(values, params...)'
    Creates the identified 'x' object to support the corresponding 'x_get'
'x_update(x_id, values, params...)'
    Updates the identified 'x' object retrieved thru the corresponding 'x_get'
'x_delete(x_id, params...)'
    Deletes the identified 'x' object retrieved thru the corresponding 'x_get'
"""


def initialize():
    """Forces the initialization for underlying database back-end module"""
    return IMPL.initialize()


##################################################
##########    HMC DB API Definition    ###########
##################################################
def hmc_find_all(context, filters=None, session=None):
    """Retrieves all of the matching HMC's that are in the Database"""
    return IMPL.hmc_find_all(context, filters, session)


def hmc_find(context, filters=None, session=None):
    """Retrieves zero or one matching HMC's that are in the Database"""
    hmcs = hmc_find_all(context, filters, session)
    return hmcs[0] if hmcs else None


def hmc_create(context, values, session=None):
    """Creates a new HMC instance in the Database"""
    return IMPL.hmc_create(context, values, session)


def hmc_update(context, hmc_uuid, values, session=None):
    """Updates an existing HMC instance in the Database"""
    return IMPL.hmc_update(context, hmc_uuid, values, session)


def hmc_delete(context, hmc_uuid, session=None):
    """Removes an existing HMC instance from the Database"""
    return IMPL.hmc_delete(context, hmc_uuid, session)


def hmc_host_delete(context, host_name, session=None):
    """Removes an existing HMC Host Mapping from the Database"""
    return IMPL.hmc_host_delete(context, host_name, session)


##################################################
##########   Host DB API Definition    ###########
##################################################
def host_cluster_find_all(context, filters=None, session=None):
    """Retrieves all of the matching cluster hosts that are in the Database"""
    return IMPL.host_cluster_find_all(context, filters=None, session=None)


def host_cluster_create(context, values):
    """Creates a new cluster host instance in the Database"""
    return IMPL.host_cluster_create(context, values)


def host_cluster_delete(context, cluster_id, host_name):
    """Removes an existing cluster host instance from the Database"""
    return IMPL.host_cluster_delete(context, cluster_id,
                                    host_name)


def host_delete(context, host_name, session=None):
    """Removes an existing Host instance from the Database"""
    return IMPL.host_delete(context, host_name, session)


##################################################
##########  Instance DB API Definition  ##########
##################################################
def instance_find_all(context, filters=None, session=None):
    """Retrieves all of the matching VM's that are in the Database"""
    filters = filters if filters is not None else dict()
    #Since OpenStack seems to include Deleted on this call, explicitly exclude
    if filters.get('deleted') is None:
        filters['deleted'] = False
    return db_api.instance_get_all_by_filters(context, filters)


def instance_find(context, filters=None, session=None):
    """Retrieves zero or one matching VM's that are in the Database"""
    instances = instance_find_all(context, filters, session)
    return instances[0] if instances else None


def instance_create(context, values, session=None):
    """Creates a new VM instance in the Database"""
    power_specs = values.pop('power_specs', None)
    inst_ref = db_api.instance_create(context, values)
    #If there were PowerSpecs provided, then insert them now
    if power_specs is not None:
        instance_power_specs_create(
            context, inst_ref['uuid'], power_specs, session)
        #Query the Instance again to make sure the PowerSpecs is populated
        inst_ref = db_api.instance_get_by_uuid(context, inst_ref['uuid'])
    return inst_ref


def instance_update(context, instance_uuid, values, session=None):
    """Updates an existing VM instance in the Database"""
    values = copy.deepcopy(values)
    power_specs = values.pop('power_specs', None)
    #Merge in the existing MetaData if they asked for Partial Updates
    _instance_merge_metadata(context, instance_uuid, values)
    #Delegate to the OpenStack method to actually update the Instance
    inst_ref = db_api.instance_update(context, instance_uuid, values)
    #If there were PowerSpecs provided, then insert them now
    if power_specs is not None and inst_ref is not None:
        instance_power_specs_update(
            context, instance_uuid, power_specs, session)
        #Query the Instance again to make sure the PowerSpecs is populated
        inst_ref = db_api.instance_get_by_uuid(context, inst_ref['uuid'])
    return inst_ref


def instance_delete(context, instance_uuid, session=None):
    """Removes an existing VM instance from the Database"""
    instance_power_specs_delete(context, instance_uuid, session)
    return db_api.instance_destroy(context, instance_uuid)


def _instance_merge_metadata(context, instance_uuid, values):
    """Helper Method to Merge Partial Metadata into existing"""
    system_metadata = values.get('system_metadata', {})
    partial_metadata = system_metadata.pop('updates_only', False)
    #If they gave us only Partial System MetaData, we need to merge
    if partial_metadata:
        old_inst = db_api.instance_get_by_uuid(context, instance_uuid)
        #Convert the MetaData on the Instance from a List to a Dictionary
        old_metal = old_inst.get('system_metadata', [])
        old_metad = dict([(itm['key'], itm['value']) for itm in old_metal])
        #Add in the new MetaData over top of the all of the old MetaData
        old_metad.update(system_metadata)
        values['system_metadata'] = old_metad


##################################################
##########  PowerSpec DB API Definition  #########
##################################################
def instance_power_specs_find_all(context, filters=None, session=None):
    """Retrieves all of the matching PowerSpecs that are in the Database"""
    return IMPL.instance_power_specs_find_all(context, filters, session)


def instance_power_specs_find(context, filters=None, session=None):
    """Retrieves zero or one matching PowerSpecs that are in the Database"""
    power_specs = instance_power_specs_find_all(context, filters, session)
    return power_specs[0] if power_specs else None


def instance_power_specs_create(context, instance_uuid, values, session=None):
    """ Updates an existing Server specs in the Database """
    return IMPL.instance_power_specs_update(context, instance_uuid, values)


def instance_power_specs_update(context, instance_uuid, values, session=None):
    """ Updates an existing Server specs in the Database """
    return IMPL.instance_power_specs_update(context, instance_uuid, values)


def instance_power_specs_delete(context, instance_uuid, session=None):
    """ Removes an existing Server specs from the Database """
    return IMPL.instance_power_specs_delete(context, instance_uuid)


##################################################
##########   VIOS DB API Definition    ###########
##################################################
def vios_find_all(context, filters=None, transaction=None):
    """Retrieves all of the matching VIOS's that are in the Database"""
    return IMPL.vios_find_all(context, filters=filters,
                              transaction=transaction)


def vios_find(context, filters=None, transaction=None):
    """Retrieves zero or one matching VIOS's that are in the Database"""
    vioses = vios_find_all(context, filters=filters, transaction=transaction)
    return vioses[0] if vioses else None


def vios_create(context, values, transaction=None):
    """Creates a new VIOS instance in the Database"""
    return IMPL.vios_create(context, values, transaction=transaction)


def vios_update(context, vios_id, values, transaction=None):
    """Updates an existing VIOS instance in the Database"""
    return IMPL.vios_update(context, vios_id, values, transaction=transaction)


def vios_delete(context, vios_id, transaction=None):
    """Removes an existing VIOS instance from the Database"""
    return IMPL.vios_delete(context, vios_id, transaction=transaction)


##################################################
##########    SCG DB API Definition    ###########
##################################################
def scg_find_all(context, filters=None, transaction=None):
    """Retrieves all of the matching SCG's that are in the Database"""
    return IMPL.scg_find_all(context, filters=filters,
                             transaction=transaction)


def find_all_scgs_with_any_cluster(context, filters=None, transaction=None):
    """Returns all SCGs that have a cluster and match the filter bindings.
    """
    return IMPL.find_all_scgs_with_any_cluster(context, filters=filters,
                                               transaction=transaction)


def scg_find(context, filters=None, transaction=None):
    """Retrieves zero or one matching SCG's that are in the Database"""
    scgs = scg_find_all(context, filters=filters, transaction=transaction)
    return scgs[0] if scgs else None


def scg_create(context, values, transaction=None):
    """Creates a new SCG instance in the Database"""
    return IMPL.scg_create(context, values, transaction=transaction)


def scg_update(context, scg_id, values, transaction=None):
    """Updates an existing SCG instance in the Database"""
    return IMPL.scg_update(context, scg_id, values, transaction=transaction)


def scg_delete(context, scg_id, transaction=None):
    """Removes an existing SCG instance from the Database"""
    return IMPL.scg_delete(context, scg_id, transaction=transaction)


##################################################
##########  FCPort DB API Definition   ###########
##################################################
def fcport_find_all(context, filters=None, transaction=None):
    """Retrieves all of the matching FCPorts that are in the Database"""
    return IMPL.fcport_find_all(context, filters=filters,
                                transaction=transaction)


def fcport_find(context, filters=None, transaction=None):
    """Retrieves zero or one matching FCPorts that are in the Database"""
    fcports = fcport_find_all(context, filters=filters,
                              transaction=transaction)
    return fcports[0] if fcports else None


def fcport_create(context, values, transaction=None):
    """Creates a new FCPort instance in the Database"""
    return IMPL.fcport_create(context, values, transaction=transaction)


def fcport_update(context, port_id, values, transaction=None):
    """Updates an existing FCPort instance in the Database"""
    return IMPL.fcport_update(context, port_id, values,
                              transaction=transaction)


def fcport_delete(context, port_id, transaction=None):
    """Removes an existing FCPort instance from the Database"""
    return IMPL.fcport_delete(context, port_id, transaction=transaction)


##################################################
##########    SEA DB API Definition    ###########
##################################################
def sea_find_all_new(context, filters=None, session=None):
    """Retrieves all of the matching SCG's that are in the Database"""
    return IMPL.sea_find_all(context, filters, session)


def sea_find(context, filters=None, session=None):
    """Retrieves zero or one matching SEA's that are in the Database"""
    seas = sea_find_all(context, filters, session)
    return seas[0] if seas else None


def sea_create(context, values, transaction=None):
    """Creates a new SCG instance in the Database"""
    return IMPL.sea_create(context, values, transaction)


def sea_update(context, sea_id, sea_values, transaction=None):
    """Updates an existing SCG instance in the Database"""
    return IMPL.sea_update(context, sea_id, sea_values, transaction)


def sea_delete(context, sea_id, session=None):
    """Removes an existing SCG instance from the Database"""
    return IMPL.sea_delete(context, sea_id, session)


##################################################
##########    VEA DB API Definition    ###########
##################################################
# TODO: 5 TO BE REMOVED IN S3
def vea_find_all(context, filters=None, session=None):
    """Retrieves all of the matching VEA's that are in the Database"""
    return IMPL.vea_find_all(context, filters, session)


def vea_find_all2(context, filters=None, transaction=None):
    """Retrieves all of the matching VEA's that are in the Database"""
    return IMPL.vea_find_all2(context, filters=filters,
                              transaction=transaction)


def vea_find(context, filters=None, session=None):
    """Retrieves zero or one matching VEA's that are in the Database"""
    veas = vea_find_all(context, filters, session)
    return veas[0] if veas else None


def vea_create(context, values, transaction=None):
    """Creates a new VEA instance in the Database"""
    return IMPL.vea_create(context, values, transaction)


def vea_update(context, vea_id, values, transaction=None):
    """Updates an existing VEA instance in the Database"""
    return IMPL.vea_update(context, vea_id, values, transaction)


def vea_delete(context, vea_id, transaction=None):
    """Removes an existing VEA instance from the Database"""
    return IMPL.vea_delete(context, vea_id, transaction)


##################################################
####  Network Association DB API Definition  #####
##################################################
def network_assoc_find_all(context, filters=None, transaction=None):
    """Retrieves all of the matching Network Associations that are in the DB"""
    return IMPL.network_assoc_find_all(context, filters, transaction)


def network_assoc_find(context, filters=None, transaction=None):
    """Retrieves zero or one matching Network Association that are in the DB"""
    network_assocs = network_assoc_find_all(context, filters, transaction)
    return network_assocs[0] if network_assocs else None


def network_assoc_create(context, values, transaction=None):
    """Creates a new Network Association instance in the Database"""
    return IMPL.network_assoc_create(context, values, transaction)


def network_assoc_update(context, network_id, values, transaction=None):
    """Updates an existing Network Association instance in the Database"""
    return IMPL.network_assoc_update(context, network_id, values,
                                     transaction=transaction)


def network_assoc_delete(context, network_id, transaction=None):
    """Removes an existing Network Association instance from the Database"""
    return IMPL.network_assoc_delete(context, network_id, transaction)


##################################################
########## Old VIOS DB API Definition  ###########
##################################################
# DEPRECATED
def vio_server_find_all(context, host_name=None, session=None):  # DEPRECATED
# DEPRECATED
    """List all VioServers for named host or all hosts

    :param context: nova.openstack.common.context.RequestContext

    :param host_name: String, yields a COMPUTE_NODE (via SERVICE association)
    - if a host is named, then the scope is all associations for that host
    - if host is None, then the scope includes all known hosts

    :param session: sqlalchemy.orm.session, manages ORM/DB transactions
    - Queries use the given session or (if None) create a locally-scoped one

    :returns: List, possibly empty, of VioServers (DOM level objects)

    :raises: ComputeHostNotFound if the host_name yields no COMPUTE_NODE
    """
    return IMPL.vio_server_find_all(context, host_name, session)


####################################################
########## Old Adapter DB API Definition ###########
####################################################
def host_reconcile_network(context, host_networking_dict):
    """
    Given a dictionary representation of the networking resources for a host,
    update the currently stored host state to match it.
    """
    return IMPL.host_reconcile_network(context, host_networking_dict)


def sea_put(context, name, vio_server, slot, state, primary_vea,
            control_channel=None, additional_veas=None, session=None):
    """Create/update the SharedEthernetAdapter as specified

    :param context: nova.openstack.common.context.RequestContext
    :param name: String
    :param vio_server: VioServer, the SEA's owner
    :param slot: Integer
    :param state: String
    :param primary_vea: VirtualEthernetAdapter, SEA's required VEA
    :param control_channel: VirtualEthernetAdapter
    :param additional_veas: List of VirtualEthernetAdapter
    :param session: sqlalchemy.orm.session, manages ORM/DB transactions

    :returns: the SEA
    """
    return IMPL.sea_put(context, name, vio_server, slot, state, primary_vea,
                        control_channel, additional_veas, session)


def sea_find_all(context, host_name=None, session=None):
    """List all SharedEthernetAdapters (SEAs) for named host or all hosts

    :param context: nova.openstack.common.context.RequestContext

    :param host_name: String, yields a COMPUTE_NODE (via SERVICE association)
    - if a host is named, then the scope is all associations for that host
    - if host is None, then the scope includes all known hosts

    :param session: sqlalchemy.orm.session, manages ORM/DB transactions
    - Queries use the given session or (if None) create a locally-scoped one

    :returns: List, possibly empty, of SEAs

    :raises: ComputeHostNotFound if the host_name yields no COMPUTE_NODE
    """
    return IMPL.sea_find_all(context, host_name, session)


def sea_find_all2(context, filters=None, transaction=None):
    """List all SharedEthernetAdapters (SEAs) matching the filter(s)

    :param context: nova.openstack.common.context.RequestContext

    :param filters: a Dictionary of bindings that the query function uses
    to select the resulting SEAs.  Keywords may include SEA attributes and
    'host_name' to scope the results to a given host.  If there are no
    bindings, then all (undeleted) SEAs will be returned.

    :returns: List, possibly empty, of SEAs

    :raises: ComputeHostNotFound if a host_name yields no COMPUTE_NODE
    """
    return IMPL.sea_find_all2(context, filters, transaction)


####################################################
########## Old NetAssn DB API Definition ###########
####################################################
def network_association_find(context, host_name, neutron_network_id,
                             session=None):
    """Return the NetworkAssociation specified by the host+network pair

    :param context: nova.openstack.common.context.RequestContext
    :param host_name: String, yields a COMPUTE_NODE (via SERVICE association)
    :param neutron_network_id: String, the network's ID defined in Neutron
    :param session: sqlalchemy.orm.session, manages ORM/DB transactions
    - Queries use the given session or (if None) create a locally-scoped one

    :returns: NetworkAssociation, found as specified, else None

    :raises: ComputeHostNotFound if the host_name yields no COMPUTE_NODE
    """
    return IMPL.network_association_find(context, host_name,
                                         neutron_network_id, session)


def network_association_find_all(context, host_name=None, session=None):
    """List all NetworkAssociations for named host or all hosts

    :param context: nova.openstack.common.context.RequestContext

    :param host_name: String, yields a COMPUTE_NODE (via SERVICE association)
    - if a host is named, then the scope is all associations for that host
    - if host is None, then the scope includes all known hosts

    :param session: sqlalchemy.orm.session, manages ORM/DB transactions
    - Queries use the given session or (if None) create a locally-scoped one

    :returns: List of NetworkAssociation, possibly empty

    :raises: ComputeHostNotFound if the host_name yields no COMPUTE_NODE
    """
    return IMPL.network_association_find_all(context, host_name, session)


def network_association_find_all_by_network(context, neutron_network_id,
                                            session=None):
    """List all NetworkAssociations for the identified network or all networks

    :param context: nova.openstack.common.context.RequestContext

    :param neutron_network_id: String, the network's ID defined in Neutron

    :param session: sqlalchemy.orm.session, manages ORM/DB transactions
    - Queries use the given session or (if None) create a locally-scoped one

    :returns: List of NetworkAssociation, possibly empty

    :raises: ComputeHostNotFound if the host_name yields no COMPUTE_NODE
    """
    return IMPL.network_association_find_all_by_network(context,
                                                        neutron_network_id,
                                                        session)


def network_association_find_distinct_networks(context,
                                               host_name,
                                               session):
    """
    returns unique networks from all network associations for given host
    :param context: The context for building the data
    :param host_name: The name of the host
    """
    return IMPL.network_association_find_distinct_networks(context,
                                                           host_name,
                                                           session)


def network_association_put_no_sea(context, host_name, neutron_network_id,
                                   session=None):
    """Create/update the NetworkAssociation as specified, with NO SEA

    :param context: nova.openstack.common.context.RequestContext
    :param host_name: String, yields a COMPUTE_NODE (via SERVICE association)
    :param neutron_network_id: String, the network's ID defined in Neutron
    :param session: sqlalchemy.orm.session, manages ORM/DB transactions

    :returns: the NetworkAssociation

    :raises: ComputeHostNotFound if the host_name yields no COMPUTE_NODE
    """
    return IMPL.network_association_put_no_sea(context, host_name,
                                               neutron_network_id, session)


def network_association_put_sea(context, host_name, neutron_network_id, sea,
                                session=None):
    """Create/update the NetworkAssociation as specified

    :param context: nova.openstack.common.context.RequestContext
    :param host_name: String, yields a COMPUTE_NODE (via SERVICE association)
    :param neutron_network_id: String, the network's ID defined in Neutron
    :param sea: SharedEthernetAdapter, the (optional) SEA for this NA
    :param session: sqlalchemy.orm.session, manages ORM/DB transactions

    :returns: the NetworkAssociation

    :raises: ComputeHostNotFound if the host_name yields no COMPUTE_NODE
    """
    return IMPL.network_association_put_sea(context, host_name,
                                            neutron_network_id, sea, session)


def instances_find(context, host_name, neutron_network_id, ports=None):
    """
    A list of all of the Virutal Machines for a given host name and neutron
    network id.
    """
    return IMPL.instances_find(context, host_name, neutron_network_id, ports)


##################################################
######## On-board Task DB API Definition #########
##################################################
def onboard_task_get_all(context, host):
    """Retrieves all of the On-board Tasks from the DB."""
    return IMPL.onboard_task_get_all(context, host)


def onboard_task_get(context, task_id):
    """Retrieves one of the On-board Tasks from the DB."""
    return IMPL.onboard_task_get(context, task_id)


def onboard_task_create(context, host):
    """Creates the given On-board Task in the DB."""
    return IMPL.onboard_task_create(context, host)


def onboard_task_update(context, task_id, values):
    """Updates the given On-board Task in the DB."""
    return IMPL.onboard_task_update(context, task_id, values)


def onboard_task_delete(context, task_id):
    """Deletes the given On-board Task from the DB."""
    return IMPL.onboard_task_delete(context, task_id)


def onboard_task_server_create(context, task_id, svr_uuid, values):
    """Create the Server record for the given On-board Task"""
    return IMPL.onboard_task_server_create(context, task_id, svr_uuid, values)


def onboard_task_server_update(context, task_id, svr_uuid, values):
    """Updates the Server record for the given On-board Task"""
    return IMPL.onboard_task_server_update(context, task_id, svr_uuid, values)
