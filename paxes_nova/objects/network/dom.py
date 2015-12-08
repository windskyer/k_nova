#
# =================================================================
# =================================================================
import inspect

from nova.objects import base
from nova.objects import fields
from nova.openstack.common import log as logging

from paxes_nova.db import api as db_api
from paxes_nova.objects import dom
from paxes_nova.objects.compute import dom as compute_dom
from paxes_nova.objects.dom import ResourceOperationNotSupported
from paxes_nova.objects.compute.dom import HostAssociate

LOG = logging.getLogger(__name__)


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

    obj_extra_fields = ['seas', 'veas']

    ###############################################
    ##### Implementing DOM Resource Definition ####
    ###############################################
    def _initialize_resource_attributes(self):
        """Sets any default values that are defined for any attributes"""
        self._seas, self._veas = (None, None)

    def _reset_cached_attributes(self):
        """Wipes out any Cached Resource Attributes that are no longer valid"""
        self._seas, self._veas = (None, None)

    ###############################################
    ######  Defining Managed Host Interface  ######
    ###############################################
    @property
    def seas(self):
        """Getter Method for the list of SEA's for this Host"""
        #If the SEA's haven't been loaded yet, load them and cache them
        if self._seas is None:
            self._seas = list()
            #Load the VIO Servers for the Host and get the SEA's there
            for vios in self.vioservers:
                self._seas.append(vios.seas)
        return self._seas

    @property
    def veas(self):
        """Getter Method for the list of VEA's for this Host"""
        #If the VEA's haven't been loaded yet, load them and cache them
        if self._veas is None:
            self._veas = list()
            #Load the VIO Servers for the Host and get the VEA's there
            for vios in self.vioservers:
                self._veas.append(vios.veas)
        return self._veas


######################################################
##########   VIO Server DOM Implementation   #########
######################################################
class VioServerFactory(compute_dom.VioServerFactory):
    """Production DOM Factory implementation for VIOS's"""

    ###############################################
    ### Implementing Resource Factory Definition ##
    ###############################################
    def _get_resource_classes(self):
        """Returns a List of DOM Classes or a Dictionary Mapping DOM Classes"""
        return {compute_dom.VioServer: VioServer}


class VioServer(compute_dom.VioServer):
    """Production DOM Resource implementation for VIO Servers"""

    obj_extra_fields = ['seas', 'veas']

    ###############################################
    ##### Implementing DOM Resource Definition ####
    ###############################################
    def _initialize_resource_attributes(self):
        """Sets any default values that are defined for any attributes"""
        self._seas, self._veas = (None, None)

    def _reset_cached_attributes(self):
        """Wipes out any Cached Resource Attributes that are no longer valid"""
        self._seas, self._veas = (None, None)

    ###############################################
    #######  Defining VIO Server Interface  #######
    ###############################################
    @property
    def sea_list(self):
        """Getter Method for the list of SEA's for this VIOS"""
        #If the SEA's haven't been loaded yet, load them and cache them
        if self._seas is None:
            sea_f = SharedEthernetAdapterFactory.get_factory()
            seas = sea_f.find_all_seas_by_vios(self._context, self.id)
            self._seas = dom.ChangeTrackedList(self._set_sea_list, seas)
        return self._seas

    def _set_sea_list(self, new_sea_list):
        """Getter Method for the list of SEA's for this VIOS"""
        for sea in self._seas:
            if sea not in new_sea_list:
                sea.vio_server = None
        for sea in new_sea_list:
            if sea.vio_server != self:
                sea.vio_server = self
        self._seas = new_sea_list

    @property
    def vea_list(self):
        """Getter Method for the list of VEA's for this VIOS"""
        #If the VEA's haven't been loaded yet, load them and cache them
        if self._veas is None:
            vea_f = VirtualEthernetAdapterFactory.get_factory()
            veas = vea_f.find_all_veas_by_vios(self._context, self.id)
            self._veas = dom.ChangeTrackedList(self._set_vea_list, veas)
        return self._veas

    def _set_vea_list(self, new_vea_list):
        """Getter Method for the list of SEA's for this VIOS"""
        for vea in self._veas:
            if vea not in new_vea_list:
                vea.vio_server = None
        for vea in new_vea_list:
            if vea.vio_server != self:
                vea.vio_server = self
        self._veas = new_vea_list


