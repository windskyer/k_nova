# =================================================================
# =================================================================
import re
import uuid
from copy import deepcopy
from datetime import datetime, timedelta
from oslo.config import cfg

from nova.objects import base, fields
from nova.volume import cinder

import nova.openstack.common.log as logging

from paxes_nova import _, logcall

from paxes_nova.objects import dom
import paxes_nova.objects.compute.dom as compute_dom
import paxes_nova.db.api as db_api

from paxes_nova.storage import exception as stgex
from paxes_nova.storage.static_topology import StaticStgTopo
from paxes_nova.volume.cinder import PowerVCVolumeAPI, PowerVCFabricAPI

from paxes_nova.virt.ibmpowervm.common.constants \
    import CONNECTION_TYPE_NPIV, CONNECTION_TYPE_SSP

LOG = logging.getLogger(__name__)
cfg_opts = [
    cfg.IntOpt('ibmpowervc_fabric_sync_interval',
               default=180,
               help='The general synchronization interval in minutes, '
               'for setting the fabric information in the DB that Fibre '
               'Channel ports are logged into. This covers cases where '
               'a fabric change occurs, but a new fabric did not get '
               'registered')
]

CONF = cfg.CONF
CONF.register_opts(cfg_opts)


######################################################
#########   Managed Host DOM Implementation   ########
######################################################
class ManagedHostFactory(compute_dom.ManagedHostFactory):
    """Production DOM Factory implementation for Hosts"""

    ###############################################
    ### Implementing Resource Factory Definition ##
    ###############################################
    def _get_resource_classes(self):
        """Returns a List of DOM Classes or a Dictionary Mapping DOM Classes"""
        return {compute_dom.ManagedHost: ManagedHost}


class ManagedHost(compute_dom.ManagedHost):
    """Production DOM Resource implementation for Managed Hosts"""

    obj_extra_fields = ['fcports']

    ###############################################
    ##### Implementing DOM Resource Definition ####
    ###############################################
    def _initialize_resource_attributes(self):
        """Sets any default values that are defined for any attributes"""
        self._fcports = None

    def _reset_cached_attributes(self):
        """Wipes out any Cached Resource Attributes that are no longer valid"""
        self._fcports = None

    ###############################################
    ######  Defining Managed Host Interface  ######
    ###############################################
    @property
    def fcports(self):
        """Getter Method for the list of FCPorts for this Host"""
        #If the Ports haven't been loaded yet, load them and cache them
        if self._fcports is None:
            fact = FcPortFactory.get_factory()
            #Call the FCPort Factory to retrieve the list of Ports for the Host
            self._fcports = \
                fact.find_all_fcports_by_host(self._context, self.host_name)
        return self._fcports


######################################################
##########   VIO Server DOM Implementation   #########
######################################################
class VioServerFactory(compute_dom.VioServerFactory):
    """Production DOM Factory implementation for VIOS's"""

    ###############################################
    ######  Defining VIOS Factory Interface  ######
    ###############################################
    def find_all_vioses_by_cluster(self, context,
                                   cluster_provider_name, transaction=None):
        """Queries all VIOS's that are part of the VIOS Cluster specified"""
        filters = dict(cluster_provider_name=cluster_provider_name)
        return self.find_all_resources(
            VioServer, context, filters, transaction)

    ###############################################
    ### Implementing Resource Factory Definition ##
    ###############################################
    def _get_resource_classes(self):
        """Returns a List of DOM Classes or a Dictionary Mapping DOM Classes"""
        return {compute_dom.VioServer: VioServer}


class VioServer(compute_dom.VioServer):
    """Production DOM Resource implementation for VIO Servers"""

    obj_extra_fields = ['fcports', 'cluster_provider']

    ###############################################
    ##### Implementing DOM Resource Definition ####
    ###############################################
    def _initialize_resource_attributes(self):
        """Sets any default values that are defined for any attributes"""
        self._fcports, self._cluster_provider = (None, None)

    def _reset_cached_attributes(self):
        """Wipes out any Cached Resource Attributes that are no longer valid"""
        self._fcports, self._cluster_provider = (None, None)

    ###############################################
    #######  Defining VIO Server Interface  #######
    ###############################################
    @property
    def fcports(self):
        """Getter Method for the list of FCPorts for this VIOS"""
        #If the Ports haven't been loaded yet, load them and cache them
        if self._fcports is None:
            fact = FcPortFactory.get_factory()
            #Call the FCPort Factory to retrieve the list of Ports for the Host
            self._fcports = \
                fact.find_all_fcports_by_vios(self._context, self.id)
        return self._fcports

    @property
    def cluster_provider(self):
        """Getter Method for the VIOS Cluster for this VIOS"""
        #If they don't have a VIOS Cluster Name set, then just return None
        if self.cluster_provider_name is None:
            return None
        #If the Storage Provider haven't been loaded yet, load and cache it
        if self._cluster_provider is None:
            fact = StorageProviderFactory.get_factory()
            #Call the Provider Factory to retrieve the given Storage Provider
            self._cluster_provider = fact.find_provider_by_name(
                self._context, self.cluster_provider_name)
        return self._cluster_provider

    @cluster_provider.setter
    def cluster_provider(self, provider):
        """Setting Method for the VIOS Cluster for this VIOS"""
        self.cluster_provider_name = None
        self._cluster_provider = provider
        #If the Provider wasn't wiped out, update the Name to match
        if provider is not None:
            self.cluster_provider_name = provider.host_name

    def is_rmc_state_ok(self):
        """ Use this object method to return True if the RMC state of this
            VIOS is one of the allowed states that will not prevent a VIOS
            from being flagged as storage-ready for SCGs.

            Current impl accepts ['active', 'busy']
            A busy state may mean that a VIOS is not ready for storage
            connectivity, but that will be determined at attach time rather
            than when an SCG is evaluated through public APIs.
        """
        if self.rmc_state == 'busy':
            LOG.info(_("The Virtual I/O Server '%(vios)s' on host %(host)s "
                       "has an RMC state of 'busy', which may impact its "
                       "connectivity capability for storage operations.") %
                     dict(vios=self.lpar_name, host=self.host_name))
        return (self.rmc_state in ['active', 'busy'])


######################################################
########  Storage Provider DOM Implementation  #######
######################################################
class StorageProviderFactory(dom.ResourceFactory):
    """Production DOM Factory implementation for Storage Providers"""

    ###############################################
    ##### Defining Provider Factory Interface #####
    ###############################################
    def construct_emc_controller(self, context, access_ip,
                                 volume_pool_name, provider_ip,
                                 provider_user_id, provider_password,
                                 provider_port=None, host_display_name=None,
                                 auto_create=False, transaction=None):
        """Constructs (and optionally creates) the Provider specified"""
        kwargs = dict(host_type='emc', access_ip=access_ip,
                      volume_pool_name=volume_pool_name,
                      provider_ip=provider_ip,
                      provider_port=provider_port,
                      provider_password=provider_password,
                      provider_user_id=provider_user_id,
                      host_display_name=host_display_name)
        #Call the Parent Class to actually Construct the given Resource
        return self.construct_resource(EmcController, context,
                                       auto_create, transaction, **kwargs)

    def construct_svc_controller(self, context, access_ip,
                                 volume_pool_name, user_id,
                                 password=None, private_key=None,
                                 ssh_port=None, host_display_name=None,
                                 auto_create=False, transaction=None):
        """Constructs (and optionally creates) the Provider specified"""
        kwargs = dict(host_type='svc', access_ip=access_ip,
                      volume_pool_name=volume_pool_name, user_id=user_id,
                      password=password, private_key=private_key,
                      ssh_port=ssh_port, host_display_name=host_display_name)
        #Call the Parent Class to actually Construct the given Resource
        return self.construct_resource(SvcController, context,
                                       auto_create, transaction, **kwargs)

    def construct_vios_cluster(self, context, host_name, hmc_uuids,
                               host_display_name=None, unique_id=None,
                               auto_create=False, transaction=None):
        """Constructs (and optionally creates) the Provider specified"""
        kwargs = dict(host_type='ssp', host_name=host_name,
                      hmc_uuids=hmc_uuids, unique_id=unique_id,
                      host_display_name=host_display_name)
        #Call the Parent Class to actually Construct the given Resource
        return self.construct_resource(ViosCluster, context,
                                       auto_create, transaction, **kwargs)

    def find_all_providers(self, context, transaction=None):
        """Queries all Storage Providers through the Cinder REST API"""
        return self.find_all_resources(
            StorageProvider, context, None, transaction)

    def find_all_providers_by_type(self, context, host_type, transaction=None):
        """Queries all Providers matching the Host Type specified"""
        return self.find_all_resources(StorageProvider, context,
                                       dict(host_type=host_type), transaction)

    def find_provider_by_name(self, context, host_name, transaction=None):
        """Queries at most one Provider matching the Host Name specified"""
        return self.find_resource(StorageProvider, context,
                                  dict(host_name=host_name), transaction)

    def find_providers_by_display_name(self, context, display_name,
                                       transaction=None):
        """Queries Providers matching the display name specified"""
        return self.find_all_resources(StorageProvider, context,
                                       dict(host_display_name=display_name),
                                       transaction)

    def delete_provider(self, context, host_name, transaction=None):
        """Deletes the Storage Provider with the given Name from Cinder"""
        return self.delete_resource(
            StorageProvider, context, host_name, transaction)

    ###############################################
    ### Implementing Resource Factory Definition ##
    ###############################################
    def _get_resource_classes(self):
        """Returns a List of DOM Classes or a Dictionary Mapping DOM Classes"""
        return [StorageProvider, EmcController, SvcController, ViosCluster]

    @base.remotable
    def _find_all_resources(self, context, resource_class,
                            filters=None, transaction=None):
        """Factory implementation to query all resources matching the filter"""
        resources = []
        #Call the Cinder REST API to retrieve the list of Storage Providers
        providers = self._get_providers_from_cinder(context, filters)
        for provider in providers:
            provider_class = self._get_provider_class(provider['host_type'])
            #Construct the appropriate Resource Class for the Provider
            aresource = self._construct_resources(
                provider_class, context, [provider], transaction)[0]
            resources.append(aresource)
        return resources

    @base.remotable
    def _delete_multiple_resources(self, context, resource_class,
                                   resource_ids, transaction=None):
        """Factory implementation to delete the resources specified"""
        client = cinder.cinderclient(context).client
        scgfact = StorageConnectivityGroupFactory.get_factory()
        #For each Host Name provided delete the Provider through REST
        for resource_id in resource_ids:
            client.delete('/os-hosts/' + resource_id + '/internal')
            #Delete the default SCG created for the given Cluster
            scgfact.delete_default_cluster_scg(context, resource_id)

    ###############################################
    #####  Shared Provider DOM Helper Methods  ####
    ###############################################
    def _get_provider_class(self, backend_type):
        """Helper method to return the right resource class for the Provider"""
        type_map = dict(emc=EmcController, svc=SvcController, ssp=ViosCluster)
        return type_map.get(backend_type)

    def _get_providers_from_cinder(self, context, filters):
        """Helper method to call the Cinder REST API to retrieve Providers"""
        providers = []
        client = cinder.cinderclient(context).client
        #Call the Cinder REST API to retrieve the list of Providers
        for host in client.get('/os-hosts')[1]['hosts']:
            #If this is a Provider retrieve the details of the Provider
            if host['service'] == 'cinder-volume':
                body = client.get('/os-hosts/' + host['host_name'])[1]
                for resource in body['host']:
                    #We want to try to find the Registration section
                    if 'registration' not in resource:
                        continue
                    provider = resource['registration']
                    #Make sure that the Provider matches the Filter specified
                    if self._provider_matches_filters(provider, filters):
                        providers.append(provider)
        return providers

    def _provider_matches_filters(self, provider, filters):
        """Helper method to determine if the provider matches the filters"""
        #See if any of the provided Filters don't match the Provider
        if filters:
            for key, val in filters.iteritems():
                if provider.get(key) != val:
                    return False
        return provider is not None and len(provider) > 0


