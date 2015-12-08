#
# =================================================================
# =================================================================

import ConfigParser
from nova.db import api as db_api
from nova.objects import base
from nova.objects import fields
from nova.openstack.common import log as logging

from paxes_nova.db import api as powervc_db_api
from paxes_nova.objects import dom

LOG = logging.getLogger(__name__)


######################################################
#######  Management Console DOM Implementation  ######
######################################################
class ManagementConsoleFactory(dom.ResourceFactory):
    """Production DOM Resource Factory class implementation for HMC's"""

    ###############################################
    ######  Defining HMC Factory Interface  #######
    ###############################################
    def construct_hmc(self, context, hmc_uuid, access_ip,
                      user_id, password, hmc_display_name,
                      auto_create=False, transaction=None):
        """Constructs (and optionally creates) the HMC specified"""
        kwargs = dict(hmc_uuid=hmc_uuid, access_ip=access_ip, user_id=user_id,
                      password=password, hmc_display_name=hmc_display_name)
        #Call the Parent Class to actually Construct the given Resource
        return self.construct_resource(
            ManagementConsole, context, auto_create, transaction, **kwargs)

    def find_all_hmcs(self, context, filters=None, transaction=None):
        """Queries at most one HMC matching the HMC UUID specified"""
        return self.find_all_resources(
            ManagementConsole, context, filters, transaction)

    def find_all_hmcs_by_host(self, context, host_name, transaction=None):
        """Queries all HMC's that are managing the Host specified"""
        hmcs = powervc_db_api.hmc_find_all(context, dict(host=host_name))
        return self._construct_resources(
            ManagementConsole, context, hmcs, transaction)

    def find_hmc_by_uuid(self, context, hmc_uuid, transaction=None):
        """Queries at most one HMC matching the HMC UUID specified"""
        return self.find_resource(ManagementConsole, context,
                                  dict(hmc_uuid=hmc_uuid), transaction)

    def delete_hmc(self, context, hmc_uuid, transaction=None):
        """Deletes the HMC with the HMC UUID specified"""
        return self.delete_resource(
            ManagementConsole, context, hmc_uuid, transaction)

    ###############################################
    ### Implementing Resource Factory Definition ##
    ###############################################
    def _get_resource_classes(self):
        """Returns a List of DOM Classes or a Dictionary Mapping DOM Classes"""
        return [ManagementConsole]

    @base.remotable
    def _find_all_resources(self, context, resource_class,
                            filters=None, transaction=None):
        """Factory implementation to query all resources matching the filter"""
        hmcs = powervc_db_api.hmc_find_all(context, filters)
        return self._construct_resources(
            resource_class, context, hmcs, transaction)

    @base.remotable
    def _delete_multiple_resources(self, context, resource_class,
                                   resource_ids, transaction=None):
        """Factory implementation to delete the resources specified"""
        for resource_id in resource_ids:
            powervc_db_api.hmc_delete(context, resource_id)


class ManagementConsole(dom.Resource):
    """Production DOM Resource class implementation for HMC's"""

    fields = dict(dom.Resource.fields,
                  hmc_uuid=fields.StringField(),
                  hmc_display_name=fields.StringField(),
                  access_ip=fields.StringField(),
                  user_id=fields.StringField(),
                  password=fields.StringField(),
                  registered_at=fields.DateTimeField(nullable=True),
                  managed_hosts=fields.ListOfStringsField(nullable=True))

    ###############################################
    ##### Implementing DOM Resource Definition ####
    ###############################################
    def _get_resource_id_attribute(self):
        """Provides the Name of the Attribute for the Resource Identifier"""
        return 'hmc_uuid'

    def _initialize_resource_attributes(self):
        """Sets any default values that are defined for any attributes"""
        setattr(self, 'managed_hosts', list())

    @base.remotable
    def _create_resource(self, context, transaction):
        """Actual Implementation that will Create the given Resource"""
        values = self._get_resource_values()
        hmc = powervc_db_api.hmc_create(context, values)
        self._hydrate_all_resource_attributes(hmc)

    @base.remotable
    def _update_resource(self, context, transaction):
        """Actual Implementation that will Update the given Resource"""
        hmc_uuid = self._get_resource_identifer()
        values = self._get_resource_values(changes_only=True)
        hmc = powervc_db_api.hmc_update(context, hmc_uuid, values)
        self._hydrate_all_resource_attributes(hmc)