######################################################
##### Virtual Ethernet Adapter DOM Implementation ####
######################################################
class VirtualEthernetAdapterFactory(dom.ResourceFactory):
    """Production DOM Factory implementation for VEA's"""

    ###############################################
    ######  Defining VEA Factory Interface  #######
    ###############################################
    def construct_vea(self, context, name, vios_id, slot, pvid, is_trunk,
                      trunk_pri, state, ieee_eth, vswitch_name, vlan_ids,
                      id=None, auto_create=False, transaction=None):
        """Constructs (and optionally creates) the VEA's specified"""
        kwargs = dict(name=name, vios_id=vios_id, slot=slot, is_trunk=is_trunk,
                      state=state, trunk_pri=trunk_pri, ieee_eth=ieee_eth,
                      pvid=pvid, vswitch_name=vswitch_name, vlan_ids=vlan_ids,
                      id=id)
        #Call the Parent Class to actually Construct the given Resource
        return self.construct_resource(VirtualEthernetAdapter, context,
                                       auto_create, transaction, **kwargs)

    def find_all_veas(self, context, filters=None, transaction=None):
        """Queries all of the VEA's matching the filter specified"""
        return self.find_all_resources(
            VirtualEthernetAdapter, context, filters, transaction)

    def find_all_veas_by_vios(self, context, vios_id, transaction=None):
        """Queries all of the VEA's matching the filter specified"""
        return self.find_all_resources(VirtualEthernetAdapter, context,
                                       dict(vios_id=vios_id), transaction)

    def find_all_veas_by_host(self, context, host_name, transaction=None):
        """Queries all of the VEA's matching the filter specified"""
        return self.find_all_resources(
            VirtualEthernetAdapter, context, dict(host=host_name), transaction)

    def find_vea_by_id(self, context, vea_id, transaction=None):
        """Queries at most one VEA's matching the VEA ID specified"""
        return self.find_resource(
            VirtualEthernetAdapter, context,
            {VirtualEthernetAdapter.get_id_attr_name(): vea_id}, transaction)

    def delete_vea(self, context, vea_id, transaction=None):
        """Deletes the VEA with the VEA ID specified"""
        return self.delete_resource(
            VirtualEthernetAdapter, context, vea_id, transaction)

    ###############################################
    ### Implementing Resource Factory Definition ##
    ###############################################
    def _get_resource_classes(self):
        """Returns a List of DOM Classes or a Dictionary Mapping DOM Classes"""
        return [VirtualEthernetAdapter]

    @base.remotable
    def _find_all_resources(self, context, resource_class,
                            filters=None, transaction=None):
        """Factory implementation to query all resources matching the filter"""
        veas = db_api.vea_find_all2(context, filters=filters,
                                    transaction=transaction)
        return self._construct_resources(
            resource_class, context, veas, transaction)

    @base.remotable
    def _delete_multiple_resources(self, context, resource_class,
                                   resource_ids, transaction=None):
        """Factory implementation to delete the resources specified"""
        for resource_id in resource_ids:
            db_api.vea_delete(context, resource_id)