class StorageProvider(dom.Resource):
    """Production DOM Resource implementation for Storage Providers"""

    fields = dict(dom.Resource.fields,
                  host_name=fields.StringField(),
                  host_type=fields.StringField(),
                  host_display_name=fields.StringField(nullable=True),
                  volume_pool_name=fields.StringField(nullable=True),
                  registered_at=fields.DateTimeField(nullable=True))

    ###############################################
    ##### Implementing DOM Resource Definition ####
    ###############################################
    def _get_resource_id_attribute(self):
        """Provides the Name of the Attribute for the Resource Identifier"""
        return 'host_name'

    @base.remotable
    def _create_resource(self, context, transaction):
        """Actual Implementation that will Create the given Resource"""
        client = cinder.cinderclient(context).client
        values = self._get_resource_values()
        registration = dict(registration=values)
        #Call the Cinder REST API to Register the Storage Provider
        client.post('/os-hosts', body=dict(host=registration))
        self._hydrate_all_resource_attributes(values)
        #If this is a VIOS Cluster, then create the default SCG for it
        scgfact = StorageConnectivityGroupFactory.get_factory()
        scgfact.create_default_cluster_scg(
            context, values['host_name'], values['host_display_name'])

    @base.remotable
    def _update_resource(self, context, transaction):
        """Actual Implementation that will Update the given Resource"""
        client = cinder.cinderclient(context).client
        host_name = self._get_resource_identifer()
        values = self._get_resource_values(changes_only=True)
        #Call the Cinder REST API to Register the Storage Provider
        url = '/os-hosts/' + host_name + '/update-registration'
        client.put(url, body=dict(registration=values))
        self._hydrate_all_resource_attributes(values)


class EmcController(StorageProvider):
    """Production DOM Resource implementation for EMC Storage Controllers"""

    fields = dict(StorageProvider.fields,
                  access_ip=fields.StringField(),
                  user_id=fields.StringField(),
                  provider_ip=fields.StringField(),
                  provider_user_id=fields.StringField(),
                  password=fields.StringField(nullable=True),
                  provider_password=fields.StringField(nullable=True))


class SvcController(StorageProvider):
    """Production DOM Resource implementation for SVC Storage Controllers"""

    fields = dict(StorageProvider.fields,
                  access_ip=fields.StringField(),
                  user_id=fields.StringField(),
                  password=fields.StringField(nullable=True),
                  ssh_port=fields.StringField(nullable=True),
                  private_key_data=fields.StringField(nullable=True))


class ViosCluster(StorageProvider):
    """Production DOM Resource implementation for VIOS Cluster Providers"""

    fields = dict(StorageProvider.fields,
                  hmc_uuids=fields.ListOfStringsField(nullable=True))


######################################################
#######  Connectivity Group DOM Implementation  ######
######################################################