######################################################
#########   Managed Host DOM Implementation   ########
######################################################
class ManagedHostFactory(dom.ResourceFactory):
    """Production DOM Resource Factory class implementation for Hosts"""

    ###############################################
    ######  Defining Host Factory Interface  ######
    ###############################################
    def find_all_hosts(self, context, filters=None, transaction=None):
        """Queries all Host that are registered on the Management System"""
        return self.find_all_resources(
            ManagedHost, context, filters, transaction)

    def find_host_by_name(self, context, host_name, transaction=None):
        """Queries at most one Host matching the Host Name specified"""
        return self.find_resource(ManagedHost, context,
                                  dict(host_name=host_name), transaction)

    def delete_host(self, context, host_name, transaction=None):
        """Deletes the Host with the Host Name specified"""
        return self.delete_resource(
            ManagedHost, context, host_name, transaction)

    ###############################################
    ### Implementing Resource Factory Definition ##
    ###############################################
    def _get_resource_classes(self):
        """Returns a List of DOM Classes or a Dictionary Mapping DOM Classes"""
        return [ManagedHost]

    @base.remotable
    def _find_all_resources(self, context, resource_class,
                            filters=None, transaction=None):
        """Factory implementation to query all resources matching the filter"""
        hosts = list()
        services = db_api.service_get_all(context)
        #Since the topic/host filter excludes disabled, filter ourselves
        services = [svc for svc in services if svc['topic'] == 'compute']
        #If they asked to filter on Host Name, then we need something special
        if filters is not None and filters.get('host_name') is not None:
            host_name = filters['host_name']
            services = [svc for svc in services if svc['host'] == host_name]
        #Now add in all the attributes for the given Compute Service
        for service in services:
            host = dict(host_name=service['host'],
                        registered_at=service['created_at'])
            #Populate extra properties from the DB Table and CONF file
            self._populate_from_compute_node(context, host, service['id'])
            self._populate_from_conf_file(context, host, service['host'])
            #Retrieve the list of HMC UUID's from the Database Table
            hmcfact = ManagementConsoleFactory.get_factory()
            hmcs = hmcfact.find_all_hmcs_by_host(context, service['host'])
            host['hmc_uuids'] = [hmc['hmc_uuid'] for hmc in hmcs]
            hosts.append(host)
        #Convert the Compute Node instances to the given DOM Resource instance
        return self._construct_resources(
            resource_class, context, hosts, transaction)

    @base.remotable
    def _delete_multiple_resources(self, context, resource_class,
                                   resource_ids, transaction=None):
        """Factory implementation to delete the resources specified"""
        for resource_id in resource_ids:
            powervc_db_api.host_delete(context, resource_id)

    ###############################################
    ######   Shared Host DOM Helper Methods  ######
    ###############################################
    def _populate_from_compute_node(self, context, host, service_id):
        """Helper method to retrieve the Compute Node for a given Host Name"""
        attrs = ['hypervisor_hostname', 'hypervisor_version']
        try:
            #Query the Compute Node from the DB to get the attributes
            node = db_api.compute_node_get_by_service_id(context, service_id)
            for attr in attrs:
                host[attr] = node[attr]
        #If we got an exception retrieving the Host, it wasn't found
        except Exception:
            pass

    def _populate_from_conf_file(self, context, host, host_name):
        """Helper method to retrieve the Compute Node for a given Host Name"""
        attrs = ['host_type', 'host_display_name',
                 'host_storage_type', 'host_uniqueid']
        try:
            #Query the Nova CONF file for the Host to get the attributes
            parser = ConfigParser.RawConfigParser()
            parser.read('/etc/nova/nova-' + host_name + '.conf')
            for attr in attrs:
                if parser.has_option('DEFAULT', attr):
                    host[attr] = parser.get('DEFAULT', attr)
        #If we got an exception retrieving the Host, it wasn't found
        except Exception:
            pass