class VirtualEthernetAdapter(compute_dom.VioServerAssociate):
    """Production DOM Resource implementation for Virtual Ethernet Adapter"""

    fields = dict(dom.Resource.fields,
                  name=fields.StringField(),
                  status=fields.StringField(nullable=True),
                  slot=fields.IntegerField(nullable=True),
                  pvid=fields.IntegerField(nullable=True),
                  is_trunk=fields.BooleanField(nullable=True),
                  trunk_pri=fields.IntegerField(nullable=True),
                  ieee_eth=fields.BooleanField(nullable=True),
                  vswitch_name=fields.StringField(nullable=True),
                  addl_vlan_ids=fields.ListOfStringsField(default=[]),
                  sea_id=fields.IntegerField(nullable=True))

    obj_extra_fields = ['sea']

    ###############################################
    ##### Implementing DOM Resource Definition ####
    ###############################################
    def __init__(self, context=None, factory_class=None, transaction=None,
                 **kwargs):
        "Constructs an instance of the given DOM Resource Class"
        super(VirtualEthernetAdapter, self).__init__(context, factory_class,
                                                     transaction, **kwargs)

    def _initialize_resource_attributes(self):
        """Sets any default values that are defined for any attributes"""
        self._vioserver, self._sea = (None, None)

    def _reset_cached_attributes(self):
        """Wipes out any Cached Resource Attributes that are no longer valid"""
        self._vioserver, self._sea = (None, None)

    @base.remotable
    def _create_resource(self, context, transaction):
        """Actual Implementation that will Create the given Resource"""
        values = self._get_resource_values()
        vea_dict = db_api.vea_create(context, values)
        self._hydrate_all_resource_attributes(vea_dict)

    @base.remotable
    def _update_resource(self, context, transaction):
        """Actual Implementation that will Update the given Resource"""
        vea_id = self._get_resource_identifer()
        values = self._get_resource_values(changes_only=True)
        vea = db_api.vea_update(context, vea_id, values, transaction)
        self._hydrate_all_resource_attributes(vea)

    @property
    def vios_factory(self, factory_class=None):
        """The appropriate type of VioServerFactory for the VA subtype.
        """
        return VioServerFactory.get_factory(factory_class)

    ###############################################
    ##########  Defining VEA Interface  ###########
    ###############################################
    @property
    def sea(self):
        """Getter Method for the SEA for this VEA"""
        #If the SEA hasn't been loaded yet, load and cache it
        if self._sea is None and self.sea_id:
            fact = SharedEthernetAdapterFactory.get_factory()
            self._sea = fact.find_sea_by_id(self._context, self.sea_id)
        return self._sea

    @sea.setter
    def sea(self, sea):
        """Setting Method for the SEA for this VEA"""
        self.sea_id = None
        self._sea = sea
        #If the SEA wasn't wiped out, update the ID to match
        if sea is not None:
            self.sea_id = sea.id

    @property
    def addl_vlan_ids(self):
        # TODO: 2 Implement DOM VirtualEthernetAdapter.addl_vlan_ids()
        raise NotImplementedError("VirtualEthernetAdapter.addl_vlan_ids(): "
                                  "NOT YET IMPLEMENTED.")


######################################################
##### Shared Ethernet Adapter DOM Implementation #####
######################################################
class SharedEthernetAdapterFactory(dom.ResourceFactory):
    """Production DOM Factory implementation for SEA's"""

    ###############################################
    ######  Defining SEA Factory Interface  #######
    ###############################################
    def construct_sea(self, context, name, vios_id, slot, state,
                      primary_vea_id, control_channel_vea_id=None,
                      additional_vea_ids=None,  auto_create=False,
                      transaction=None):
        """Constructs (and optionally creates) the SEA's specified"""
        kwargs = dict(name=name, vios_id=vios_id, slot=slot,
                      state=state, primary_vea_id=primary_vea_id,
                      control_channel_vea_id=control_channel_vea_id,
                      additional_vea_ids=additional_vea_ids)
        #Call the Parent Class to actually Construct the given Resource
        return self.construct_resource(SharedEthernetAdapter, context,
                                       auto_create, transaction, **kwargs)

    def find_all_seas(self, context, filters=None, transaction=None):
        """Queries all of the SEA's matching the filter specified"""
        return self.find_all_resources(
            SharedEthernetAdapter, context, filters, transaction)

    def find_all_seas_by_vios(self, context, vios_id, transaction=None):
        """Queries all of the SEA's matching the filter specified"""
        return self.find_all_resources(
            SharedEthernetAdapter, context, dict(vios_id=vios_id), transaction)

    def find_all_seas_by_host(self, context, host_name, transaction=None):
        """Queries all of the SEA's matching the filter specified"""
        return self.find_all_resources(
            SharedEthernetAdapter, context, dict(host=host_name), transaction)

    def find_sea_by_id(self, context, sea_id, transaction=None):
        """Queries at most one SEA's matching the SEA ID specified"""
        return self.find_resource(SharedEthernetAdapter, context,
                                  dict(id=sea_id), transaction)

    def delete_sea(self, context, sea_id, transaction=None):
        """Deletes the SEA with the SEA ID specified"""
        return self.delete_resource(
            SharedEthernetAdapter, context, sea_id, transaction)

    ###############################################
    ### Implementing Resource Factory Definition ##
    ###############################################
    def _get_resource_classes(self):
        """Returns a List of DOM Classes or a Dictionary Mapping DOM Classes"""
        return [SharedEthernetAdapter]

    @base.remotable
    def _find_all_resources(self, context, resource_class,
                            filters=None, transaction=None):
        """Factory implementation to query all resources matching the filter"""
        seas = db_api.sea_find_all2(context, filters)
        return self._construct_resources(
            resource_class, context, seas, transaction)

    @base.remotable
    def _delete_multiple_resources(self, context, resource_class,
                                   resource_ids, transaction=None):
        """Factory implementation to delete the resources specified"""
        for resource_id in resource_ids:
            db_api.sea_delete(context, resource_id)