class StorageConnectivityGroupFactory(dom.ResourceFactory):
    """Production DOM Factory implementation for Storage Connectivity Groups"""

    ###############################################
    ######  Defining SCG Factory Interface  #######
    ###############################################
    SORTTYPE_EXTERNAL = 'external'
    SORTTYPE_CLUSTER = 'cluster'

    def construct_scg(self, context, display_name, enabled=None,
                      auto_defined=None, auto_add_vios=None,
                      fc_storage_access=None, port_tag=None,
                      cluster_provider_name=None, priority_cluster=None,
                      priority_external=None, vios_ids=None,
                      auto_create=False, transaction=None):
        """Constructs (and optionally creates) the SCG specified"""
        # TODO: 5 Check w/ BT: why don't defautl=/init work for this?
        if vios_ids is None:
            vios_ids = []
        kwargs = dict(display_name=display_name, enabled=enabled,
                      auto_defined=auto_defined, auto_add_vios=auto_add_vios,
                      fc_storage_access=fc_storage_access, port_tag=port_tag,
                      cluster_provider_name=cluster_provider_name,
                      priority_external=priority_external,
                      priority_cluster=priority_cluster, vios_ids=vios_ids)
        kwargs['id'] = str(uuid.uuid4())
        #Call the Parent Class to actually Construct the given Resource
        return self.construct_resource(StorageConnectivityGroup, context,
                                       auto_create, transaction, **kwargs)

    def create_default_fc_scg(self, context):
        """Creates the overall Default SCG if it doesn't exist yet"""
        filters = dict(fc_storage_access=True, auto_defined=True,
                       display_name='Any host, all VIOS')
        #We don't want to even try creating the Default SCG if not HMC
        if 'hmc' not in compute_dom.ManagedHost.get_supported_host_types():
            return None
        scgs = self.find_all_scgs(context, filters)
        #Only want to create the Default FC SCG if it doesn't exist
        if len(scgs) <= 0:
            return self.construct_scg(
                context, 'Any host, all VIOS', True, True, True,
                True, priority_external=1, auto_create=True)
        return scgs[0]

    def create_default_cluster_scg(self, context,
                                   provider_id, cluster_display_name):
        """Creates the Default SCG for the Cluster if it doesn't exist yet"""
        name = 'Any host in ' + cluster_display_name
        scgs = self.find_all_scgs_by_cluster(context, provider_id)
        #Only want to create the Default Cluster SCG if it doesn't exit
        if len(scgs) > 0:
            LOG.info(_("Default storage connectivity group for SSP provider "
                       "%(provider_id)s already exists: %(scg_id)s.") %
                     dict(provider_id=provider_id, scg_id=scgs[0].id))
            return scgs[0]

        # We also need to check for default SCGs with the same name because
        # SCG display_name must be unique per DB constraints. This needs
        # to be removed if found.
        filters = {'display_name': name, 'auto_defined': True}
        scgs = self.find_all_resources(
            StorageConnectivityGroup, context, filters=filters)
        if len(scgs) > 0:
            scgids = [scg.id for scg in scgs]
            LOG.info(_("Delete the automatically defined storage connectivity "
                       "group with ID %(scgids)s for the cluster provider "
                       "with ID %(prov_id)s that no longer exists, in "
                       "preparation for registering a provider with the "
                       "same name.") %
                     dict(scgids=scgids,
                          prov_id=scgs[0].cluster_provider_name))
            self.delete_multiple_resources(
                StorageConnectivityGroup, context, scgids)
        LOG.info(_("Now construct a new SSP-based storage connectivity group "
                   "with name: %s.") % name)
        return self.construct_scg(
            context, name, True, True, True, True,
            cluster_provider_name=provider_id, auto_create=True)

    def find_all_scgs(self, context, filters=None, sort_type=None,
                      transaction=None):
        """Returns all persistent SCGs matching the given criteria.

        :param context: The authorization context object.

        :filters: SCG-based bindings that will scope the query results.
        Generally, keywords match SCG attributes.  In particular, keyword
        'cluster_provider_name' is only valid if sort_type is
        SORTTYPE_CLUSTER.  This value should be the clusterID of the provider
        to match on.

        :param sort_type: Either None, SORTTYPE_EXTERNAL, or SORTTYPE_CLUSTER.
            If external or cluster, then the matching SCGs will be returned
            in priority order for their respective type.

        :parm transaction: Optional; query uses given txn instead of its own.

        :returns: list of matching SCG DOM objects.
        """
        if filters is None:
            filters = {}  # NOTE: Arg-default "={}" is a nasty Python gotcha
        priority_selector = lambda scg: scg.priority_cluster
        if sort_type == self.SORTTYPE_EXTERNAL:
            filters['fc_storage_access'] = True
            priority_selector = lambda scg: scg.priority_external
        elif filters.get('cluster_provider_name', None) is not None:
            # Avoid selecting all clusters if a name was given
            sort_type = None
        if sort_type != self.SORTTYPE_CLUSTER:
            scg_dicts_list = self._find_all_resources(
                context, StorageConnectivityGroup, filters=filters,
                transaction=transaction)
        else:
            scg_dicts_list = self.find_all_scgs_with_any_cluster(
                context, filters=filters, transaction=transaction)
        scg_list = self._construct_resources(StorageConnectivityGroup,
                                             context, scg_dicts_list,
                                             transaction)
        return sorted(scg_list, key=priority_selector)

    def find_all_scgs_by_cluster(self, context, cluster_provider_name=None,
                                 filters=None, transaction=None):
        """Returns SCGs for the named cluster, else for ANY cluster if None
        """
        if cluster_provider_name is not None:
            if filters is None:
                filters = {}
            filters['cluster_provider_name'] = cluster_provider_name
            return self.find_all_resources(
                StorageConnectivityGroup, context, filters=filters,
                transaction=transaction)
        else:
            return self.find_all_scgs_with_any_cluster(
                context, filters=filters, transaction=transaction)

    def find_all_scgs_with_any_cluster(self, context, filters=None,
                                       transaction=None):
        """Returns all SCGs that have a cluster and match the filter bindings.
        """
        return db_api.find_all_scgs_with_any_cluster(context, filters=filters,
                                                     transaction=transaction)

    def find_scg_by_id(self, context, scg_id, transaction=None):
        """Queries at most one SCG matching the SCG ID specified"""
        scg_id_key = StorageConnectivityGroup.get_id_attr_name()
        return self.find_resource(StorageConnectivityGroup, context,
                                  filters={scg_id_key: scg_id},
                                  transaction=transaction)

    def delete_scg(self, context, scg_id, transaction=None):
        """Deletes the SCG with the SCG ID specified"""
        LOG.info(_("The storage connectivity group with ID %(scg_id)s is "
                   "requested to be deleted.") % dict(scg_id=scg_id))
        return self.delete_resource(
            StorageConnectivityGroup, context, scg_id, transaction)

    def delete_default_cluster_scg(self, context,
                                   provider_id, transaction=None):
        """Deletes the default SCG for the VIOS Cluster specified"""
        scgs = self.find_all_scgs_by_cluster(
            context, cluster_provider_name=provider_id,
            filters={'auto_defined': True}, transaction=transaction)
        scgids = [scg.id for scg in scgs]
        LOG.info(_("Delete the automatically defined storage connectivity "
                   "group for the cluster provider '%(provider)s' with the "
                   "following connectivity group ID or IDs: %(scgids)s.") %
                 dict(provider=provider_id, scgids=scgids))
        if scgids:
            # Only call if we looked up any matching SCGs since it may
            # have already been removed.
            return self.delete_multiple_resources(
                StorageConnectivityGroup, context, scgids, transaction)

    ###############################################
    ### Implementing Resource Factory Definition ##
    ###############################################
    def _get_resource_classes(self):
        """Returns a List of DOM Classes or a Dictionary Mapping DOM Classes"""
        return [StorageConnectivityGroup]

    @base.remotable
    def _find_all_resources(self, context, resource_class,
                            filters=None, transaction=None):
        """Factory implementation to query all resources matching the filter"""
        # TODO 5 Need to tie transaction into the db_api query-function
        scgs = db_api.scg_find_all(context, filters=filters,
                                   transaction=transaction)
        return self._construct_resources(
            resource_class, context, scgs, transaction)

    @base.remotable
    def _delete_multiple_resources(self, context, resource_class,
                                   resource_ids, transaction=None):
        """Factory implementation to delete the resources specified"""
        for resource_id in resource_ids:
            db_api.scg_delete(context, resource_id)