class ManagedHost(dom.Resource):
    """Production DOM Resource class implementation for Hosts"""

    fields = dict(dom.Resource.fields,
                  host_name=fields.StringField(),
                  host_type=fields.StringField(nullable=True),
                  host_display_name=fields.StringField(nullable=True),
                  host_storage_type=fields.StringField(nullable=True),
                  host_uniqueid=fields.StringField(nullable=True),
                  hypervisor_hostname=fields.StringField(nullable=True),
                  hypervisor_version=fields.IntegerField(nullable=True),
                  hmc_uuids=fields.ListOfStringsField(nullable=True),
                  registered_at=fields.DateTimeField(nullable=True))

    obj_extra_fields = ['vioservers', 'hmcs']
    _supported_host_types = None

    ###############################################
    ##### Implementing DOM Resource Definition ####
    ###############################################
    def _get_resource_id_attribute(self):
        """Provides the Name of the Attribute for the Resource Identifier"""
        return 'host_name'

    def _initialize_resource_attributes(self):
        """Sets any default values that are defined for any attributes"""
        self._vioservers, self._hmcs = (None, None)

    def _reset_cached_attributes(self):
        """Wipes out any Cached Resource Attributes that are no longer valid"""
        self._vioservers, self._hmcs = (None, None)

    ###############################################
    ######  Defining Managed Host Interface  ######
    ###############################################
    @staticmethod
    def get_supported_host_types():
        """Helper method to determine what types of Hosts are supported"""
        #For performance we will only retrieve the Host Type once
        if ManagedHost._supported_host_types is None:
            host_type = None
            parser = ConfigParser.RawConfigParser()
            parser.read('/etc/nova/nova.conf')
            #Use the PowerVM-specific property for now
            if parser.has_option('DEFAULT', 'powervm_mgr_type'):
                host_type = parser.get('DEFAULT', 'powervm_mgr_type')
            #Default to IVM for now if the property is not set
            ManagedHost._supported_host_types =\
                [host_type] if host_type else ['ivm']
        return ManagedHost._supported_host_types

    @property
    def vioservers(self):
        """Getter Method for the list of VIOS's for this Host"""
        #If the VIOS's haven't been loaded yet, load them and cache them
        if self._vioservers is None:
            fact = VioServerFactory.get_factory()
            #Call the VioServer Factory to retrieve the list of VIO Servers
            self._vioservers = \
                fact.find_all_vioses_by_host(self._context, self.host_name)
        return self._vioservers

    @property
    def hmcs(self):
        """Getter Method for the list of HMC's for this Host"""
        #If the HMC's haven't been loaded yet, load them and cache them
        if self._hmcs is None:
            self._hmcs = list()
            #Only bother to do the lookup if there is a HMC UUID set
            if self.hmc_uuids:
                fact = ManagementConsoleFactory.get_factory()
                #Loop through each of the HMC UUID's, looking up each one
                for hmc_uuid in self.hmc_uuids:
                    #Call the Factory to retrieve the HMC specified
                    self._hmcs.append(
                        fact.find_hmc_by_uuid(self._context, hmc_uuid))
        return self._hmcs