def _set_additional_vea_ids(addl_vea_ids):
    pass


def _set_additional_veas(addl_veas):
    pass


class SharedEthernetAdapter(compute_dom.VioServerAssociate):
    """Production DOM Resource implementation for Shared Ethernet Adapter"""
    fields = dict(dom.Resource.fields,
                  name=fields.StringField(),
                  state=fields.StringField(nullable=True),
                  slot=fields.IntegerField(nullable=True),
                  primary_vea_id=fields.IntegerField(nullable=True),
                  control_channel_vea_id=fields.IntegerField(nullable=True),
                  additional_vea_ids=fields.ListOfStringsField(
                      default=dom.ChangeTrackedList(_set_additional_vea_ids,
                                                    [])))

    obj_extra_fields = ['primary_vea', 'control_channel', 'additional_veas']

    ###############################################
    ##### Implementing DOM Resource Definition ####
    ###############################################
    def __init__(self, context=None, factory_class=None, transaction=None,
                 **kwargs):
        "Constructs an instance of the given DOM Resource Class"
        super(SharedEthernetAdapter, self).__init__(context, factory_class,
                                                    transaction, **kwargs)
#        self._additional_veas = []
        self.additional_veas = dom.ChangeTrackedList(_set_additional_veas,
                                                     [])
        self._removed_vea_id_set = set()

    def _initialize_resource_attributes(self):
        """Sets any default values that are defined for any attributes"""
        self._primary_vea, _additional_veas = (None, None)
        self._vioserver, self._veas = (None, None)
        self._control_channel = None

    def _reset_cached_attributes(self):
        """Wipes out any Cached Resource Attributes that are no longer valid"""
        self._primary_vea, _additional_veas = (None, None)
        self._vioserver, self._veas = (None, None)
        self._control_channel = None

    @base.remotable
    def _create_resource(self, context, transaction):
        """Actual Implementation that will Create the given Resource"""
        values = self._get_resource_values()
        sea = db_api.sea_create(context, values, transaction)
        self._hydrate_all_resource_attributes(sea)

    @base.remotable
    def _update_resource(self, context, transaction):
        """Actual Implementation that will Update the given Resource"""
        sea_id = self._get_resource_identifer()
        values = self._get_resource_values(changes_only=True)
        if values:
            sea = db_api.sea_update(context, sea_id, values, transaction)
            self._hydrate_all_resource_attributes(sea)

    @property
    def vios_factory(self, factory_class=None):
        """The appropriate type of VioServerFactory for the VA subtype.
        """
        return VioServerFactory.get_factory(factory_class)

    ###############################################
    ##########  Defining SEA Interface  ###########
    ###############################################
    @property
    def primary_vea(self):
        """Getter Method for the Primary VEA for this SEA"""
        #If the Primary VEA hasn't been loaded yet, load and cache it
        if self._primary_vea is None and self._primary_vea_id:
            fact = VirtualEthernetAdapterFactory.get_factory()
            self._primary_vea = fact.find_vea_by_id(
                self._context, self._primary_vea_id)
        return self._primary_vea

    @primary_vea.setter
    def primary_vea(self, primary_vea):
        """Setting Method for the Primary VEA for this SEA"""
        self._primary_vea_id = None
        self._primary_vea = primary_vea
        #If the Primary VEA wasn't wiped out, update the ID to match
        if primary_vea is not None:
            self._primary_vea_id = primary_vea.id

    @property
    def additional_veas_OLD(self):
        """Getter Method for the Additional VEA's for this SEA"""
        if self.primary_vea_id in self.additional_vea_ids:
            self.additional_vea_ids.remove(self.primary_vea_id)
        for vea in self._additional_veas:
            if vea.id not in self.additional_vea_ids:
                self._additional_veas.remove(vea)
                vea.sea = None
                self._removed_vea_id_set.add(vea.id)
        vea_ids = [vea.id for vea in self._additional_veas]
        txn = self.get_or_construct_transaction()
        with txn.begin():
            vea_factory = VirtualEthernetAdapterFactory.get_factory()
            for vea_id in self.additional_vea_ids:
                if vea_id not in vea_ids:
                    vea = vea_factory.find_vea_by_id(self._context, vea_id,
                                                     txn)
                    self._additional_veas.append(vea)
                    vea_ids.append(vea.id)
        return self._additional_veas

    @property
    def control_channel(self):
        """Getter Method for the Control Channel for this SEA"""
        #If the Control Channel hasn't been loaded yet, load and cache it
        if self._control_channel is None and self.control_channel_vea_id:
            fact = VirtualEthernetAdapterFactory.get_factory()
            self._control_channel = fact.find_vea_by_id(
                self._context, self.control_channel_vea_id)
        return self._control_channel

    @control_channel.setter
    def control_channel(self, control_channel):
        """Setting Method for the Control Channel for this SEA"""
        self.control_channel_vea_id = None
        self._control_channel = control_channel
        #If the Control Channel wasn't wiped out, update the ID to match
        if control_channel is not None:
            self.control_channel_vea_id = control_channel.id