class StorageConnectivityGroup(dom.Resource):
    """Production DOM Resource implementation for Storage Connectivity Group"""

    fields = dict(dom.Resource.fields,
                  id=fields.StringField(),
                  display_name=fields.StringField(),
                  enabled=fields.BooleanField(nullable=True),
                  auto_defined=fields.BooleanField(nullable=True),
                  auto_add_vios=fields.BooleanField(nullable=True),
                  fc_storage_access=fields.BooleanField(nullable=True),
                  port_tag=fields.StringField(nullable=True),
                  cluster_provider_name=fields.StringField(nullable=True),
                  priority_cluster=fields.IntegerField(nullable=True),
                  priority_external=fields.IntegerField(nullable=True),
                  vios_ids=fields.ListOfStringsField(nullable=True))

    obj_extra_fields = ['_vios_list']

    ###############################################
    ##### Implementing DOM Resource Definition ####
    ###############################################
    def _initialize_resource_attributes(self):
        """Sets any default values that are defined for any attributes"""
        self._vios_list = None
        self._dict_rep = None
        self.vios_ids = []

    def _reset_cached_attributes(self):
        """Wipes out any Cached Resource Attributes that are no longer valid"""
        self._vios_list = None

    @base.remotable
    def _create_resource(self, context, transaction):
        """Actual Implementation that will Create the given Resource"""
        values = self._get_resource_values()
        viosfact = VioServerFactory.get_factory()
        #If we should Auto-Add the VIOS's then we want to add them on creation
        if values.get('auto_add_vios'):
            provider = values.get('cluster_provider_name')
            filters = None if provider is None \
                else dict(cluster_provider_name=provider)
            vioses = viosfact.find_all_vioses(context, filters)
            values['vios_ids'] = [vios.id for vios in vioses]
        #Now call the Database API to actually create the SCG in the DB
        scg_dict = db_api.scg_create(context, values)
        self._hydrate_all_resource_attributes(scg_dict)

    @base.remotable
    def _update_resource(self, context, transaction):
        """Actual Implementation that will Update the given Resource"""
        scg_id = self._get_resource_identifer()
        values = self._get_resource_values(changes_only=True)
        scg = db_api.scg_update(context, scg_id, values)
        self._hydrate_all_resource_attributes(scg)

    ###############################################
    #########    Defining SCG Interface   #########
    ###############################################
    @property
    def vios_list(self):
        """Getter Method for the list of VIOS's for this SCG"""
        return self.get_vios_list(None)

    def get_vios_list(self, transaction):
        """ Helper that can take a transaction """
        #If the VIOS's haven't been loaded yet, load them and cache them
        if self._vios_list is None:
            temp_list = list()
            #Only bother to do the lookup if there is a VIOS ID set
            if self.vios_ids:
                fact = VioServerFactory.get_factory()
                #Loop through each of the VIOS ID's, looking up each one
                for vios_id in self.vios_ids:
                    #Call the Factory to retrieve the VIOS specified
                    temp_list.append(
                        fact.find_vios_by_id(self._context,
                                             vios_id,
                                             transaction=transaction))
            self._vios_list = dom.ChangeTrackedList(self._vios_list_setter,
                                                    temp_list)
        return self._vios_list

    @vios_list.setter
    def vios_list(self, new_list):
        LOG.debug("*vios_list setter*")
        self._vios_list_setter(new_list)

    def _vios_list_setter(self, new_list):
        self._vios_list = new_list
        vios_id_list = []
        for vios in new_list:
            vios_id_list.append(vios.id)
        self.vios_ids = vios_id_list

    ##################################
    # Extended DOM methods for SCGs  #
    ##################################
    def to_dict(self, context=None, transaction=None,
                skip_extended_lookups=False):
        """
        Return a dictionary representation of this SCG resource object.
        If a cached dictionary was previously built, then that will be
        returned, else one is built and cached on the DOM, and returned.
        :param context: Optional authorization context
        :param transaction: Optional active transaction scope to pass down
        :param skip_extended_lookup: If True, then processing will be
                optimized to not do extra database lookups or cinder calls
                to get SCG information that is pulled in from those areas.
                This type of call is not mainline and not cached.
        :returns: A dictionary representation of this SCG instance that
                  has the form defined by callers of the REST
                  storage-connectivity-groups API if not skipping extended
                  lookups.
        """
        if self._dict_rep is not None and not skip_extended_lookups:
            return self._dict_rep
        # Otherwise construct the dictionary
        d = self._get_dict_of_basic_attrs()

        # remove props we don't expose in the DOM fashion
        if "cluster_provider_name" in d.keys():
            del d["cluster_provider_name"]

        # Add in the basic cluster dict if SCG is associated with one
        if self.cluster_provider_name:
            d["vios_cluster"] = {"provider_name": self.cluster_provider_name}

        # If not doing extended lookups, then just return a simplified
        # dictionary, but don't cache it.
        if skip_extended_lookups:
            d['vios_ids'] = self.vios_ids if self.vios_ids is not None else []
            return d

        # Mainline path: Convert the associated topology info into nested
        # dictionary data for the SCG.
        hosts = {}
        vlist = self.get_vios_list(transaction=transaction)
        for vios in vlist:
            h_name = vios.host_name
            if h_name not in hosts.keys():
                hosts[h_name] = {"name": h_name, "vios_list": []}
            v = {"name": vios.lpar_name, "id": vios.id,
                 "lpar_id": vios.lpar_id,
                 "state": vios.state, "rmc_state": vios.rmc_state}
            hosts[h_name]["vios_list"].append(v)
        d["host_list"] = hosts.values()
        LOG.debug("SCG '%s' - host_list: %s." % (self.id, d["host_list"]))

        # Add provider information to dictionary.
        # 'applicable_providers' is new in 1.2.1 and it contains a list
        # of applicable storage_hostnames for the SCG that are not in
        # 'error' state. This allows for efficiencies in logic that needs
        # the SCG provider info, and if enabled in API output, this could
        # render the server_storage_hosts API obsolete.
        pci = None
        try:
            pci = PowerVCVolumeAPI().get_stg_provider_info(context,
                                                           use_cache=True)
            d['applicable_providers'] = []
        except Exception as ex:
            LOG.exception(ex)
            LOG.warn(_("Could not retrieve provider data."))
        if self.fc_storage_access and pci:
            for p in pci.values():
                if p['stg_type'] == 'fc' and p['backend_state'] != "error":
                    d['applicable_providers'].append(p['storage_hostname'])
        cluster = self.cluster_provider_name
        if cluster is not None:
            if pci and cluster in pci:
                d["vios_cluster"]["provider_display_name"] = \
                    pci[cluster]["host_display_name"]
                d["vios_cluster"]["backend_state"] = \
                    pci[cluster]["backend_state"]
                if pci[cluster]["backend_state"] != "error":
                    d['applicable_providers'].append(cluster)

        self._dict_rep = d
        return d

    def _get_dict_of_basic_attrs(self):
        """Returns a dict of simple, non-None attributes on this SCG.

        Attributes that are themselves other Resources are not included here.
        """
        scg_attr_keys = [
            'auto_add_vios', 'auto_defined', 'cluster_provider_name',
            'created_at', 'display_name', 'enabled', 'fc_storage_access',
            'id', 'port_tag', 'priority_cluster', 'priority_external',
            'updated_at']
        attr_dict = {}
        for key in scg_attr_keys:
            if self[key] is not None and self[key] != "None":
                attr_dict[key] = self[key]
        return attr_dict

    @logcall
    def to_dict_with_ports(self, context, include_ports=True,
                           include_offline=False, host_name=None,
                           transaction=None):
        """
        TODO: This should be made a private helper method and the to_dict()
              method signature changed to allow more flexibility in requesting
              the level of detail. E.g.: basic_only, with_vios_topology,
              with_providers, with_computed_readiness, with_online_ports,
              with_offline_ports.
        Return the SCG dictionary with FC port info. So the nested
        structure is
        {
            'prop': <value>,...
            'host_list': [{
                'vios_list': [{
                    'fcport_list': [{
                        'port_prop': <value>,...
                    },...]
                },...]
            },...]
        }
        Only FC Ports that are 'enabled' and have a matching SCG port_tag
        and have an OK status are included in the output, except for flagged
        cases outlined next.

        If include_ports is False, then use the port information only in
        calculating if the member VIOS is 'ready'.
        If include_offline is True, then include ports without an "OK" status,
        but don't set the port_ready and vios_ready counts through the
        data.
        If host_name is not none, then scope SCG output to only the provided
        host. This is useful for checking whether a particular host has
        sufficient connectivity.
        """
        scg = self  # init var for use in this method

        # First deep copy the base to_dict() data structure.
        # This will look up member VIOSes if necessary.
        scg_dict = deepcopy(self.to_dict(context=context,
                                         transaction=transaction))
        # Build a map of vios id to vios DOM for use later.
        vios_map = dict(map(lambda x: (x.id, x),
                            scg.get_vios_list(transaction)))
        scg_port_count = 0

        # vios_ready_count tracks the number of VIOSes that have been
        # determined to be 'storage_ready'. Note that The 'storage_ready'
        # value of true does not necessarily predict a successful
        # deployment using this VIOS as there are potentially many other
        # factors (even VIOS related) that could impact deployment and/or
        # relocation.
        scg_dict['vios_ready_count'] = 0
        host_name_verified = False
        for host in scg_dict['host_list']:
            if host_name is not None:
                if host['name'] != host_name:
                    # The caller is asking that this host be filtered out
                    # of the output, so just continue
                    continue
                host_name_verified = True
            host_port_count = 0
            vios_ready_count = 0
            for vios in host['vios_list']:
                db_vios = vios_map[vios['id']]

                # filter the ports
                port_list = db_vios.fcports if db_vios.fcports else []
                filtered = self._filter_applicable_ports(port_list,
                                                         include_offline)
                if include_ports:
                    vios['fcport_list'] = filtered
                num_filtered = len(filtered)
                vios['total_fcport_count'] = len(db_vios.fcports)
                if not include_offline:
                    vios['port_ready_count'] = num_filtered
                    # check if a provider is available
                    provider_ok = True
                    if 'vios_cluster' in scg_dict:
                        # Specific SSP provider to check
                        clst = scg_dict['vios_cluster']
                        if clst and 'backend_state' in clst and \
                                clst['backend_state'] == 'error':
                            provider_ok = False
                        # If the VIOS owns FC ports, then their status may
                        # not matter for the SSP case, so we artificially
                        # increment num_filtered here to keep the vios ready.
                        elif num_filtered == 0 and len(port_list) > 0:
                            LOG.debug("Override the requirement for FC "
                                      "ports in the SSP case.")
                            num_filtered = 1
                    elif 'applicable_providers' in scg_dict and \
                            not scg_dict['applicable_providers']:
                        # There is not at least one OK registered provider
                        provider_ok = False
                    # Track whether this VIOS is storage-ready. This is
                    # not necessarily a predictor of a successful deploy
                    # or relocation using this VIOS. Other factors may come
                    # into play.
                    if provider_ok and db_vios.state == 'running'\
                            and db_vios.is_rmc_state_ok()\
                            and num_filtered > 0:
                        vios['storage_ready'] = True
                        vios_ready_count = vios_ready_count + 1
                    else:
                        vios['storage_ready'] = False
                    host_port_count = host_port_count + len(filtered)
                LOG.debug("Length of db_vios.fcports = %d. The "
                          "filtered list length = %d." %
                          (len(db_vios.fcports), len(filtered)))
            if not include_offline:
                if self.fc_storage_access:
                    host['port_ready_count'] = host_port_count
                scg_port_count = scg_port_count + host_port_count
                host['vios_ready_count'] = vios_ready_count
                scg_dict['vios_ready_count'] += vios_ready_count
        if not include_offline and self.fc_storage_access:
            scg_dict['port_ready_count'] = scg_port_count
        if host_name is not None and not host_name_verified:
            error = _("The provided hypervisor host name '%(host)s' is not a "
                      "member of the storage connectivity group "
                      "'%(name)s'") % dict(host=host_name,
                                           name=self.display_name)
            msg = stgex.IBMPowerVCStorageError.msg_fmt % locals()
            LOG.error(msg)
            raise stgex.IBMPowerVCStorageError(msg)
        return scg_dict

    def _filter_applicable_ports(self, db_fc_port_list, include_offline):
        """
        Convert each fc port DTO to a dictionary, then
        filter the ports by enabled and port_tag and an
        existing wwpn, and optionally, by 'status'.
        """
        ports = filter((lambda port: port['enabled'] and
                        port['wwpn'] and len(port['wwpn']) and
                        (include_offline or port['status'] == 'OK') and
                        (self.port_tag is None or
                         ('port_tag' in port and
                          self.port_tag == port['port_tag']))),
                       map(lambda p: fcport_to_dict(p),
                           db_fc_port_list))
        # After filtering, we need to sort the ports by available_connections
        # So that the database retrieval of this list is consistent
        # with the live-data retrieval when determining connectivity
        # information.
        ports.sort(key=(lambda x: x['available_connections']),
                   reverse=True)
        LOG.debug("Sorted and filtered ports: %s." % ports)
        return ports

    def replace_vios_list(self, context, transaction, vios_ids):
        """
        Replace the SCG 'vios_list' field with the passed list of ids.
        """
        # NOTE: We compose our own unique vios "id" from:
        #       <host_name>##<lpar_id>
        #       This format is validated by this method. It also matches the
        #       vios.id format of the VioServer DOM begining in PVC 1.2.1.
        host_map = {}
        for vios_id in vios_ids:
            LOG.debug("Input VIOS_id = '%s'" % vios_id)
            parts = vios_id.split("##")
            if len(parts) != 2:
                msg = _("vios_ids item not in correct format for storage "
                        "connectivity group creation: %s" % vios_id)
                raise stgex.IBMPowerVCStorageError(msg)
            if parts[0] not in host_map.keys():
                host_map[parts[0]] = [vios_id]
            else:
                host_map[parts[0]].append(vios_id)
        LOG.debug("host_map:\n%s" % host_map)
        # First initialize the vios_list
        new_vios_list = []
        vios_factory = VioServerFactory.get_factory()
        # Now loop over the map of incoming items to look up VIOS DOMs and
        # pass in. TODO: Check whether looking up VIOSes by id instead of by
        # host would be cleaner or more efficient here. Also, could maybe
        # skip lookups for VIOSs already SCG members.
        for host, vioses in host_map.iteritems():
            # append vioses
            db_vioses = vios_factory.find_all_vioses_by_host(
                context, host, transaction=transaction)
            db_vios_by_id = dict([(db_v.id, db_v) for db_v in db_vioses])
            LOG.debug("db_vios_by_id=%s" % db_vios_by_id)
            for v_id in vioses:
                if v_id in db_vios_by_id:
                    new_vios_list.append(db_vios_by_id[v_id])
                else:
                    vios_id = v_id
                    msg = stgex.IBMPowerVCViosNotFound.msg_fmt % locals()
                    ex = stgex.IBMPowerVCViosNotFound(msg)
                    raise ex
        # Update the DOM property
        self.vios_list = new_vios_list

    def get_connectivity(self, context, host_name, live_data=True):
        """
        Return deploy-time storage connectivity information, given this
        storage connectivity group and a host name chosen for deployment.
        If the SCG specifies Storage Controller connectivity, then
        FC Port information is included.
        An algorithm will determine appropriate ports based on connectivity
        properties like dual/single fabric.

        :param context:   An authorization context.
        :param host_name: The host to constrain the VIOS candidates to.
                          This is the registered compute host name.
        :param live_data: Go to HMC K2 API to retrieve host topology data
                          if this parm is True, else only use DB. Live
                          data allows best-fit ordering of FC Ports when
                          multiple ports are available per VIOS and fabric.
        :returns:         See get_connectivity_info() method above for details
                          on ths structure of the returned connectivity
                          information.
                          Raises a IBMPowerVCStorageError exception if none
                          of the SCG-member VIOSes allow storage
                          connectivity with current topology data.
        """
        # We do not verify the 'enabled' state of the SCG here. We
        # assume the enabled state is filtered on when choosing an
        # SCG for deployment, but does not restrict operations later.

        scg_name = self.display_name
        conn_info = {'connection-types': [], 'scg_name': scg_name,
                     'scg': self, 'target_host': host_name}
        # Retrieve the member VIOSes only for the given host
        scg_vioses = self._get_host_vios_list(context, host_name)
        if not scg_vioses:
            error = _("The storage connectivity group '%s' associated "
                      "with this request does not have any VIOS members "
                      "for storage I/O connectivity." % scg_name)
            msg = stgex.IBMPowerVCStorageError.msg_fmt % locals()
            LOG.error(msg)
            raise stgex.IBMPowerVCStorageError(msg)
        LOG.debug("Initial SCG member vios info for host '%s': %s." %
                  (host_name, scg_vioses))
        num_v = len(scg_vioses)

        topology = None
        if live_data:
            ####################################################
            # Get live topology data for normal deploy flows   #
            ####################################################
            try:
                # we import here to avoid possible circular dependencies
                # as code as shifted into/out of the DOM.
                from powervc_nova.storage import common as topo
                # A few notes:
                # 1. Make the 'live' K2 call here without asking for Cluster
                #    feed for performance, since we are not syncing with any
                #    cluster membership data below.
                # 2. This may not be a live call since the K2 operator has
                #    built in caching on the VIOS elements.
                # 3. The FC Port properties we are interested in, specifically
                #    available_ports, is not supported for HMC eventing so
                #    the event-based operator cache does not work in our
                #    favor here - it falls back to the time based cache only.
                # 4. Due to #2 and #3 above, we cannot depend on the default
                #    time based cache for live data (e.g. 10 mins). We need
                #    to override the max time value to be on the order of
                #    seconds.
                topology = topo.get_host_topo_from_k2(context, host_name,
                                                      skip_cluster=True,
                                                      max_cache_age=30)
                vios_map = topology['vios_by_id']\
                    if host_name in topology['hosts'] else {}
                for scg_vios in scg_vioses:
                    LOG.debug("scg_vios = '%s'." % scg_vios)
                    if scg_vios['id'] not in vios_map.keys():
                        LOG.debug("Topology vios for id '%s' not found."
                                  % scg_vios['id'])
                        continue
                    ###############################################
                    # Replace scg_vios data with live topo data   #
                    # for specific properties.                    #
                    ###############################################
                    topo_vios = vios_map[scg_vios['id']]
                    scg_vios['state'] = topo_vios['state']
                    scg_vios['rmc_state'] = topo_vios['rmc_state']
                    # Unfortunately we cannot replace the fc port data from
                    # database wholesale because the live topology data
                    # does not contain the port metadata (fabric, port_tag,
                    # enabled). So we update just the vfc and status info.
                    for scg_port in scg_vios['fcport_list']:
                        if scg_port['id'] not in topo_vios['fcports']:
                            LOG.debug("Topology port for id '%s' not found. "
                                      "Its VIOS or RMC state is likely down."
                                      % scg_port['id'])
                            continue
                        topo_port = topo_vios['fcports'][scg_port['id']]
                        if 'available_vports' in topo_port.keys():
                            # this is the fcport sort key
                            scg_port['available_vports'] = \
                                topo_port['available_vports']
                            if 'total_vports' in topo_port.keys():
                                scg_port['total_vports'] = \
                                    topo_port['total_vports']
                        else:
                            scg_port['available_vports'] = 0
                        if 'status' in topo_port.keys():
                            scg_port['status'] = topo_port['status']
                    # Sort the FC ports so that the first one has the
                    # most VFCs available. We do a reverse sort so that
                    # the port with the most available VFCs is first.
                    # Note that the database ports are already sorted even
                    # even if we don't go through this live data flow.
                    LOG.debug("Refreshed scg_vios['fcport_list']: %s." %
                              scg_vios['fcport_list'])
                    scg_vios['fcport_list'].sort(key=(lambda x:
                                                      x['available_vports']),
                                                 reverse=True)
                    LOG.debug("Updated scg_vios = '%s'." % scg_vios)
                LOG.debug("SCG vios and fcport membership data updated "
                          "with synchronous K2 data.")
            except Exception as ex:
                LOG.exception(ex)
                LOG.warn(_("Could not retrieve storage topology data. "
                           "Continuing with database values only."))

        # Handle common cases of vios filtering where the VIOS or the
        # RMC connection is not active. We allow VIOSes with a 'busy'
        # RMC state to pass this filter. The nova orchestrator attachment
        # code for Paxes will check the state on a failed POST and will
        # retry multiple times for a "busy" RMC condition.
        p = re.compile('(running|active|busy)$', re.IGNORECASE)
        scg_vioses = [x for x in scg_vioses if p.match(x['state'])
                      and p.match(x['rmc_state'])]
        LOG.debug("Active SCG member vios info for host '%s': %s." %
                  (host_name, scg_vioses))
        if not scg_vioses:
            # This warning will report the specific problem, and then the
            # general error case at the bottom of this method will get
            # thrown with this subcase.
            msg = _("No VIOS members of storage connectivity group "
                    "%(scg)s have a running partition state and an "
                    "active or busy RMC state.") % dict(scg=self.display_name)
            conn_info['sub_case'] = msg
            LOG.warn(msg)
        else:
            # Call private helper methods to add the connectivity information.
            # It is important to add the NPIV connectivity first in the
            # case of "dual-connectivity" because it has the most detailed
            # information and the helper methods assume this ordering for
            # performance.
            if self.fc_storage_access is True:
                ###################################
                # Add FC (NPIV) connectivity      #
                ###################################
                self._add_npiv_conn_type(conn_info, scg_vioses,
                                         host_name, context)

            if self.cluster_provider_name is not None:
                ###################################
                # Add VIOS Cluster connectivity   #
                ###################################
                self._add_ssp_conn_type(conn_info, scg_vioses)

        if not conn_info['connection-types']:
            # No connectivity types were added to the info structure.
            if 'sub_case' in conn_info:
                sub_case = conn_info['sub_case']
            else:
                # General
                sub_case = _("Review the following recovery conditions or ",
                             "requirements and try the request again.")
            msg = stgex.IBMPowerVCConnectivityError.msg_fmt % dict(
                host_name=host_name, scg_name=scg_name, sub_case=sub_case)
            LOG.error(msg)
            raise stgex.IBMPowerVCStorageError(msg)

        LOG.debug("connectivity_info for SCG '%s': %s." % (scg_name,
                                                           conn_info))
        # Log an info message with connectivity passed back
        msg = _("Returning storage connectivity information for host "
                "%(host)s and SCG '%(scg)s' with %(num_vioses)d member "
                "VIOSs. The allowed VIOS connectivity follows.") %\
            dict(host=host_name, scg=self.display_name, num_vioses=num_v)
        for entry in conn_info['vios_list']:
            v_msg = _(" VIOS '%(vios)s' with state '%(state)s' and RMC state "
                      "'%(rmc)s' has connectivity types %(conn)s.") %\
                dict(vios=entry['name'], state=entry['state'],
                     rmc=entry['rmc_state'], conn=entry['connection-types'])
            msg = msg + v_msg
        LOG.info(msg)
        # return the data
        return conn_info

    def _get_host_vios_list(self, context, host_name):
        """
        Given an SCG object and host name, return a list of vios members for
        the host in their dictionary representation. A special to_dict()
        method is called on the SCG DOM to include the FC ports for each
        VIOS that are applicable to the SCG definition. The VIOS membership
        is automatically kept in sync with topology data.
        """
        # Get the dictionary representation with port info.
        # We include offline ports in this list because when live K2
        # data is gone after for these ports, we don't want to exclude
        # one that may come online since the last time topology has been
        # reconciled in the database.
        scg_dict = self.to_dict_with_ports(context, include_offline=True,
                                           host_name=host_name)

        # Check that the passed host is a member of the SCG.
        for host in scg_dict['host_list']:
            if host['name'] == host_name:
                return host["vios_list"]

        error = _("The passed host_name '%(host)s' is not a member of the "
                  "storage connectivity group with ID '%(scg_id)s'" %
                  dict(host=host_name, scg_id=self.id))
        LOG.error(error)
        msg = stgex.IBMPowerVCStorageError.msg_fmt % locals()
        ex = stgex.IBMPowerVCStorageError(msg)
        raise ex

    def _add_ssp_conn_type(self, conn_info, scg_vioses):
        """
        Private method to add VIOS cluster SSP connectivity info to the
        conn_info structure passed. The VIOSes that are members of the SCG and
        the chosen host are passed in and are assumed to be current so they
        will not be looked up again from the SCG.
        """
        if not "vios_list" in conn_info:
            conn_info["vios_list"] = []
        conn_vios_ids = [x['id'] for x in conn_info['vios_list']]

        for vios_dict in scg_vioses:
            # Check to see whether the vios should be filtered out of the
            # connectivity list, specifically for SSP connectivity.
            if self.validate_ssp_connectivity(vios_dict):
                if vios_dict['id'] not in conn_vios_ids:
                    # VIOS is not already in the connectivity info. Add it.
                    vios_dict['connection-types'] = [CONNECTION_TYPE_SSP]
                    conn_info['vios_list'].append(vios_dict)
                    LOG.debug("Added vios to connectivity for SSP only: %s"
                              % vios_dict)
                else:
                    vios_dict['connection-types'].append(CONNECTION_TYPE_SSP)
                if not CONNECTION_TYPE_SSP in conn_info['connection-types']:
                    conn_info['connection-types'].append(CONNECTION_TYPE_SSP)
            else:
                if vios_dict['id'] in conn_vios_ids:
                    LOG.info(_("Storage connectivity group '%(scg)s' "
                               "specifies both ssp and npiv connectivity, but "
                               "the following VIOS entry is not valid "
                               "for SSP connectivity: %(vios)s.") %
                             dict(scg=self.id, vios=vios_dict))
                else:
                    LOG.info(_("VIOS entry is excluded from SSP "
                               "connectivity: %s.") % vios_dict)
        return

    def validate_ssp_connectivity(self, vios_dict):
        """
        Validate SSP connectivity on a per-vios basis. It is not yet
        practical to check things like whether there is vSCSI virtual
        slots available. However, we can check the status of the
        associated SSP/VIOS cluster.
        """
        return True

    def _add_npiv_conn_type(self, conn_info, scg_vioses, host, context):
        """
        Private method to add NPIV connection information to conn_info
        structure passed. The VIOSes that are members of the SCG and
        the chosen host are passed in and are assumed to be current so they
        will not be looked up again from the SCG. If live (synchronous)
        topology data was collected, then the FC ports for each vios will
        be priority-ordered based on the number of available 'virtual
        functions' of the port (number of VMs that can use it for NPIV).

        This method deletes the 'fcport_list' dictionary entry from
        scg_vioses and replaces it with lists split out by fabric.
        """

        # We assume vios_list will start empty in this method.
        conn_info['vios_list'] = []
        at_least_one_port = False
        at_least_one_dual = False  # applies only to dual fabric config
        for vios_dict in scg_vioses:

            # The following call will do the required second tier of
            # filtering by: 'enabled', 'fabric', and 'port_tag'
            ports_by_fabric = self._vios_ports_by_fabric(vios_dict)
            if ports_by_fabric.keys():
                vios_dict["ports"] = ports_by_fabric
                at_least_one_port = True
                # Remove the old 'fcport_list' entry not split out by fabric
                del vios_dict['fcport_list']
                # The orchestrator has requested that we only add the VIOS
                # when ports are available for the NPIV case.
                vios_dict['connection-types'] = [CONNECTION_TYPE_NPIV]
                conn_info['vios_list'].append(vios_dict)
                if not CONNECTION_TYPE_NPIV in conn_info['connection-types']:
                    conn_info['connection-types'].\
                        append(CONNECTION_TYPE_NPIV)

                if (ports_by_fabric.keys()[0] == "None" or
                        len(ports_by_fabric.keys()) > 1):
                    at_least_one_dual = True
            else:
                LOG.debug("No viable FC ports for vios '%s'. It will not be "
                          "included in the connectivity list." % vios_dict)

        if not at_least_one_port:
            warn = _("There are no FC Ports for any Virtual I/O Servers in "
                     "storage connectivity group, %(name)s, and host "
                     "%(host)s that satisfy the connectivity criteria.")
            warn = warn % dict(name=self.display_name, host=host)
            LOG.warning(warn)
            conn_info['sub_case'] = warn
            LOG.info(_("Data for Virtual I/O Servers not storage-ready"
                       ": %s.") % scg_vioses)
            # Continue since ssp connectivity may still be applicable
            del conn_info['vios_list']
        elif not at_least_one_dual:
            # NOTE: In most environments, this condition is likely an error,
            #       i.e. the resulting deploy will not have the redundancy
            #       desired. When Enhancement 9933 (SCG VIOS multiplicity)
            #       is implemented, then the SCG can specify the required
            #       number of separate VIOS connections and the error case
            #       could be accurately distinguished from a desired
            #       single-VIOS case.
            msg = _("FC Ports for host '%s' may be configured for "
                    "dual switch fabrics, but there are no Virtual I/O "
                    "Servers with at least one applicable FC Port for each "
                    "fabric.")
            LOG.info(msg % host)

        # Return the connectivity structure
        return conn_info

    def _vios_ports_by_fabric(self, vios_dict):
        """
        Given a vios dictionary containing a fcport_list of Fibre
        Channel port information, return a dictionary mapping of the ports
        split out by their respective fabrics, as required by the
        get_connectivity_info() API. It is expected that the "fcport_list"
        coming in is already filtered for the specific SCG DOM passed
        (i.e. it does not contain disabled ports or ports with tags not
        matching the SCG port_tag), AND already sorted by 'available_vports'
        if that information is available.
        This method may do extra filtering on the ports as needed.
        """
        return_dict = {}

        if not vios_dict['fcport_list']:
            LOG.info(_("Virtual I/O Server '%(name)s' has no applicable FC "
                       "Ports for the storage connectivity group '%(scg)s'. "
                       "Skipping it.") %
                     dict(name=vios_dict['name'], scg=self.display_name))
            return {}

        wwpns = set()
        for port in vios_dict['fcport_list']:
            fabric = ("None" if port['fabric'] is None or
                      port['fabric'] == "None" else port['fabric'])
            port_info = {'udid': port['id'],
                         'name': port['name'],
                         'wwpn': port['wwpn']}
            if 'enabled' in port.keys() and not port['enabled']:
                LOG.debug("Skipping FC port since not enabled for PowerVC: "
                          "%s." % port)
                continue
            if port['wwpn'] in wwpns:
                LOG.warn(_("FC Port with WWPN already seen for Virtual I/O"
                           " Servers. Skipping FC Port: %s.") % port)
                continue
            if 'status' in port.keys():
                if port['status'].startswith("OK"):
                    port_info['status'] = port['status']
                else:
                    LOG.debug("Skipping port. Status is NOT OK: %s." % port)
                    continue
            wwpns.add(port['wwpn'])
            if 'total_vports' in port.keys():
                port_info['total_vports'] = port['total_vports']
                port_info['available_vports'] = port['available_vports']
            elif 'status' not in port.keys():
                # This should not happen with the status being set properly
                LOG.debug("Skipping port since no VFC info (non-npiv): "
                          "%s." % port)
                continue

            # Add port to fabric list
            if fabric in return_dict:
                return_dict[fabric].append(port_info)
            else:
                return_dict[fabric] = [port_info]
        # end for each db_port
        if "A" in return_dict and "B" in return_dict and "None" in return_dict:
            LOG.warn(_("FC Port configuration anomaly: Since dual-fabric "
                       "ports are available, ports not identified with a "
                       "fabric will not be storage connectivity candidates:"
                       " %s." % str(return_dict['None'])))
            del return_dict['None']
        return return_dict

    def verify_host_connectivity(self, context, host_name=None, msg_dict=None):
        """
        Check that the passed SCG has any storage connectivity using
        topology and metadata information currently in the PVC DB.
        Do not go to K2 synchronously for this check.
        :param scg: The storage connectivity group to check.
        :param host_name: Optional host to constrain the checking to,
                    i.e. check just this host
        :param msg_dict: Optional dictionary with key 'messages' containing
                         a list of informational or warning messages that
                         can be appended to.
        :returns: A list of hosts with at least one VIOS member of the SCG
                  that has at least one connectivity type. An empty list
                  means no connectivity.
        """
        hosts_with_conn = []
        hosts_wout = []
        scg_dict = self.to_dict_with_ports(context, host_name=host_name)
        # loop over list of host names
        for host in scg_dict['host_list']:
            if host_name and host_name != host['name']:
                LOG.debug("Host %s not considered since filtered out by "
                          "specific host to check connectivity for: %s." %
                          (host['name'], host_name))
            # For Paxes 1.2.0.x, any vios count greater than 0, is
            # sufficient for the storage ready filters. A future release
            # may require dual-vios per SCG definition.
            elif 'vios_ready_count' in host and host['vios_ready_count'] > 0:
                hosts_with_conn.append(host['name'])
                LOG.debug("Adding host '%s' to connectivity list for SCG "
                          "'%s'. host_conn_info=%s." %
                          (host['name'], self.display_name, host))
            else:
                hosts_wout.append(host['name'])
                msg = _("Host '%(host_name)s' does not meet the connectivity "
                        "criteria per storage connectivity group '%(scg)s', "
                        "and it is filtered out of host candidacy lists. The "
                        "reference connectivity information is: %(host)s.") %\
                    dict(host_name=host['name'], scg=self.display_name,
                         host=host)
                LOG.info(msg)

        if hosts_with_conn:
            msg = _("Storage connectivity group '%(scg)s' allows at least one "
                    "type of connectivity from hosts: %(host)s.")\
                % dict(scg=self.display_name, host=hosts_with_conn)
            if not host_name:
                msg = msg + _(" Hosts without connectivity: %s") % hosts_wout
            LOG.info(msg)
        elif hosts_wout and msg_dict:
            msg = _("INFO: One or more hosts do not meet the connectivity "
                    "requirements of storage connectivity group '%(scg)s', "
                    "so the group's use is restricted. The member hosts "
                    "checked for connectivity were: %(hosts)s.")
            msg_dict['messages'].append(msg % dict(scg=self.display_name,
                                                   hosts=hosts_wout))
        return hosts_with_conn

    def get_host_name_list(self):
        """
        Return a list of host names in the SCG DOM passed. The SCG must have
        been looked up by a utility that populated it's dictionary rep in the
        context of a db session.
        """
        return [host['name'] for host in self.to_dict()['host_list']]