######################################################
#######   Managed Instance DOM Implementation   ######
######################################################
class ManagedInstanceFactory(dom.ResourceFactory):
    """Production DOM Resource Factory class implementation for Instances"""

    ###############################################
    ##### Defining Instance Factory Interface #####
    ###############################################
    def find_all_instances(self, context, filters=None, transaction=None):
        """Queries all Instance on the given  specified"""
        return self.find_all_resources(
            ManagedInstance, context, filters, transaction)

    def find_all_instances_by_host(self, context, host_name, transaction=None):
        """Queries all Instances that exist on the Host specified"""
        return self.find_all_resources(ManagedInstance, context,
                                       dict(host=host_name), transaction)

    def find_instance_by_uuid(self, context, instance_uuid, transaction=None):
        """Queries at most one Instance matching the UUID specified"""
        return self.find_resource(ManagedInstance, context,
                                  dict(uuid=instance_uuid), transaction)

    def delete_instance(self, context, instance_uuid, transaction=None):
        """Deletes the Instance with the UUID specified"""
        return self.delete_resource(
            ManagedInstance, context, instance_uuid, transaction)

    ###############################################
    ### Implementing Resource Factory Definition ##
    ###############################################
    def _get_resource_classes(self):
        """Returns a List of DOM Classes or a Dictionary Mapping DOM Classes"""
        return [ManagedInstance]

    @base.remotable
    def _find_all_resources(self, context, resource_class,
                            filters=None, transaction=None):
        """Factory implementation to query all resources matching the filter"""
        instances = powervc_db_api.instance_find_all(context, filters)
        return self._construct_resources(
            resource_class, context, instances, transaction)

    @base.remotable
    def _delete_multiple_resources(self, context, resource_class,
                                   resource_ids, transaction=None):
        """Factory implementation to delete the resources specified"""
        for resource_id in resource_ids:
            powervc_db_api.instance_delete(context, resource_id)


class ManagedInstance(dom.Resource):
    """Production DOM Resource class implementation for Instances"""

    fields = dict(dom.Resource.fields,
                  id=fields.IntegerField(nullable=True),
                  uuid=fields.UUIDField(),
                  name=fields.StringField(nullable=True),
                  display_name=fields.StringField(nullable=True),
                  display_description=fields.StringField(default=''),
                  host=fields.StringField(nullable=True),
                  node=fields.StringField(nullable=True),
                  hostname=fields.StringField(nullable=True),
                  vm_state=fields.StringField(nullable=True),
                  power_state=fields.IntegerField(nullable=True),
                  task_state=fields.StringField(nullable=True),
                  memory_mb=fields.IntegerField(nullable=True),
                  vcpus=fields.IntegerField(nullable=True),
                  root_gb=fields.IntegerField(nullable=True),
                  ephemeral_gb=fields.IntegerField(nullable=True),
                  os_type=fields.StringField(nullable=True),
                  architecture=fields.StringField(nullable=True),
                  vm_mode=fields.StringField(nullable=True),
                  root_device_name=fields.StringField(nullable=True),
                  default_ephemeral_device=fields.StringField(nullable=True),
                  default_swap_device=fields.StringField(nullable=True),
                  config_drive=fields.StringField(default=''),
                  config_drive_id=fields.StringField(default=''),
                  kernel_id=fields.StringField(default=''),
                  ramdisk_id=fields.StringField(default=''),
                  access_ip_v4=fields.IPV4AddressField(nullable=True),
                  access_ip_v6=fields.IPV6AddressField(nullable=True),
                  user_id=fields.StringField(nullable=True),
                  project_id=fields.StringField(nullable=True),
                  availability_zone=fields.StringField(nullable=True),
                  image_ref=fields.StringField(nullable=True),
                  instance_type_id=fields.IntegerField(nullable=True),
                  reservation_id=fields.StringField(nullable=True),
                  key_name=fields.StringField(nullable=True),
                  key_data=fields.StringField(nullable=True),
                  user_data=fields.StringField(nullable=True),
                  created_at=fields.DateTimeField(nullable=True),
                  updated_at=fields.DateTimeField(nullable=True),
                  deleted_at=fields.DateTimeField(nullable=True),
                  scheduled_at=fields.DateTimeField(nullable=True),
                  launched_at=fields.DateTimeField(nullable=True),
                  terminated_at=fields.DateTimeField(nullable=True),
                  progress=fields.IntegerField(nullable=True),
                  locked=fields.BooleanField(nullable=True),
                  metadata=dom.MetaDataDictField(nullable=True),
                  system_metadata=dom.MetaDataDictField(nullable=True),
                  info_cache=dom.InfoCacheDictField(nullable=True),
                  power_specs=dom.DictOfAnyTypeField(nullable=True))

    ###############################################
    ##### Implementing DOM Resource Definition ####
    ###############################################
    def _get_resource_id_attribute(self):
        """Provides the Name of the Attribute for the Resource Identifier"""
        return 'uuid'

    @base.remotable
    def _create_resource(self, context, transaction):
        """Actual Implementation that will Create the given Resource"""
        values = self._get_resource_values()
        values.pop('name', None)
        values['info_cache'] = {'network_info': '[]'}
        instance = powervc_db_api.instance_create(context, values)
        self._hydrate_all_resource_attributes(instance)

    @base.remotable
    def _update_resource(self, context, transaction):
        """Actual Implementation that will Update the given Resource"""
        inst_uuid = self._get_resource_identifer()
        values = self._get_resource_values(changes_only=True)
        values.pop('name', None)
        instance = powervc_db_api.instance_update(context, inst_uuid, values)
        self._hydrate_all_resource_attributes(instance)