######################################################
##### NetworkAssociation DOM Implementation ####
######################################################
class NetworkAssociationFactory(dom.ResourceFactory):
    """
    Creates a DOM Network Association object.

    :param host_name: The host that this mapping applies to.  Should be a
                      string that identifies the host.
    :param neutron_net_id: The reference to the Neutron Networks UUID.
                           Should be a string value.
    :param sea: The SharedEthernetAdapter that should be used.  Must reside
                on the vios previously passed in.  If set to None,
                indicates that this association should NOT allow VMs to be
                placed on the Host defined above for the network specified.
    """
    """Production DOM Factory implementation for NetworkAssociations"""

    ###############################################
    ######  Defining VEA Factory Interface  #######
    ###############################################
    def construct_nwassn(self, context, host_name, neutron_network_id,
                         sea=None, auto_create=False, transaction=None):
        """Constructs (and optionally creates) a VEA as specified"""
        if (auto_create and
                self.find_nwassn(context, host_name, neutron_network_id,
                                 transaction) is not None):
            raise ValueError(_("Failed to create a new NetworkAssociation "
                               "for host_name= '" + host_name
                               + "' and neutron_network_id= '"
                               + neutron_network_id
                               + "' because such a NA already exists."))
        kwargs = dict(host_name=host_name,
                      neutron_network_id=neutron_network_id, sea=sea)
        #Call the Parent Class to actually Construct the given Resource
        return self.construct_resource(NetworkAssociation, context,
                                       auto_create, transaction, **kwargs)

    def find_nwassn(self, context, host_name, neutron_network_id,
                    transaction=None):
        """Queries all of the VEA's matching the filter specified"""
        filters = {'host_name': host_name,
                   'neutron_network_id': neutron_network_id}
        return self.find_resource(NetworkAssociation, context, filters,
                                  transaction)

    def find_all_nwassns(self, context, filters=None, transaction=None):
        """Queries all of the VEA's matching the filter specified"""
        return self.find_all_resources(NetworkAssociation, context, filters,
                                       transaction)

    def find_all_nwassns_by_host(self, context, host_name, transaction=None):
        """Queries all of the VEA's matching the filter specified"""
        filters = {'host_name': host_name}
        return self.find_all_resources(NetworkAssociation, context, filters,
                                       transaction)

    def find_all_nwassns_by_network_id(self, context, neutron_network_id,
                                       transaction=None):
        """Queries all of the VEA's matching the filter specified"""
        filters = {'neutron_network_id': neutron_network_id}
        return self.find_all_resources(NetworkAssociation, context, filters,
                                       transaction)

    def delete_vea(self, context, vea_id, transaction=None):
        """Deletes the VEA with the VEA ID specified"""
        raise ResourceOperationNotSupported(operation=inspect.stack()[0][3],
                                            class_name=self.__class__.__name__)