##########################################################
#  END: SCG DOM methods, migrated from "storage.olddom"  #
##########################################################


######################################################
#######  Fibre Channel Port DOM Implementation  ######
######################################################
class FcPortFactory(dom.ResourceFactory):
    """Production DOM Factory implementation for FC Ports"""

    def __init__(self):
        """ Call super constructor and set class vars """
        super(FcPortFactory, self).__init__()
        self.fabrics = {}
        self.fabric_sync_time = None
        self.max_sync_mins = CONF.ibmpowervc_fabric_sync_interval

    ###############################################
    ## FC fabric helper methods. Fabric info is  ##
    ## not persisted in the nova DB, but the DOM ##
    ## is extended to support this information   ##
    ###############################################
    def sync_port_to_fabrics_needed(self):
        """ Returns True if max time has elapsed and a general sync of
            of port entries to fabric entries is needed. Also resets the
            sync time to now. Else return False.
        """
        LOG.debug("self.fabric_sync_time=%s, self.max_sync_mins=%d" %
                  (str(self.fabric_sync_time), self.max_sync_mins))
        if self.fabric_sync_time is None:
            return True
        delta = datetime.now() - self.fabric_sync_time
        if delta > timedelta(0, self.max_sync_mins * 60):
            return True
        return False

    def sync_fcports_to_fabric(self, context, transaction, wwpn_to_dom):
        """
        Synchronize the fcport.fabric property to a registered fabric.
        :param context: The API and DB authorization context.
        :param transaction: The current DB transaction object.
        :param wwpn_to_dom: Dictionary mapping of wwpn string to FCPort DOM.
        """
        # Get the internal API (cinder client for nova)
        fabricAPI = PowerVCFabricAPI()

        # Always pay the expense of getting the current registered fabrics
        LOG.debug("Get registered fabric information from cinder.")
        cur_fabs = fabricAPI.get_fabrics(context)
        pri_fabs = self.fabrics  # prior tracked fabrics
        LOG.debug("cur_fabs=%s,\n prior_fabs=%s" %
                  (cur_fabs, self.fabrics))

        # We want to do the relatively more expensive work of mapping ports to
        # fabrics in these situations:
        # 1. There are registered fabrics and the last time the port-to-fabric
        #    sychronization was exceeds the configured time interval. This
        #    covers rare cases where a port is cabled up or cables are
        #    switched sometime after fabric registration.
        # 2. One or more new fabrics have been registered since the last check.
        # 3. Fabric A is currently registered and was registered in the last
        #    check but the IP has changed (effectively a new registration).
        # 4. Fabric B is currently registered and was registered in the last
        #    check but the IP has changed (effectively a new registration).
        if (cur_fabs and self.sync_port_to_fabrics_needed()) or\
                (len(cur_fabs) > len(pri_fabs)) or\
                ('A' in cur_fabs and 'A' in pri_fabs and
                 cur_fabs['A']['access_ip'] != pri_fabs['A']['access_ip']) or\
                ('B' in cur_fabs and 'B' in pri_fabs and
                 cur_fabs['B']['access_ip'] != pri_fabs['B']['access_ip']):
            # Now try to map the wwpns to any detected changed fabric setting.
            # The fabric information that comes back on the prior call does
            # not include any fabric health status, so a switch problem
            # would show up in not getting data back from the next call.
            port_mapping = fabricAPI.map_ports_to_fabrics(context,
                                                          wwpn_to_dom.keys())
            self.fabric_sync_time = datetime.now()
            if port_mapping:
                for wwpn, fabric in port_mapping.iteritems():
                    # Since there could be issues getting the fabric data,
                    # we never map a port to "None", only if it is a valid
                    # fabric and if it has changed from the current setting.
                    if fabric and fabric != wwpn_to_dom[wwpn].fabric:
                        LOG.info(_("Setting FC Port with WWPN '%(wwpn)s' to "
                                   "fabric '%(to_fabric)s' from prior value "
                                   "of '%(from_fabric)s'.") %
                                 dict(wwpn=wwpn, to_fabric=fabric,
                                      from_fabric=wwpn_to_dom[wwpn].fabric))
                        wwpn_to_dom[wwpn].fabric = fabric
                        wwpn_to_dom[wwpn].save(context=context,
                                               transaction=transaction)
                    else:
                        LOG.debug("WWPN %s fabric is unknown or unchanged: "
                                  "%s." % (wwpn, wwpn_to_dom[wwpn].fabric))
            else:
                LOG.warn(_("Unable to get mapped WWPN information from "
                           "fabric switches."))
        else:
            LOG.debug("FC switch fabric registration criteria not met for "
                      "syncing ports to fabrics.")
        self.fabrics = cur_fabs

    ###############################################
    ###### Defining FCPort Factory Interface ######
    ###############################################
    def construct_fcport(self, context, name, vios_id, id=None,
                         wwpn=None, status=None, enabled=None, port_tag=None,
                         fabric=None, adapter_id=None, auto_create=False,
                         transaction=None):
        """Constructs (and optionally creates) the SCG specified"""
        if id is None:
            id = str(uuid.uuid4())
        kwargs = dict(name=name, vios_id=vios_id, id=id, wwpn=wwpn,
                      status=status, enabled=enabled, port_tag=port_tag,
                      fabric=fabric, adapter_id=adapter_id)
        #Call the Parent Class to actually Construct the given Resource
        return self.construct_resource(FcPort, context, auto_create,
                                       transaction, **kwargs)

    def find_all_fcports(self, context, filters=None, transaction=None):
        """Queries all of the FCPorts matching the filter specified"""
        return self.find_all_resources(
            FcPort, context, filters, transaction)

    def find_all_fcports_by_vios(self, context, vios_id, transaction=None):
        """Queries all of the FCPorts matching the filter specified"""
        vios_ref_key = compute_dom.VioServerAssociate.get_vios_ref_attr_name()
        return self.find_all_resources(
            FcPort, context, {vios_ref_key: vios_id}, transaction)

    def find_all_fcports_by_host(self, context, host_name, transaction=None):
        """Queries all of the FCPorts matching the filter specified"""
        return self.find_all_resources(
            FcPort, context, dict(host=host_name), transaction)

    def find_fcport_by_id(self, context, port_id, transaction=None):
        """Queries at most one FCPort matching the Port ID specified"""
        return self.find_resource(FcPort, context,
                                  dict(id=port_id), transaction)

    def delete_fcport(self, context, port_id, transaction=None):
        """Deletes the FCPort with the Port ID specified"""
        return self.delete_resource(
            FcPort, context, port_id, transaction)

    ###############################################
    ### Implementing Resource Factory Definition ##
    ###############################################
    def _get_resource_classes(self):
        """Returns a List of DOM Classes or a Dictionary Mapping DOM Classes"""
        return [FcPort]

    @base.remotable
    def _find_all_resources(self, context, resource_class,
                            filters=None, transaction=None):
        """Factory implementation to query all resources matching the filter"""
        fcports = db_api.fcport_find_all(context, filters)
        return self._construct_resources(
            resource_class, context, fcports, transaction)

    @base.remotable
    def _delete_multiple_resources(self, context, resource_class,
                                   resource_ids, transaction=None):
        """Factory implementation to delete the resources specified"""
        for resource_id in resource_ids:
            db_api.fcport_delete(context, resource_id)