class HostAssociate(dom.Resource):
    """Production DOM Resource class implementation for VIOS's"""

    fields = dict(dom.Resource.fields,
                  host_name=fields.StringField())


######################################################
##########   VIO Server DOM Implementation   #########
######################################################
class VioServerFactory(dom.ResourceFactory):
    """Production DOM Resource Factory class implementation for VIOS's"""

    ###############################################
    ######  Defining VIOS Factory Interface  ######
    ###############################################
    def construct_vios(self, context, host_name, lpar_id,
                       lpar_name, state, rmc_state, cluster_provider_name=None,
                       auto_create=False, transaction=None):
        """Constructs (and optionally creates) the VIOS specified"""
        kwargs = dict(host_name=host_name,
                      lpar_id=lpar_id, lpar_name=lpar_name,
                      state=state, rmc_state=rmc_state,
                      cluster_provider_name=cluster_provider_name)
        #Call the Parent Class to actually Construct the given Resource
        return self.construct_resource(
            VioServer, context, auto_create, transaction, **kwargs)

    def find_all_vioses(self, context, filters=None, transaction=None):
        """Queries all VIOS's matching the filters specified"""
        return self.find_all_resources(
            VioServer, context, filters, transaction)

    def find_all_vioses_by_host(self, context, host_name, transaction=None):
        """Queries all VIOS's that are part of the host specified"""
        if host_name is None:
            filters = None
        else:
            filters = dict(host_name=host_name)
        return self.find_all_resources(VioServer, context, filters=filters,
                                       transaction=transaction)

    def find_vios_by_id(self, context, vios_id, transaction=None):
        """Returns VIOS matching the given ID if found, else None."""
        if not vios_id:
            return None
        # TODO 6 Find_by_id: (1) Move up to Resrc needs 'id' attr fr. class
        # TODO 6 Find_by_id: (2) Moving VIOS 'id' may affect db_api filter map
        return self.find_resource(VioServer, context,
                                  {VioServer.get_id_attr_name(): vios_id},
                                  transaction)

    def delete_vios(self, context, vios_id, transaction=None):
        """Deletes the VIOS with the Host Name + LPAR ID specified"""
        return self.delete_resource(
            VioServer, context, vios_id, transaction)

    ###############################################
    ### Implementing Resource Factory Definition ##
    ###############################################
    def _get_resource_classes(self):
        """Returns a List of DOM Classes or a Dictionary Mapping DOM Classes"""
        return [VioServer]

    @base.remotable
    def _find_all_resources(self, context, resource_class,
                            filters=None, transaction=None):
        """Factory implementation to query all resources matching the filter"""
        vioses = powervc_db_api.vios_find_all(context, filters=filters)
        return self._construct_resources(
            resource_class, context, vioses, transaction)

    @base.remotable
    def _delete_multiple_resources(self, context, resource_class,
                                   resource_ids, transaction=None):
        """Factory implementation to delete the resources specified"""
        for resource_id in resource_ids:
            # TODO 5 Delete these as a single group instead of one-by-one?
            powervc_db_api.vios_delete(context, resource_id, transaction)