#        return self.delete_resource(
#            VirtualEthernetAdapter, context, vea_id, transaction)

    ###############################################
    ### Implementing Resource Factory Definition ##
    ###############################################
    def _get_resource_classes(self):
        """Returns a List of DOM Classes or a Dictionary Mapping DOM Classes"""
        return [NetworkAssociation]

    @base.remotable
    def _find_all_resources(self, context, resource_class,
                            filters=None, transaction=None):
        """Factory implementation to query all resources matching the filter"""
        nwassns = db_api.network_assoc_find_all(context, filters)
        return self._construct_resources(
            resource_class, context, nwassns, transaction)

    @base.remotable
    def _delete_multiple_resources(self, context, resource_class,
                                   resource_ids, transaction=None):
        """Factory implementation to delete the resources specified"""
        for resource_id in resource_ids:
            db_api.network_assoc_delete(context, resource_id)


class NetworkAssociation(HostAssociate):
    """Production DOM Resource implementation for Virtual Ethernet Adapter"""

    fields = dict(dom.Resource.fields,
                  id=fields.IntegerField(nullable=True),
                  neutron_network_id=fields.StringField(nullable=False),
                  sea_id=fields.IntegerField(nullable=True))

    obj_extra_fields = []

    ###############################################
    ##### Implementing DOM Resource Definition ####
    ###############################################
    def __init__(self, context=None, factory_class=None, transaction=None,
                 **kwargs):
        "Constructs an instance of the given DOM Resource Class"
        super(NetworkAssociation, self).__init__(context, factory_class,
                                                 transaction, **kwargs)
        self._sea = None
        if 'sea' in kwargs and kwargs['sea'] is not None:
            self.sea_id = kwargs['sea'].id

    def _initialize_resource_attributes(self):
        """Sets any default values that are defined for any attributes"""
        super(NetworkAssociation, self)._initialize_resource_attributes()

    def _reset_cached_attributes(self):
        """Wipes out any Cached Resource Attributes that are no longer valid"""
        super(NetworkAssociation, self)._reset_cached_attributes()

    @base.remotable
    def _create_resource(self, context, transaction):
        """Actual Implementation that will Create the given Resource"""
        values = self._get_resource_values()
        nwassn = db_api.network_assoc_create(context, values)
        self._hydrate_all_resource_attributes(nwassn)

    @base.remotable
    def _update_resource(self, context, transaction):
        """Actual Implementation that will Update the given Resource"""
        nwassn_id = self._get_resource_identifer()
        values = self._get_resource_values(changes_only=True)
        nwassn = db_api.network_assoc_update(context, nwassn_id, values,
                                             transaction)
        self._hydrate_all_resource_attributes(nwassn)

    @property
    def sea(self):
        if self._sea is None and self.sea_id is not None:
            sea_factory = SharedEthernetAdapterFactory.get_factory()
            self._sea = sea_factory.find_sea_by_id(self._context, self.sea_id,
                                                   self._transaction)
        return self._sea

    @sea.setter
    def sea(self, new_sea):
        if not new_sea:
            self._sea = None
            self.sea_id = None
        else:
            self._sea = new_sea
            self.sea_id = new_sea.id


######################################################
#####  Host Network Topology DOM Implementation  #####
######################################################
class HostNetworkTopologyFactory(dom.ResourceFactory):
    """Production DOM Factory implementation for Host Network Topology"""

    ###############################################
    ######  Defining HNT Factory Interface  #######
    ###############################################
    def construct_network_topology(self, context, host_name, transaction=None):
        """Constructs the Network Topology for the Host specified"""
        #Call the Parent Class to actually Construct the given Resource
        return self.construct_resource(HostNetworkTopology, context, False,
                                       transaction, host_name=host_name)

    def find_network_topology(self, context, host_name, transaction=None):
        """Queries the Network Topology for the Host specified"""
        return self.find_resource(HostNetworkTopology, context,
                                  dict(host_name=host_name), transaction)

    ###############################################
    ### Implementing Resource Factory Definition ##
    ###############################################
    def _get_resource_classes(self):
        """Returns a List of DOM Classes or a Dictionary Mapping DOM Classes"""
        return [HostNetworkTopology]

    @base.remotable
    def _find_all_resources(self, context, resource_class,
                            filters=None, transaction=None):
        """Factory implementation to query all resources matching the filter"""
        topo = dict()
        host = filters.get('host_name', '')
        #Construct each of the Factories used to retrieve the info
        seaFact = SharedEthernetAdapterFactory.getFactory()
        veaFact = VirtualEthernetAdapterFactory.getFactory()
        viosFact = VioServerFactory.getFactory()
        #Retrieve the VIOS's, FCPorts, and VIOS Clusters for the given Host
        topo['vioservers'] = viosFact.find_all_vioses_by_host(context, host)
        topo['seas'] = seaFact.find_all_seas_by_host(context, host)
        topo['veas'] = veaFact.find_all_veas_by_host(context, host)
        return self._construct_resources(
            resource_class, context, [topo], transaction)