class FcPort(compute_dom.VioServerAssociate):
    """Production DOM Resource implementation for FC Ports"""

    fields = dict(compute_dom.VioServerAssociate.fields,
                  name=fields.StringField(nullable=True),
                  status=fields.StringField(nullable=True),
                  enabled=fields.BooleanField(nullable=True),
                  wwpn=fields.StringField(nullable=True),
                  adapter_id=fields.StringField(nullable=True),
                  port_tag=fields.StringField(nullable=True),
                  fabric=fields.StringField(nullable=True))

    obj_extra_fields = ['vios_list']

    ###############################################
    ##### Implementing DOM Resource Definition ####
    ###############################################
#    def __init__(self, context=None, factory_class=None, transaction=None,
#                 **kwargs):
#        "Constructs an instance of the given DOM Resource Class"
#        super(FcPort, self).__init__(context, factory_class, transaction,
#                                     **kwargs)
#        self.id = kwargs.get('id', str(uuid.uuid4()))
#        self.vio_server = kwargs.get('vio_server', None)
#        self.adapter_id = kwargs.get('adapter_id', None)

    def _initialize_resource_attributes(self):
        """Sets any default values that are defined for any attributes"""
        self._vioserver = None

    def _reset_cached_attributes(self):
        """Wipes out any Cached Resource Attributes that are no longer valid"""
        self._vioserver = None

    @base.remotable
    def _create_resource(self, context, transaction):
        """Actual Implementation that will Create the given Resource"""
        values = self._get_resource_values()
        fcport = db_api.fcport_create(context, values)
        self._hydrate_all_resource_attributes(fcport)

    @base.remotable
    def _update_resource(self, context, transaction):
        """Actual Implementation that will Update the given Resource"""
        fcport_id = self._get_resource_identifer()
        values = self._get_resource_values(changes_only=True)
        fcport = db_api.fcport_update(context, fcport_id, values)
        self._hydrate_all_resource_attributes(fcport)

    ###############################################
    #########  Defining FCPort Interface  #########
    ###############################################
    @property
    def vios_factory(self, factory_class=None):
        """The appropriate type of VioServerFactory for the VA subtype.
        """
        return VioServerFactory.get_factory(factory_class)

    #############################################
    ####  VioServerAssociate: Class methods  ####
    #############################################
    @classmethod
    def get_resource_id_attribute(cls):
        """Provides the Name of the Attribute for the Resource Identifier"""
        return 'adapter_id'