class VioServer(HostAssociate):
    """Production DOM Resource class implementation for VIOS's"""

    fields = dict(HostAssociate.fields,
                  id=fields.StringField(nullable=True),
                  lpar_id=fields.IntegerField(),
                  lpar_name=fields.StringField(),
                  state=fields.StringField(nullable=True),
                  rmc_state=fields.StringField(nullable=True),
                  cluster_provider_name=fields.StringField(nullable=True))

    ###############################################
    ##### Implementing DOM Resource Definition ####
    ###############################################
    @base.remotable
    def _create_resource(self, context, transaction):
        """Actual Implementation that will Create the given Resource"""
        values = self._get_resource_values()
        vios = powervc_db_api.vios_create(context, values, transaction)
        self._hydrate_all_resource_attributes(vios)

    @base.remotable
    def _update_resource(self, context, transaction):
        """Actual Implementation that will Update the given Resource"""
        vios_id = self._get_resource_identifer()
        values = self._get_resource_values(changes_only=True)
        vios = powervc_db_api.vios_update(context, vios_id, values)
        self._hydrate_all_resource_attributes(vios)

    @classmethod
    def get_resource_id_attribute(cls):
        """Provides the Name of the Attribute for the Resource Identifier"""
        return 'id'


##############################
####  VioServerAssociate  ####
##############################
class VioServerAssociate(dom.Resource):
    """An (abstract) superclass for types that explicitly associate to a VIOS.

    NOTE: This should be an abstract base class (abc), but setting
    "__metaclass__ = abc.ABCMeta" caused a TypeError from the meta-class
    inheritance chain.
    """
    fields = dict(dom.Resource.fields,
                  id=fields.StringField(nullable=True),
                  vios_id=fields.StringField(nullable=False))

    obj_extra_fields = ['vio_server']

    def __init__(self, context=None, factory_class=None, transaction=None,
                 **kwargs):
        "Constructs an instance of the given DOM Resource Class"
        super(VioServerAssociate, self).__init__(context, factory_class,
                                                 transaction, **kwargs)

    @property
    def vios_factory(self, factory_class=None):
        """Subclasses return the appropriate type of VioServerFactory.
        """
        raise dom.ResourceOperationNotSupported(
            operation='vios_factory', class_name=self.__class__.__name__)

    @property
    def vio_server(self):
        """Return the VioServer that is associated to this object."""
        #If the VIOS hasn't been loaded yet, load and cache it
        if self._vioserver is None and self.vios_id is not None:
            # TODO 4 DOM Txn: Need to include this in a DOM transaction?
            self._vioserver = self.vios_factory.find_vios_by_id(self._context,
                                                                self.vios_id)
        return self._vioserver

    @vio_server.setter
    def vio_server(self, viosref):
        """Set the association to the given VIOS, ref'd as either DOM or id.

        :param viosref: Either a VioServer DOM object or a vios_id reference
        """
        if isinstance(viosref, VioServer):
            self._vioserver = viosref
            self.vios_id = self._vioserver.id
        else:
            self._vioserver = None
            self.vios_id = viosref

    #############################################
    ####  VioServerAssociate: Class methods  ####
    #############################################
    @classmethod
    def get_vios_ref_attr_name(cls):
        return 'vios_id'

######################################################
##### Compute DOM Resource Factory Registration ######
######################################################
dom.ResourceFactoryLocator.register_factory(VioServerFactory())
dom.ResourceFactoryLocator.register_factory(ManagedHostFactory())
dom.ResourceFactoryLocator.register_factory(ManagedInstanceFactory())
dom.ResourceFactoryLocator.register_factory(ManagementConsoleFactory())