class HostNetworkTopology(dom.Resource):
    """Production DOM Resource implementation for Host Network Topology"""

    fields = dict(dom.Resource.fields,
                  host_name=fields.StringField(),
                  vioservers=fields.ListOfObjectsField(dom.Resource),
                  seas=fields.ListOfObjectsField(dom.Resource),
                  veas=fields.ListOfObjectsField(dom.Resource))

    ###############################################
    ##### Implementing DOM Resource Definition ####
    ###############################################
    def _get_resource_id_attribute(self):
        """Provides the Name of the Attribute for the Resource Identifier"""
        return 'host_name'

    def _initialize_resource_attributes(self):
        """Sets any default values that are defined for any attributes"""
        setattr(self, 'vioservers', list())
        setattr(self, 'seas', list())
        setattr(self, 'veas', list())

    def _create_resource(self, context, transaction):
        """Actual Implementation that will Create the given Resource"""
        #Since the Topology isn't actually persisted we will treat
        #someone who queried the Topology first the same as construct it
        super(HostNetworkTopology, self)._update_resource(context, transaction)

    @base.remotable
    def _update_resource(self, context, transaction):
        """Actual Implementation that will Update the given Resource"""
        #Update the Attributes on each of the VIO Servers
        for vios in self.vioservers:
            vios_id = vios._get_resource_identifer()
            values = vios._get_resource_values(changes_only=True)
            db_api.vios_update(self._context, vios_id, values)
        #Retrieve the existing SEA's for the Host to figure out differences
        dbseas = db_api.sea_find_all(self._context, dict(host=self.host_name))
        exist_seas = dict([(sea['id'], sea) for sea in dbseas])
        new_seas = dict([(sea['id'], sea) for sea in self.seas])
        #Determine whether the SEA needs to be Created or updated
        for sea in new_seas:
            #If the SEA already exists, we just want to update what changed
            if exist_seas.get(sea['id']) is not None:
                values = sea._get_resource_values(changes_only=True)
                db_api.sea_update(self._context, {'sea_id': sea['id']},
                                  values, transaction)
            #If the SEA is new, then we want to create it in the database
            else:
                values = sea._get_resource_values(changes_only=False)
                db_api.sea_create(self._context, values)
        #Now we want to clean up any SEA's that no longer exist in the topology
        for sea in exist_seas:
            if new_seas.get(sea['id']) is None:
                db_api.sea_delete(self._context, sea['id'])
        #Retrieve the existing VEA's for the Host to figure out differences
        dbveas = db_api.vea_find_all(self._context, dict(host=self.host_name))
        exist_veas = dict([(vea['id'], vea) for vea in dbveas])
        new_veas = dict([(vea['id'], vea) for vea in self.veas])
        #Determine whether the VEA needs to be Created or updated
        for vea in new_veas:
            #If the VEA already exists, we just want to update what changed
            if exist_veas.get(vea['id']) is not None:
                values = vea._get_resource_values(changes_only=True)
                db_api.vea_update(self._context, vea['id'], values,
                                  transaction)
            #If the VEA is new, then we want to create it in the database
            else:
                values = vea._get_resource_values(changes_only=False)
                db_api.vea_create(self._context, values)
        #Now we want to clean up any VEA's that no longer exist in the topology
        for vea in exist_veas:
            if new_veas.get(vea['id']) is None:
                db_api.vea_delete(self._context, vea['id'])


######################################################
##### Network DOM Resource Factory Registration ######
######################################################
dom.ResourceFactoryLocator.register_factory(VioServerFactory())
dom.ResourceFactoryLocator.register_factory(ManagedHostFactory())
dom.ResourceFactoryLocator.register_factory(HostNetworkTopologyFactory())
dom.ResourceFactoryLocator.register_factory(SharedEthernetAdapterFactory())
dom.ResourceFactoryLocator.register_factory(VirtualEthernetAdapterFactory())
dom.ResourceFactoryLocator.register_factory(NetworkAssociationFactory())