######################################################
#####  Host Storage Topology DOM Implementation  #####
######################################################
class HostStorageTopologyFactory(dom.ResourceFactory):
    """Production DOM Factory implementation for Host Storage Topology"""

    ###############################################
    ## Cluster membership tracked info.           #
    ## This will likely move away from the        #
    ## factory once HostStorageTopology instances #
    ## come into use.                             #
    ###############################################
    def __init__(self):
        self.static_topo = StaticStgTopo()

    def get_static_topology(self):
        return self.static_topo

    ###############################################
    ######  Defining HST Factory Interface  #######
    ###############################################
    def construct_storage_topology(self, context, host_name, transaction=None):
        """Constructs the Storage Topology for the Host specified"""
        #Call the Parent Class to actually Construct the given Resource
        return self.construct_resource(HostStorageTopology, context, False,
                                       transaction, host_name=host_name)

    def find_storage_topology(self, context, host_name, transaction=None):
        """Queries the Storage Topology for the Host specified"""
        return self.find_resource(HostStorageTopology, context,
                                  dict(host_name=host_name), transaction)

    ###############################################
    ### Implementing Resource Factory Definition ##
    ###############################################
    def _get_resource_classes(self):
        """Returns a List of DOM Classes or a Dictionary Mapping DOM Classes"""
        return [HostStorageTopology]

    @base.remotable
    def _find_all_resources(self, context, resource_class,
                            filters=None, transaction=None):
        """Factory implementation to query all resources matching the filter"""
        topo = dict()
        host = filters.get('host_name', '')
        #Construct each of the Factories used to retrieve the info
        portFact = FcPortFactory.getFactory()
        viosFact = VioServerFactory.getFactory()
        #Retrieve the VIOS's, FCPorts, and VIOS Clusters for the given Host
        topo['vioservers'] = viosFact.find_all_vioses_by_host(context, host)
        topo['fcports'] = portFact.find_all_fcports_by_host(context, host)
        return self._construct_resources(
            resource_class, context, [topo], transaction)


class HostStorageTopology(dom.Resource):
    """Production DOM Resource implementation for Host Storage Topology"""

    fields = dict(dom.Resource.fields,
                  host_name=fields.StringField(),
                  vioservers=fields.ListOfObjectsField(dom.Resource),
                  fcports=fields.ListOfObjectsField(dom.Resource))

    ###############################################
    ##### Implementing DOM Resource Definition ####
    ###############################################
    def _get_resource_id_attribute(self):
        """Provides the Name of the Attribute for the Resource Identifier"""
        return 'host_name'

    def _initialize_resource_attributes(self):
        """Sets any default values that are defined for any attributes"""
        setattr(self, 'vioservers', list())
        setattr(self, 'fcports', list())

    def _create_resource(self, context, transaction):
        """Actual Implementation that will Create the given Resource"""
        #Since the Topology isn't actually persisted we will treat
        #someone who queried the Topology first the same as construct it
        super(HostStorageTopology, self)._update_resource(context, transaction)

    @base.remotable
    def _update_resource(self, context, transaction):
        """Actual Implementation that will Update the given Resource"""
        #Update the Cluster Provider Name on each of the VIO Servers
        for vios in self.vioservers:
            vios_id = vios._get_resource_identifer()
            values = vios._get_resource_values(changes_only=True)
            db_api.vios_update(self._context, vios_id, values)
        #Retrieve the existing FCPorts for the Host to figure out differences
        dbps = db_api.fcport_find_all(self._context, dict(host=self.host_name))
        exist_ports = dict([(port['id'], port) for port in dbps])
        new_ports = dict([(port['id'], port) for port in self.fcports])
        #Determine whether the Port needs to be Created or updated
        for port in new_ports:
            #If the Port already exists, we just want to update what changed
            if exist_ports.get(port['id']) is not None:
                values = port._get_resource_values(changes_only=True)
                db_api.fcport_update(self._context, port['id'], values)
            #If the Port is new, then we want to create it in the database
            else:
                values = port._get_resource_values(changes_only=False)
                db_api.fcport_create(self._context, values)
        #Now we want to clean up any ports that no longer exist in the topology
        for port in exist_ports:
            if new_ports.get(port['id']) is None:
                db_api.fcport_delete(self._context, port['id'])


##################################################
####  Functions migrated from storage.olddom  ####
##################################################
def fcport_to_dict(db_fcport):
    """
    Convenience method to take a FC Port entry DTO and return a dict
    representation of the DTO. When FCPorts are created and looked up by
    their own DOM above, then this logic can move to the DOM's to_dict().
    """
    fcport_dict = {'id': db_fcport.id,
                   'adapter_id': db_fcport.adapter_id,
                   'wwpn': db_fcport.wwpn,
                   'name': db_fcport.name,
                   'enabled': db_fcport.enabled,
                   'fabric': db_fcport.fabric}
    if db_fcport.vio_server is not None:
        fcport_dict['vio_server'] = db_fcport.vio_server.lpar_name
    if db_fcport.port_tag:
        fcport_dict['port_tag'] = db_fcport.port_tag
    if db_fcport.status:
        # if status is "OK:...", get also get the number of available
        # virtual adapter connections that remain for the port. There
        # is not a separate field for this - likely to add in a follow-on
        # release.
        parts = str(db_fcport.status).split(":")
        if len(parts) > 1:
            fcport_dict['status'] = parts[0]
            fcport_dict['available_connections'] = int(parts[1])
        else:
            fcport_dict['status'] = str(db_fcport.status)
            fcport_dict['available_connections'] = 0
    else:
        fcport_dict['status'] = "[unknown]"
        fcport_dict['available_connections'] = 0
    return fcport_dict


######################################################
##### Storage DOM Resource Factory Registration ######
######################################################
dom.ResourceFactoryLocator.register_factory(FcPortFactory())
dom.ResourceFactoryLocator.register_factory(VioServerFactory())
dom.ResourceFactoryLocator.register_factory(ManagedHostFactory())
dom.ResourceFactoryLocator.register_factory(StorageProviderFactory())
dom.ResourceFactoryLocator.register_factory(HostStorageTopologyFactory())
dom.ResourceFactoryLocator.register_factory(StorageConnectivityGroupFactory())
