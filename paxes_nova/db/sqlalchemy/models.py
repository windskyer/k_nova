# =================================================================
# =================================================================
"""
IBM PowerVC, 2013, Nova extensions:
This is the object-relational mapping (ORM) for PVC's Nova subdomains.
It defines the mappings between PVC resource types,
such as SharedEthernerAdpater, and each type's corresponding database table.
Using SqlAlchemy, each resource type is mapped as
a <em>Data Transfer Object</em> (DTO), such as SharedEthernerAdpaterDTO,
because it has attributes (data) but no behaviors.
The full resource class is a subclass of its DTO, where the subclass adds
resource behaviors to complete the design object model (DOM)
for the resource type.
DOM resource classes for PowerVC Nova are defined various resource modules
in package: nova.api.openstack.compute.contrib.
"""

from sqlalchemy import Column, ForeignKey, event
from sqlalchemy import Integer, String, Boolean, DateTime, Float, Text
from sqlalchemy.orm import relationship, joinedload, backref
from sqlalchemy.orm.session import Session

from sqlalchemy.ext.declarative import declared_attr

from oslo.config import cfg
from nova import context as nova_context
from nova import exception
import nova.db.api as nova_db_api
import nova.db.sqlalchemy.api as nova_db_sa_api
import nova.db.sqlalchemy.models as nova_db_sa_models
from nova.objects import base as base_obj
from nova.objects import instance as inst_obj
from nova.objects import fields as fields_util
from nova.openstack.common import log as logging
from nova.openstack.common import timeutils
from paxes_nova.db.sqlalchemy import delete_cascade

from paxes_nova.objects.dom import ResourceFactory
from paxes_nova.objects.transaction import Transaction


class PowerVCTableNames(object):
    ibm_hmcs = 'ibm_hmcs'
    ibm_hmc_hosts = 'ibm_hmc_hosts'
    #
    compute_node_power_specs = 'compute_node_power_specs'
    instance_power_specs = 'instance_power_specs'
    #
    vio_server = 'server_vio'
    #
    shared_ethernet_adapter = 'adapter_ethernet_shared'
    virtual_ethernet_adapter = 'adapter_ethernet_virtual'
    network_association = 'host_network_association'
    #
    storage_connectivity_group = 'storage_connectivity_group'
    scg_vios_association = 'scg_vios_association'
    fc_port_for_adapter = 'adapter_port_fc'
    #
    compute_nodes_health_status = 'compute_nodes_health_status'
    instances_health_status = 'instances_health_status'
    #
    onboard_tasks = 'onboard_tasks'
    onboard_task_servers = 'onboard_task_servers'

TBL = PowerVCTableNames

NAME_LEN = 255
DESC_LEN = 1023
UUID_LEN = 36
STATUS_LEN = 31
USER_LEN = 63
CRED_LEN = 255
IP_LEN = 39
MODE_LEN = 31

LOG = logging.getLogger(__name__)


######################################################
#############    HMC Model Definition    #############
######################################################
class HmcDTO(nova_db_sa_models.BASE, nova_db_sa_models.NovaBase):
    """ The HmcDTO class provides the ORM for the ibm_hmcs table """
    __tablename__ = 'ibm_hmcs'
    __table_args__ = ()
    #HMC DTO Model Attributes
    id = Column('id', Integer, primary_key=True)
    hmc_uuid = Column('uuid', String(UUID_LEN), nullable=False)
    hmc_display_name = Column('display_name', String(NAME_LEN), nullable=False)
    access_ip = Column('access_ip', String(NAME_LEN), nullable=False)
    user_id = Column('user_id', String(USER_LEN), nullable=False)
    password = Column('user_credentials', String(CRED_LEN), nullable=False)
    registered_at = Column('registered_at', DateTime, default=timeutils.utcnow)


class HmcHostsDTO(nova_db_sa_models.BASE, nova_db_sa_models.NovaBase):
    """ The HmcHostsDTO class provides the ORM for the ibm_hmc_hosts table """
    __tablename__ = 'ibm_hmc_hosts'
    __table_args__ = ()
    #HMC Hosts DTO Model Attributes
    id = Column('id', Integer, primary_key=True)
    hmc_uuid = Column('hmc_uuid', String(UUID_LEN), nullable=False)
    host_name = Column('host_name', String(NAME_LEN), nullable=False)


class HmcHostClustersDTO(nova_db_sa_models.BASE, nova_db_sa_models.NovaBase):
    """The HmcHostClustersDTO class provides the ORM for the clusters table"""
    __tablename__ = 'ibm_hmc_host_clusters'
    __table_args__ = ()
    #HMC Host Clusters DTO Model Attributes
    id = Column('id', Integer, primary_key=True)
    hmc_uuid = Column('hmc_uuid', String(UUID_LEN), nullable=False)
    host_name = Column('host_name', String(NAME_LEN), nullable=False)
    cluster_id = Column('cluster_id', String(UUID_LEN), nullable=False)


######################################################
########  IBM Power Specs Model Definition  ##########
######################################################
class InstancePowerSpecsDTO(nova_db_sa_models.BASE,
                            nova_db_sa_models.NovaBase):
    """The InstPowerSpecDTO class provides the ORM for the power_specs Table"""
    __tablename__ = 'instance_power_specs'
    __table_args__ = ()
    #Instance Power Specification DTO Model Attributes
    id = Column('id', Integer, primary_key=True)
    instance_uuid = Column('instance_uuid', String(UUID_LEN), nullable=False)
    vm_id = Column('vm_id', String(UUID_LEN))
    vm_uuid = Column('vm_uuid', String(UUID_LEN))
    vm_name = Column('vm_name', String(NAME_LEN))
    vios_ids = Column('vios_ids', String(MODE_LEN))
    vcpu_mode = Column('vcpu_mode', String(MODE_LEN))
    memory_mode = Column('memory_mode', String(MODE_LEN))
    vcpus = Column('vcpus', Integer)
    min_vcpus = Column('min_vcpus', Integer)
    max_vcpus = Column('max_vcpus', Integer)
    proc_units = Column('proc_units', Float)
    min_proc_units = Column('min_proc_units', Float)
    max_proc_units = Column('max_proc_units', Float)
    memory_mb = Column('memory_mb', Integer)
    min_memory_mb = Column('min_memory_mb', Integer)
    max_memory_mb = Column('max_memory_mb', Integer)
    root_gb = Column('root_gb', Integer)
    ephemeral_gb = Column('ephemeral_gb', Integer)
    pending_action = Column('pending_action', String(STATUS_LEN))
    pending_onboard_actions = Column(
        'pending_onboard_actions', String(NAME_LEN))
    processor_compatibility = Column(
        'processor_compatibility', String(MODE_LEN))
    current_compatibility_mode = Column(
        'current_compatibility_mode', String(MODE_LEN))
    desired_compatibility_mode = Column(
        'desired_compatibility_mode', String(MODE_LEN))
    dedicated_sharing_mode = Column(
        'dedicated_sharing_mode', String(MODE_LEN))
    dedicated_proc = Column('dedicated_proc', String(MODE_LEN))
    avail_priority = Column('avail_priority', Integer)
    shared_weight = Column('shared_weight', Integer)
    rmc_state = Column('rmc_state', String(STATUS_LEN))
    uncapped = Column('uncapped', Boolean)
    operating_system = Column('operating_system', String(NAME_LEN))
    cpu_topology = Column('cpu_topology', Text)
    #Instance Power Specification DTO Model Relationships
    _instance = relationship(
        nova_db_sa_models.Instance,
        backref=backref('_power_specs', uselist=False,
                        lazy='joined', innerjoin=True),
        foreign_keys=instance_uuid,
        primaryjoin='InstancePowerSpecsDTO.instance_uuid == Instance.uuid')


# Register InstancePowerSpecsDTO as a PVC Dependent Type soft-delete cascades
#delete_cascade.DepTypeMgmt.instance().registerDepTypeForDeleteCascades(
#   InstancePowerSpecsDTO, 'instance_uuid', nova_db_sa_models.Instance, 'uuid')


######################################################
#############   VIOS Model Definition    #############
######################################################
class VioServerDTO(nova_db_sa_models.BASE, nova_db_sa_models.NovaBase):
    """The VioServerDTO class provides the ORM for the server_vio Table"""
    __abstract__ = True  # SqlA skips mapping this class to a table
    # VIO Server DTO Model Attributes
    _pk_id = Column('pk_id', Integer, primary_key=True)
    lpar_id = Column('lpar_id', Integer, nullable=False)
    lpar_name = Column('lpar_name', String(NAME_LEN), nullable=False)
    state = Column('state', String(STATUS_LEN))
    rmc_state = Column('rmc_state', String(STATUS_LEN))
    cluster_provider_name = Column('cluster_provider_name', String(NAME_LEN))
#    # VIO Server DTO Model Relationships
    _host_name = None  # Derived: SERVICES.HOST via join on _compute_node_id

    @declared_attr
    def _compute_node_id(self):
        return Column('compute_node_id', Integer,
                      ForeignKey('compute_nodes.id'), nullable=False)

    @declared_attr
    def sea_list(self):
        return relationship("SharedEthernetAdapter", cascade="all",
                            backref=backref('vio_server', lazy='immediate'))

    @declared_attr
    def vea_list(self):
        return relationship("VirtualEthernetAdapter", cascade="all",
                            backref=backref('vio_server', lazy='immediate'))

    @declared_attr
    def fc_port_list(self):
        return relationship("FcPortDTO",
                            cascade="all, delete-orphan",
                            back_populates='vio_server')

    def __init__(self, lpar_name, lpar_id, host_name, state, rmc_state):
        self.lpar_name = lpar_name
        self.lpar_id = lpar_id
        self._host_name = host_name
        self.state = state
        self.rmc_state = rmc_state

    @property
    def id(self):
        """
        Derived attribute: Provide the Server (abstract level) 'id' attribute
        from this 'lpar_id' (reverse of usual)
        """
        return str(self.lpar_id)  # NOTE: Server's 'id' is String, not Integer

    @property
    def name(self):
        """
        Derived attribute: Provide the Server (abstract level) 'name' attribute
        from this 'lpar_name' (reverse of usual)
        """
        return self.lpar_name

    @property
    def host_name(self):
        """
        The host_name is derived (and cached) from the Service.host that is
        associated via the Compute_Node ("host") association.
        Initial retrieval of the host_name uses the admin context
        (and the associated Compute_Node/host).
        """
        if self._host_name:
            return self._host_name
        else:
            if self._compute_node_id:
                # TODO: 03 HostAssc: Need optional session on compute_node_get?
                compute_node = nova_db_api.compute_node_get(
                    nova.context.get_admin_context(),
                    self._compute_node_id)
                if compute_node:
                    self._host_name = compute_node.service.host
                    return self._host_name
                else:
                    return None
            else:
                return None

    # TODO 04 Refactor away duplication with NetworkAssociationDTO
    def get_host_name(self):
        """
        The host_name is derived (and cached) from the Service.host that is
        associated via the Compute_Node ("host") association.
        Initial retrieval of the host_name uses the admin context
        (and the associated Compute_Node/host).
        """
        if self._host_name:
            return self._host_name
        else:
            if self._compute_node_id:
                # TODO: 03 VIOS-DTO: Need optional session on compute_node_get?
                compute_node = nova_db_api.compute_node_get(
                    nova_context.get_admin_context(),
                    self._compute_node_id)
                if compute_node:
                    self._host_name = compute_node.service.host
                    return self._host_name
                else:
                    return None
            else:
                return None

    def save(self, context, session=None):
        if context:
            if self._host_name and not self._compute_node_id:
                self._compute_node_id = host_find_id_by_name(context,
                                                             self._host_name)
            if self._compute_node_id and not self._host_name:
                self._host_name = self.get_host_name()
        else:
            if not self._compute_node_id:
                LOG.warn('VioServerDTO.save: For _host_name = '
                         + str(self._host_name)
                         + ', the _compute_node_id is None, and'
                         ' no context was provided (for finding it)')
        super(VioServerDTO, self).save(session)

    def delete(self, context, session=None):
        # remove the VIOS from the mapping cache while we still have the PK id
        DtoBase_sqla._vios_id_pk2dom_map.pop(self._pk_id, None)
        query = model_query(context, ScgViosAssociationDTO, session=session)
        # delete() needs synchronize_session='fetch'/'evaluate' or such?
        query.filter_by(_vios_pk_id=self._pk_id).delete()
        _delete_helper(self, context, session)


######################################################
#############  Adapter Model Definition  #############
######################################################
class SharedEthernetAdapterDTO(nova_db_sa_models.BASE,
                               nova_db_sa_models.NovaBase):
    """The SEA DTO class provides the ORM for the adap_ethernet_shared Table"""
    __abstract__ = True  # SqlA skips mapping this class to a table
    #Shared Ethernet Adapter DTO Model Attributes
    _pk_id = Column('pk_id', Integer, primary_key=True)

    @declared_attr
    def _vios_pk_id(self):
        return Column('vios_pk_id', Integer,
                      ForeignKey(TBL.vio_server + '.pk_id'), nullable=False)

    name = Column('adapter_name', String(NAME_LEN), nullable=False)
    slot = Column('slot', Integer)
    state = Column('state', String(STATUS_LEN))
    _primary_vea_pk_id = Column('primary_vea_pk_id', Integer, nullable=False)
    _control_channel_vea_pk_id = Column('control_channel_vea_pk_id', Integer)
    _primary_vea = None  # Derived from _primary_vea_pk_id and _all_veas
    _additional_veas = []
    #Shared Ethernet Adapter DTO Model Relationships

    @declared_attr
    def _all_veas(self):
        return relationship("VirtualEthernetAdapter")

    def __init__(self, name, vio_server, primary_vea, **kwargs):
        self.name = name
        self._vios_pk_id = vio_server._pk_id
        self.primary_vea = primary_vea
        if kwargs.get('control_channel') is not None:
            """BEGIN
            LOG.warn('SEA-DTO init: Setting control_channel, name = '
                     + str(kwargs['control_channel'].name))
            END"""
            self.control_channel = kwargs['control_channel']
        #
        self.slot = kwargs.get('slot', None)
        self.state = kwargs.get('state', None)
        self.additional_veas = kwargs.get('additional_veas', [])

    @property
    def primary_vea(self):
        """
        Returns the VEA that is the primary VEA for the SEA.  A SEA can not
        be defined without a primary VEA.
        """
        if not self._primary_vea:
            if self._primary_vea_pk_id:
                for vea in self._all_veas:
                    if vea._pk_id == self._primary_vea_pk_id:
                        self._primary_vea = vea
            if not self._primary_vea:
                # TODO: 02 SEA-DTO: Check on error/RAS idiom
                raise RuntimeError('SEA is missing a primary VEA,'
                                   ' with expected pk_id = '
                                   + str(self._primary_vea_pk_id))
        if not self._primary_vea_pk_id:
            self._primary_vea_pk_id = self._primary_vea._pk_id
        return self._primary_vea

    @primary_vea.setter
    def primary_vea(self, vea):
        """
        The Primary VirtualEthernetAdapter on the SEA.  This is a required
        element that can not be set to None.

        :param vea: The new VEA.
        """
        if vea._pk_id:
            # TODO: 04 If already a p-vea, need to remove it first?
            self._primary_vea_pk_id = vea._pk_id
        if vea not in self._all_veas:
            self._all_veas.append(vea)
        self._primary_vea = vea

    @property
    def control_channel(self):
        """
        The VEA that serves as the control channel (for fail-over
        capabilities).
        """
        ctrl_channel = None
        if self._control_channel_vea_pk_id is not None:
            for vea in self._all_veas:
                if vea._pk_id == self._control_channel_vea_pk_id:
                    ctrl_channel = vea
            if ctrl_channel is None:
                self._control_channel_vea_pk_id = None
        return ctrl_channel

    @control_channel.setter
    def control_channel(self, vea):
        if vea is None:
            self._control_channel_vea_pk_id = None
        else:
            self._control_channel_vea_pk_id = vea._pk_id
            if vea not in self._all_veas:
                self._all_veas.append(vea)

    @property
    def additional_veas(self):
        """
        The additional VirtualEthernetAdapters for the client VM traffic.
        """

        # Make a derived list of the additional VEAs.  This enables users to
        # make use of the .append, .extend, etc... methods.  Whenever they
        # invoke those, it will call the method to update the shadow list.
        return _DerivedList(self._additional_veas_shadow_list(),
                            self._set_additional_vea_shadow_list)

    @additional_veas.setter
    def additional_veas(self, vea_list):
        self._set_additional_vea_shadow_list(vea_list)

    def _additional_veas_shadow_list(self):
        """
        Returns a shadow list of the additional VEAs.
        """
        # The additional VEA list is a subset of the _all_veas list.
        addl_vea_list = list(self._all_veas)
        addl_vea_list.remove(self.primary_vea)
        if(self.control_channel is not None and
           self.control_channel in addl_vea_list):
            addl_vea_list.remove(self.control_channel)
        return addl_vea_list

    def _set_additional_vea_shadow_list(self, vea_list):
        """
        Internal method used to set the additional VEA Shadow List.  Can't
        be contained within the additional_veas_shadow_list or else it
        could not pass in properly.
        """
        primary = self.primary_vea
        ctrl_chnl = self.control_channel
        if vea_list is None:
            vea_list = []
        for vea in vea_list:
            if vea not in self._all_veas:
                self._all_veas.append(vea)
        for vea in self._all_veas:
            if vea not in vea_list and vea != primary and vea != ctrl_chnl:
                self._all_veas.remove(vea)

    @property
    def server(self):
        """
        Derived attribute: Provide the Adapter (abstract level) 'server'
        association from this 'vio_server' (the reverse of usual inheritance)
        """
        return self.vio_server

    def save(self, context, session=None):
        if not self._primary_vea_pk_id:
            if not self.primary_vea._pk_id:
                self.primary_vea.save(context, session)
            self._primary_vea_pk_id = self.primary_vea._pk_id
        super(SharedEthernetAdapterDTO, self).save(session)

    def delete(self, context, session=None):
        _delete_helper(self, context, session)


class VirtualEthernetAdapterDTO(nova_db_sa_models.BASE,
                                nova_db_sa_models.NovaBase):
    """The VEA DTO class provides the ORM for the adapt_ethernet_virt Table"""
    __abstract__ = True  # SqlA skips mapping this class to a table
    #Virtual Ethernet Adapter DTO Model Attributes
    _pk_id = Column('pk_id', Integer, primary_key=True)

    @declared_attr
    def _vios_pk_id(self):
        return Column('vios_pk_id', Integer,
                      ForeignKey(TBL.vio_server + '.pk_id'), nullable=False)

    @declared_attr
    def sea_pk_id(self):
        return Column('sea_pk_id', Integer,
                      ForeignKey(TBL.shared_ethernet_adapter + '.pk_id'))

    name = Column('adapter_name', String(NAME_LEN), nullable=False)
    slot = Column('slot', Integer)
    state = Column('state', String(STATUS_LEN))
    pvid = Column('pvid', Integer)
    is_trunk = Column('is_trunk', Boolean)
    trunk_pri = Column('trunk_priority', Integer)
    ieee_eth = Column('is_ieee_ethernet', Boolean)
    vswitch_name = Column('vswitch_name', String(NAME_LEN))
    _vlan_ids_string = Column('vlan_ids_string', String(NAME_LEN))
    _vlan_ids_shadow_list = None
    _SEP_CHAR = ','  # VLAN ID separator for list stored as a string
    # Virtual Ethernet Adapter DTO Model Relationships

    def __init__(self, name, vio_server, **kwargs):
        self.name = name
        self._vios_pk_id = vio_server._pk_id
        #
        self.slot = kwargs.get('slot', None)
        self.pvid = kwargs.get('pvid', None)
        self.vswitch_name = kwargs.get('vswitch_name', None)
        self._set_vlan_ids_shadow_list(kwargs.get('addl_vlan_ids', []))
        self.state = kwargs.get('state', 'Unavailable')
        self.ieee_eth = kwargs.get('ieee_eth', False)
        self.trunk_pri = kwargs.get('trunk_pri', 1)
        self.is_trunk = kwargs.get('is_trunk', False)

    def _get_vlan_ids_shadow_list(self):
        """
        Returns the shadow list of VLAN IDs.

        :returns: The list of VLAN IDs as Integer objects
        """

        # Database stores the VLAN IDs as a single string...convert.
        if self._vlan_ids_shadow_list is None:
            if not self._vlan_ids_string:
                self._vlan_ids_string = ''
                self._vlan_ids_shadow_list = []
            else:
                self._vlan_ids_shadow_list = [int(x) for x in
                                              self._vlan_ids_string.split(
                                                  self._SEP_CHAR)]
        return self._vlan_ids_shadow_list

    def _set_vlan_ids_shadow_list(self, vlan_ids_list):
        """
        Will set the shadow list of additional vlan IDs on the object.

        :param vlan_ids_list: The list of integer VLAN IDs.
        """
        self._vlan_ids_shadow_list = vlan_ids_list
        self._vlan_ids_string = \
            self._SEP_CHAR.join([str(x) for x in vlan_ids_list])

    @property
    def addl_vlan_ids(self):
        """
        Returns a list of the additional VLAN IDs on the Virtual Ethernet
        Adapter.

        :returns: The list of VLAN IDs as Integer objects
        """

        # Return a derived list, which will call _set_vlan_ids_shadow_list
        # when any modification (ex. append) is invoked on it.
        return _DerivedList(self._get_vlan_ids_shadow_list(),
                            self._set_vlan_ids_shadow_list)

    @addl_vlan_ids.setter
    def addl_vlan_ids(self, vlan_ids_list):
        """
        Property function that sets the list.  This only works when the list
        has a = done against it.
        """
        self._set_vlan_ids_shadow_list(vlan_ids_list)

    def save(self, context, session=None):
        super(VirtualEthernetAdapterDTO, self).save(session)

    def delete(self, context, session=None):
        _delete_helper(self, context, session)


class NetworkAssociationDTO(nova_db_sa_models.BASE,
                            nova_db_sa_models.NovaBase):
    """The Network Assoc DTO class provides the ORM for the net_assoc Table"""
    __abstract__ = True  # SqlA skips mapping this class to a table
    #Network Association DTO Model Attributes
    _pk_id = Column('pk_id', Integer, primary_key=True)
    neutron_net_id = Column('neutron_network_id',
                            String(UUID_LEN), nullable=False)

    @declared_attr
    def _compute_node_id(self):
        return Column('compute_node_id', Integer,
                      ForeignKey('compute_nodes.id'), nullable=False)

    @declared_attr
    def sea_pk_id(self):
        return Column('sea_pk_id', Integer,
                      ForeignKey(TBL.shared_ethernet_adapter + '.pk_id'))

    _host_name = None
    #Network Association DTO Model Relationships
    relationship("compute_nodes")

    @declared_attr
    def sea(self):
        return relationship("SharedEthernetAdapter",
                            backref=backref('network_associations',
                                            cascade="all"))

    def __init__(self, host_name, neutron_network_id, sea=None):
        self._host_name = host_name
        self.neutron_net_id = neutron_network_id
        if sea is None:
            self.sea_pk_id = None
            self.sea = None
        else:
            self.sea_pk_id = sea._pk_id
            self.sea = sea

    # TODO: 5 Refactor away via super := HostAssociateDTO
    def get_host_name(self):
        """
        The host_name is derived (and cached) from the Service.host that is
        associated via the Compute_Node ("host") association.
        Initial retrieval of the host_name uses the admin context
        (and the associated Compute_Node/host).
        """
        if self._host_name:
            return self._host_name
        else:
            if self._compute_node_id:
                # TODO: 03 VIOS-DTO: Need optional session on compute_node_get?
                compute_node = nova_db_api.compute_node_get(
                    nova_context.get_admin_context(),
                    self._compute_node_id)
                if compute_node:
                    self._host_name = compute_node.service.host
                    return self._host_name
                else:
                    return None
            else:
                return None

    def save(self, context, session=None):
        if context:
            if self._host_name and not self._compute_node_id:
                self._compute_node_id = host_find_id_by_name(context,
                                                             self._host_name)
            if self._compute_node_id and not self._host_name:
                self._host_name = self.get_host_name()
        else:
            if not self._compute_node_id:
                LOG.warn('VioServerDTO.save: For _host_name = '
                         + str(self._host_name)
                         + ', the _compute_node_id is None, and'
                         ' no context was provided (for finding it)')
        super(NetworkAssociationDTO, self).save(session)

    def delete(self, context, session=None):
        _delete_helper(self, context, session)


######################################################
############  Storage Model Definition   #############
######################################################

#class StorageConnectivityGroupDTO(ResourceStateMgmt_dtoSaBase,
#                                  nova_db_sa_models.BASE,
#                                  nova_db_sa_models.NovaBase):
#    """The SCG DTO class provides the ORM for the storage_conn_group Table"""
#    __tablename__ = 'storage_connectivity_group'
#    __table_args__ = ()
#    #Storage Connectivity Group DTO Model Attributes
#    _pk_id = Column('pk_id', Integer, primary_key=True)
#    id = Column('id', String(UUID_LEN), nullable=False)
#    display_name = Column('display_name', String(NAME_LEN), nullable=False,
#                          unique=True)
#    enabled = Column('enabled', Boolean)
#    auto_defined = Column('auto_defined', Boolean)
#    auto_add_vios = Column('auto_add_vios', Boolean)
#    fc_storage_access = Column('fc_storage_access', Boolean)
#    port_tag = Column('port_tag', String(NAME_LEN))
#    cluster_provider_name = \
#        Column('cluster_provider_name', String(NAME_LEN))
#    priority_cluster = Column('priority_cluster', Integer)
#    priority_external = Column('priority_external', Integer)
#    #Storage Connectivity Group DTO Model Relationships
#    vios_list = relationship("VioServer",
#                             secondary=lambda: ScgViosAssociationDTO.
#                             __table__,
#                             backref='scg_list')


#class FcPortDTO(ResourceStateMgmt_dtoSaBase, nova_db_sa_models.BASE,
#                nova_db_sa_models.NovaBase):
#    """The FCPort DTO class provides the ORM for the port_fc Table"""
#    __tablename__ = 'port_fc'
#    __table_args__ = ()
#    #FC Port DTO Model Attributes
#    _pk_id = Column('pk_id', Integer, primary_key=True)
#    id = Column('id', String(NAME_LEN), nullable=False)  # Need LEN >= 128
#    name = Column('name', String(NAME_LEN))
#    status = Column('status', String(STATUS_LEN))
#    enabled = Column('enabled', Boolean)
#    wwpn = Column('wwpn', String(UUID_LEN))
#    adapter_id = Column('adapter_id', String(UUID_LEN), nullable=False)
#    port_tag = Column('port_tag', String(NAME_LEN))
#    fabric = Column('fabric', String(NAME_LEN))
#    #FC Port DTO Model Relationships
#    _vios_pk_id = Column('vios_pk_id', Integer,
#                         ForeignKey(TBL.vio_server + '.pk_id'),
#                         nullable=False)
#    vio_server = relationship("VioServer", back_populates='fc_port_list')


#class ScgViosAssociationDTO(nova_db_sa_models.BASE,
#                            nova_db_sa_models.NovaBase):
#    """The ScgViosAssocDTO class provides the ORM for the scg_vios Table"""
#    __tablename__ = 'scg_vios_association'
#    __table_args__ = ()
#    #SCG to VIO Server Association DTO Model Attributes
#    _pk_id = Column('pk_id', Integer, primary_key=True)
#    _scg_pk_id = Column('scg_pk_id', Integer, ForeignKey(
#                        StorageConnectivityGroupDTO.__tablename__ + '.pk_id'),
#                        nullable=False)
#    _vios_pk_id = Column('vios_pk_id', Integer,
#                         ForeignKey(TBL.vio_server + '.pk_id'),
#                         nullable=False)


######################################################
##########  Health Status Model Definition   #########
######################################################
class ComputeNodeHealthStatusDTO(nova_db_sa_models.BASE,
                                 nova_db_sa_models.NovaBase):
    """The Compute Health DTO class provides the ORM for the health Table"""
    __tablename__ = 'compute_node_health_status'
    __table_args__ = ()
    #Compute Health Status DTO Model Attributes
    id = Column(String(UUID_LEN), primary_key=True)
    health_state = Column(String(255))
    reason = Column(Text)
    unknown_reason_details = Column(Text)


class InstanceHealthStatusDTO(nova_db_sa_models.BASE,
                              nova_db_sa_models.NovaBase):
    """The Instance Health DTO class provides the ORM for the health Table"""
    __tablename__ = 'instance_health_status'
    __table_args__ = ()
    #Instance Health Status DTO Model Attributes
    id = Column(String(UUID_LEN), primary_key=True)
    health_state = Column(String(255))
    reason = Column(Text)
    unknown_reason_details = Column(Text)


class HMCHealthStatusDTO(nova_db_sa_models.BASE,
                         nova_db_sa_models.NovaBase):
    """The HMC Health DTO class provides the ORM for the health Table"""
    __tablename__ = 'ibm_hmc_health_status'
    __table_args__ = ()
    #HMC Health Status DTO Model Attributes
    id = Column(String(36), primary_key=True)
    health_state = Column(String(255))
    reason = Column(Text)
    unknown_reason_details = Column(Text)


######################################################
##########  On-board Task Model Definition   #########
######################################################
class OnboardTaskDTO(nova_db_sa_models.BASE, nova_db_sa_models.NovaBase):
    """The OnboardTaskDTO class provides the ORM for the onboard_tasks table"""
    __tablename__ = 'onboard_tasks'
    __table_args__ = ()
    #On-board Task DTO Model Attributes
    id = Column('id', Integer, primary_key=True)
    host = Column('host', String(NAME_LEN), nullable=False)
    status = Column('status', String(STATUS_LEN), default='waiting',
                    nullable=False)
    started = Column('started_at', DateTime,
                     default=timeutils.utcnow, nullable=False)
    ended = Column('ended_at', DateTime)
    progress = Column('progress', Integer, default=0, nullable=False)


class OnboardTaskServerDTO(nova_db_sa_models.BASE, nova_db_sa_models.NovaBase):
    """The OnboardTaskServerDTO class provides the ORM for the tasks table"""
    __tablename__ = 'onboard_task_servers'
    __table_args__ = ()
    #On-board Task Server DTO Model Attributes
    id = Column('id', Integer, primary_key=True)
    task_id = Column('task_id', Integer, nullable=False)
    server_uuid = Column('server_uuid', String(UUID_LEN), nullable=False)
    server_name = Column('server_name', String(NAME_LEN), nullable=False)
    status = Column('status', String(STATUS_LEN), nullable=False)
    fault_message = Column('fault_message', String(NAME_LEN))


###################################################
##########   List of Paxes 1.2 DTO's   ##########
###################################################
#POWERVC_V1R2_DTOS = [
#    HmcDTO,  HmcHostsDTO, HmcHostClustersDTO, InstancePowerSpecsDTO,
#    VioServerDTO, SharedEthernetAdapterDTO, VirtualEthernetAdapterDTO,
#    NetworkAssociationDTO, StorageConnectivityGroupDTO, FcPortDTO,
#    ScgViosAssociationDTO, ComputeNodeHealthStatusDTO,
#    InstanceHealthStatusDTO,
#    HMCHealthStatusDTO, OnboardTaskDTO, OnboardTaskServerDTO
#]
POWERVC_V1R2_DTOS = []


###################################################
#########  Override PowerSpecs on Instance  #######
###################################################
#The way OpenStack is setup each Instance/VM basically has 2 names, the
#"display name" that the user specifies and the "name" that is internally
#generated based on a template and what is used to actually create the VM
#on the Hyper-visor.  This template name isn't actually persisted and is
#generated on the fly when someone asks for it.  This works well for deployed
#instances, but for any vm's that are changed outside of Paxes or are
#on-boarded, the template name won't match the actual name of the VM.
#
#This code below is to inject method overrides for the name and power_specs
#properties on the OpenStack SQLAlchemy and Nova.Objects Instance classes
#so that the PowerSpecs attributes are available on the Instance object and
#also so that that the name property returns the vm_name value from the
#PowerSpecs if it is set rather than the generated template name.  Both the
#on-boarding and out-of-band changes will cause the vm_name to be set to
#the actual VM's name on the Hyper-visor, so will always reflect the actual
#name rather than some internally generated name based on the id by OpenStack.
class PowerSpecsField(fields_util.FieldType):
    def coerce(self, obj, attr, value):
        if value is not None and not isinstance(value, dict):
            ValueError(_('The PowerSpecs must be a Dictionary'))
        return dict(value.iteritems()) if value is not None else dict()


def _instance_extra_specs(self):
    """Override the Instance ExtraSpecs to return PowerSpecs"""
    return ['name', 'power_specs']


def _instance_power_specs_get(self):
    """Override the Instance PowerSpecs Property Get Method"""
    adict = dict()
    attrs = ['created_at', 'updated_at', 'deleted_at', 'deleted', '_instance']
    try:
        power_specs = self['_power_specs']
        #Loop through each of the Columns in the Table, adding to Dictionary
        for column in power_specs.__table__.columns:
            if column.name not in attrs:
                adict[column.name] = getattr(power_specs, column.name)
        return adict
    #To work around OpenStack not returning the full retrieved
    #Instance from the instance_create method, allow an exception
    except Exception:
        return adict


def _instance_power_specs_set(self, value):
    """Override the Instance PowerSpecs Property Set Method"""
    pass


def _instance_name(self):
    """Override the SQLAlchemy Instance Name method"""
    power_specs = self.get('power_specs')
    power_specs = power_specs if power_specs is not None else dict()
    vm_name = power_specs.get('vm_name')
    if vm_name is not None and len(vm_name) > 0:
        return vm_name
    vm_name = self._orig_name()
    #We need to sanitize the template name and substitute invalid chars
    return _sanitize_instance_name(vm_name)

_INVALID_NAME_CHARS = '()\<>*$&?|[]\'"`,/#=:;'
_INVALID_NAME_CHAR_MAP = dict([(c, '') for c in list(_INVALID_NAME_CHARS)])


def _sanitize_instance_name(vm_name):
    """Replace invalid characters in the template name for the VM"""
    replaced_char = False
    #If the VM Name is None or an empty string, just return it
    if vm_name:
        new_name = list(vm_name)
        #Loop through each character in the string, substituting if invalid
        for index in range(len(new_name)):
            ordinal = ord(new_name[index])
            out_of_range = ordinal < 32 or ordinal > 126
            #Substitute any spaces with underscore in the VM name
            if ordinal == 32:
                new_name[index] = '_'
                replaced_char = True
            #Replace reserved or non-ASCII characters with the caret symbol
            elif out_of_range or new_name[index] in _INVALID_NAME_CHAR_MAP:
                new_name[index] = '^'
                replaced_char = True
        #For performance only convert new name to a string if it changed
        if replaced_char:
            return "".join(new_name)
    return vm_name


def _instance_create_listener(mapper, connection, target):
    """Helper method to listen for Instance Creations to create PowerSpecs"""
    try:
        session = nova_db_sa_api.get_session()
        #Need to create a default PowerSpecs when the Instance is created
        with session.begin():
            specs_ref = InstancePowerSpecsDTO()
            specs_ref.update(dict(instance_uuid=target['uuid']))
            specs_ref.save(session=session)
    except Exception as exc:
        LOG.warn('Error inserting PowerSpecs in the database')
        LOG.exception(exc)


#Inject the Name and PowerSpecs into the SQLAlchemy Instance
_orig_dbinst_name = getattr(nova_db_sa_models.Instance, 'name')
_db_power_spec_methods = property(fget=_instance_power_specs_get,
                                  fset=_instance_power_specs_set)
setattr(nova_db_sa_models.Instance, '_extra_keys',
        property(fget=_instance_extra_specs))
setattr(nova_db_sa_models.Instance, '_orig_name', _orig_dbinst_name.fget)
setattr(nova_db_sa_models.Instance, 'power_specs', _db_power_spec_methods)
setattr(nova_db_sa_models.Instance, 'name', property(fget=_instance_name))

#Inject the Name and PowerSpecs into the Instance NovaObject
inst_obj.Instance.fields['power_specs'] = \
    fields_util.Field(PowerSpecsField(), nullable=True)
base_obj.make_class_properties(inst_obj.Instance)
_orig_instobj_name = getattr(inst_obj.Instance, 'name')
setattr(inst_obj.Instance, '_orig_name', _orig_instobj_name.fget)
setattr(inst_obj.Instance, 'name', property(fget=_instance_name))

#Listen for notifications after an Instance is created in the DB
event.listen(nova_db_sa_models.Instance,
             'after_insert', _instance_create_listener)


###################################################
###########   Internal Model Utilities   ##########
###################################################
class _DerivedList(list):
    """
    Represents a list that is used to tie into derived list properties.  Will
    call a setter function when any of the modification methods are called
    on the list.  This setter method should update the parent element that
    this list was derived from.
    """

    def __init__(self, data=[], setter_functon=None):
        list.__init__(self, data)
        self.setter_functon = setter_functon

    def extend(self, iterable):
        list.extend(self, iterable)
        self.setter_functon(self)

    def append(self, obj):
        list.append(self, obj)
        self.setter_functon(self)

    def remove(self, value):
        list.remove(self, value)
        self.setter_functon(self)

    def pop(self, value):
        ret = list.pop(self)
        self.setter_functon(self)
        return ret

    def insert(self, index, obj):
        list.insert(self, index, obj)
        self.setter_functon(self)

    def reverse(self):
        list.reverse(self)
        self.setter_functon(self)

    def sort(self, compare=None, key=None, reverse=False):
        list.sort(self, compare, key, reverse)
        self.setter_functon(self)


def model_query(context, model, *args, **kwargs):
    """ Wrappers the Base Model Query to provide PowerVC-specific logic """
    return nova_db_sa_api.model_query(context, model, *args, **kwargs)


def host_find_id_by_name(context, host_name):
    """
    Returns the COMPUTE_NODES.ID (Integer) for the named host,
    else raises exception: ComputeHostNotFound
    """
    # NOTE: For RTC #3940, this implementation was duplicated from:
    # service_get_by_compute_host(context, host) in nova.db.sqlalchemy.api
    # so as to avoid the admin requirement: @require_admin_context
    svc_result = model_query(context, nova_db_sa_models.Service,
                             read_deleted="no").\
        options(joinedload('compute_node')).\
        filter_by(host=host_name).\
        filter_by(topic=cfg.CONF.compute_topic).\
        first()
    if not svc_result:
        raise exception.ComputeHostNotFound(host=host_name)
    #If the Compute Node was never created, just return
    if len(svc_result.compute_node) <= 0:
        return None
    else:
        return svc_result.compute_node[0].id


# ===={ BEGIN: New DTOs }====================================================

from sqlalchemy.orm.mapper import reconstructor

from nova.openstack.common import jsonutils
from nova.db.sqlalchemy import api as nova_session

import nova.context
import nova.db.sqlalchemy.models as nova_db_models

from paxes_nova.objects.dom import ResourceNotFound, MultipleResourcesFound
from paxes_nova.objects.compute.dom import VioServer, HostAssociate,\
    VioServerAssociate


class DtoBase_sqla(nova_db_models.BASE,
                   nova_db_models.NovaBase):
    """
    A root implementation of DtoBase, based on OpenStack Nova & SQLAlchemy.
    """
    __abstract__ = True  # SqlA skips mapping this class to a table

#    @classmethod
#    def _execute_fn_in_session_transaction(cls, fn, transaction=None):
#        """
#        TODO: 6 COMMENT
#        """
#        is_txn_passed_in = (transaction is not None)
#        session = Transaction_sqla.find_session(transaction)
#        if not is_txn_passed_in:
#            # Disassociate all cached & managed objects from the local session
#            session.expunge_all()

    # TODO: 4 REMOVE IN SPRINT 3?
    _vios_id_pk2dom_map = {}  # Maps DTO _pk_id to AOM/DOM-level VIOS-ID

    def __init__(self, context, *args, **kwargs):
        """
        TODO: 6 COMMENT
        """
        self._init_reconstructor_for_sqla()
        if self._is_initializer_new('__init__', save_init_name=True):
            self._context = context

    @reconstructor
    def _init_reconstructor_for_sqla(self):
        """
        No-arg initializer invoked on SA's DB loads instead of __init__.

        Subclass overrides should call 'super', but they should NOT be
        called by any sibling '__init__'.
        """
        if self._is_initializer_new('_init_reconstructor_for_sqla',
                                    save_init_name=True):
            self._context = None

    def _is_initializer_new(self, initializer_name, save_init_name=False):
        """True if name was never saved before, with option to save it."""
        if not hasattr(self, '_initializer_list'):
            self._initializer_list = []
        if initializer_name in self._initializer_list:
            return False
        else:
            if save_init_name:
                self._initializer_list.append(initializer_name)
            return True

    def hydrate_associations(self, transaction=None):
        pass

    def save(self, context, session=None):
        super(DtoBase_sqla, self).save(session)

    def delete(self, context, session=None):
        _delete_helper(self, context, session)

    #########################################################
    ####  DtoBase_sqla: Class-methods (implementations)  ####
    #########################################################
    @classmethod
    def create_dom(cls, context, dom_values, transaction=None):
        """Implementation: see the parent's method for more information."""
        return cls.put_dom(context, cls(context), dom_values, transaction)

    @classmethod
    def find_all_doms(cls, context, filters=None, transaction=None,
                      query_modifier_fn=None):
        """Implementation: see the parent's method for more information."""
        session = Transaction_sqla.find_session(transaction)
        dto_list = cls.find_all_dtos(context, filters, session=session,
                                     query_modifier_fn=query_modifier_fn)
        dom_list = []
        for dto in dto_list:
            dom_list.append(cls.map_dto_to_dom(context, dto, session))
        return dom_list

    @classmethod
    def find_all_dtos(cls, context, filters, session=None,
                      query_modifier_fn=None):
        """Implementation: see the parent's method for more information."""
        query = model_query(context, cls, session=session)
        if filters:
            cls.convert_dom_dict_to_dto(context, filters, session)
            query = query.filter_by(**filters)
        if query_modifier_fn:
            query = query_modifier_fn(query)
        dto_list = query.all()
        for dto in dto_list:
            dto.__init__(context)
            # TODO: 3 Check hydrate_assoc'ns vs. new __init__: still needed?
            dto.hydrate_associations()
        return dto_list

    @classmethod
    def get_dom(cls, context, filters, transaction=None):
        """Implementation: see the parent's method for more information."""
        session = Transaction_sqla.find_session(transaction)
        dto = cls.get_dto(context, filters, session=session)
        return cls.map_dto_to_dom(context, dto, session)

    @classmethod
    def get_dto(cls, context, filters, session=None):
        """Implementation: see the parent's method for more information."""
        dtos = cls.find_all_dtos(context, filters, session)
        if len(dtos) == 0:
            raise ResourceNotFound(filters=str(filters),
                                   class_name=cls.__name__)
        elif len(dtos) > 1:
            raise MultipleResourcesFound(filters=str(filters),
                                         class_name=cls.__name__)
        return dtos[0]

    @classmethod
    def get_dto_by_id(cls, context, dom_class, dom_id, session=None):
        """Implementation: see the parent's method for more information."""
        dom_id_filter = construct_dom_id_filter(dom_class, dom_id)
        return cls.get_dto(context, dom_id_filter, session)

    @classmethod
    def update_dom(cls, context, filters, dom_values, transaction=None):
        """Implementation: see the parent's method for more information."""
        session = Transaction_sqla.find_session(transaction)
        dto = cls.get_dto(context, filters, session=session)
        return cls.put_dom(context, dto, dom_values, transaction)

    @classmethod
    def update_dom_by_id(cls, context, dom_class, dom_id, dom_values,
                         transaction=None):
        """Implementation: see the parent's method for more information."""
        dom_id_filter = construct_dom_id_filter(dom_class, dom_id)
        return cls.update_dom(context, dom_id_filter, dom_values, transaction)

    @classmethod
    def put_dom(cls, context, dto, dom_values, transaction=None):
        """Implementation: see the parent's method for more information."""
        # Find or create a session for this PUT transaction
        session = find_compatible_session(dto, transaction)
        if session is not None:
            is_session_local = False
        else:
            session = nova_session.get_session()
            is_session_local = True
        # In a txn, convert the DOM state to DTO and save it
        with session.begin(subtransactions=True):
            cls.convert_dom_dict_to_dto(context, dom_values, session=session)
            dto.update(dom_values)
            # If DTO is not in session, add only AFTER the DTO is complete
            if Session.object_session(dto) != session:
                session.add(dto)
            dto.save(context, session=session)
        # Convert the saved DTO state back to a DOM-level representation
        dom_result_dict = cls.map_dto_to_dom(context, dto, session=session)
        if is_session_local:
            # Disassociate all cached & managed objects from the local session
            session.expunge_all()
        return dom_result_dict

    @classmethod
    def delete_dom(cls, context, filters, transaction=None):
        """Implementation: see the parent's method for more information."""
        # TODO 4 Bulk delete query, w/ ".delete(synchronize_session=False)"?
        session = Transaction_sqla.find_session(transaction,
                                                create_if_not_found=True)
        dto = cls.get_dto(context, filters, session=session)
        cls.delete_dto(context, dto, session=session)

    @classmethod
    def delete_dom_by_id(cls, context, dom_class, dom_id, transaction=None):
        """Implementation: see the parent's method for more information."""
        # TODO 4 Bulk delete query, w/ ".delete(synchronize_session=False)"?
        session = Transaction_sqla.find_session(transaction,
                                                create_if_not_found=True)
        dto = cls.get_dto_by_id(context, dom_class, dom_id, session=session)
        cls.delete_dto(context, dto, session=session)

    @classmethod
    def delete_dto(cls, context, dto, session=None):
        """Implementation: see the parent's method for more information."""
        # TODO 4 Bulk delete query, w/ ".delete(synchronize_session=False)"?
        if session is None:
            session = nova_session.get_session()
        with session.begin(subtransactions=True):
            dto.delete(context, session)

    @classmethod
    def convert_dom_dict_to_dto(cls, context, dom_dict, session=None):
        """Implementation: see the parent's method for more information."""
        return dom_dict

    @classmethod
    def convert_dto_dict_to_dom(cls, context, dto_dict, session=None):
        """Implementation: see the parent's method for more information."""
        return dto_dict

    @classmethod
    def convert_dom_id_to_pk_id(cls, dom_class, dom_dict):
        """Convert dict item for DOM ID attribute into a '_pk_id' item"""
        dom_id_key = dom_class.get_id_attr_name()
        resrc_id = dom_dict.pop(dom_id_key, None)
        if resrc_id:
            dom_dict['_pk_id'] = int(resrc_id)

    @classmethod
    def convert_pk_id_to_dom_id(cls, dom_class, dto_dict):
        """Convert dict item for '_pk_id' into a DOM ID attribute item"""
        dto_id = dto_dict.pop('_pk_id', None)
        if dto_id:
            dom_id_key = dom_class.get_id_attr_name()
            dto_dict[dom_id_key] = str(dto_id)

    @classmethod
    def map_dto_to_dom(cls, context, dto, session=None):
        """Returns a DOM-level dictionary representing the given DTO"""
        dto_dict = jsonutils.to_primitive(dto)
        cls.convert_dto_dict_to_dom(context, dto_dict, session=session)
        return dto_dict


def construct_dom_id_filter(dom_resource_subclass, dom_id_value):
    """
    Returns a new dict w/ one entry: { <dom-id-key>: <dom-id-value>}

    The dom-id-key is name of the ID field/attribute for the given type of
    DOM resource (a subclass of pvc_nova.objects.dom.Resource).  The
    dom-id-key is obtained from a class-method 'get_id_attr_name()', which
    should be defined on the given DOM Resource subclass.
    """
    return {dom_resource_subclass.get_id_attr_name(): dom_id_value}


def find_compatible_session(dto, transaction=None):
    """
    Returns SA session compatible for both DTO & transaction if any; else None

    Either of the arguments, DTO or transaction, could already be associated
    with a session, or not.  If both are associated, it could be to the same
    session or to different sessions.  If they are associated to different
    sessions, then an error is raised.  Otherwise, if a session is found for
    either argument (or both), it is returned; else None is returned.

    :dto: An instance of a subtype of DtoBase, it is a SA-mapped object for
    which a session is needed for persistence query operation(s).

    :transaction: An instance of a subtype of Transaction_sqla, it provides
    the current transaction/session context if any, or it may be None.

    :return: a SA session that was already associated to either given
    argument (or to both).  If neither argument already had an associated
    session, then None is returned.

    :errors: A ValueError is raised if the DTO and transaction are already
    associated to different sessions.
    """
    dto_session = Session.object_session(dto)
    txn_session = Transaction_sqla.find_session(transaction)
    if dto_session is not None:
        if (txn_session is None or txn_session == dto_session):
            return dto_session
        else:
            # TODO 3 Change to a RAS/NLS pattern?
            raise ValueError(_("The given DTO and transaction already "
                             "have sessions that are different."))
    else:
        if txn_session is not None:
            return txn_session
        else:
            return None


def _delete_helper(mapped_object, context, session=None):
    """This provides a general way to delete a SqlA-mapped object via session.
    """
    if session is None:
        session = Session.object_session(mapped_object)
    if session is not None:
        session.delete(mapped_object)
    else:
        session = nova_session.get_session()
        session.add(mapped_object)
        with session.begin(subtransactions=True):
            session.delete(mapped_object)


######################################################
#############   VIOS Model Definition    #############
######################################################
class HostAssociateDTO(DtoBase_sqla):
    """Abstract superclass that associates to a host via a compute_node f-key
    """
    __abstract__ = True  # SqlA skips mapping this class to a table
    _host_name = None  # Derived: SERVICES.HOST via join on _compute_node_id

    @declared_attr
    def _compute_node_id(self):
        return Column('compute_node_id', Integer,
                      ForeignKey('compute_nodes.id'), nullable=False)

    #############################################
    ####  HostAssociateDTO instance methods  ####
    #############################################
    @property
    def host_name(self):
        """
        The host_name is derived (and cached) from the Service.host that is
        associated via the Compute_Node ("host") association.
        Initial retrieval of the host_name uses the admin context
        (and the associated Compute_Node/host).
        """
        if self._host_name:
            return self._host_name
        else:
            if self._compute_node_id:
                # TODO: 03 HostAssc: Need optional session on compute_node_get?
                compute_node = nova_db_api.compute_node_get(
                    nova.context.get_admin_context(),
                    self._compute_node_id)
                if compute_node:
                    self._host_name = compute_node.service.host
                    return self._host_name
                else:
                    return None
            else:
                return None

    @host_name.setter
    def host_name(self, host_name):
        self._host_name = host_name

    def save(self, context, session=None):
        if context:
            if self._host_name and not self._compute_node_id:
                self._compute_node_id = host_find_id_by_name(context,
                                                             self._host_name)
        else:
            if not self._compute_node_id:
                LOG.warn('VioServerDTO2.save: For _host_name = '
                         + str(self._host_name)
                         + ', the _compute_node_id is None, and'
                         ' no context was provided (for finding it)')
        super(HostAssociateDTO, self).save(context, session=session)

    ##########################################
    ####  HostAssociateDTO class methods  ####
    ##########################################
    @classmethod
    def convert_dom_dict_to_dto(cls, context, dom_dict, session=None):
        """Implementation: see the parent's method for more information."""
        cls.convert_dom_id_to_pk_id(HostAssociate, dom_dict)
        host_name = dom_dict.pop('host_name', None)
        if host_name:
            host_id = host_find_id_by_name(context, host_name)
            dom_dict['_compute_node_id'] = host_id
        return super(HostAssociateDTO, cls).convert_dom_dict_to_dto(
            context, dom_dict, session)

    @classmethod
    def convert_dto_dict_to_dom(cls, context, dto_dict, session=None):
        """Implementation: see the parent's method for more information."""
        cls.convert_pk_id_to_dom_id(HostAssociate, dto_dict)
        compute_id = dto_dict.pop('_compute_node_id', None)
        if compute_id is not None:
            dto_dict['host_name'] = _get_compute_host_name(context,
                                                           compute_id)
        return super(HostAssociateDTO, cls).convert_dto_dict_to_dom(
            context, dto_dict, session)


def _get_compute_host_name(context, compute_node_id, session=None):
    """Helper method to retrieve the Host Name for a Compute Node ID"""
    host_name = None
    #First we need to query the Compute Node to figure out the Service
    query = model_query(
        context, nova_db_models.ComputeNode, session=session)
    compute_node = query.filter_by(id=compute_node_id).first()
    #If the Compute Node was found, query the Service for that Host
    if compute_node is not None:
        query = model_query(
            context, nova_db_models.Service, session=session)
        service = query.filter_by(id=compute_node.service_id).first()
        host_name = service.host if service is not None else None
    return host_name


class VioServerDTO2(HostAssociateDTO):
    """The VioServerDTO2 class provides the ORM for the server_vio Table"""
    __tablename__ = TBL.vio_server + "2"
    __table_args__ = ()
    # TODO: 5 This is an AOM definition (Storage API); it should be moved up.
    ID_SEPARATOR = "##"  # Move this definition to the AOM level
    # VIO Server DTO Model Attributes
    _pk_id = Column('pk_id', Integer, primary_key=True)
    lpar_id = Column('lpar_id', Integer, nullable=False)
    lpar_name = Column('lpar_name', String(NAME_LEN), nullable=False)
    state = Column('state', String(STATUS_LEN))
    rmc_state = Column('rmc_state', String(STATUS_LEN))
    cluster_provider_name = Column('cluster_provider_name', String(NAME_LEN))
    # VIO Server DTO Model Relationships
#    sea_list = relationship("SharedEthernetAdapterDTO", cascade="all",
#                            backref=backref('vio_server', lazy='immediate'))
#    vea_list = relationship("VirtualEthernetAdapterDTO", cascade="all",
#                            backref=backref('vio_server', lazy='immediate'))
#    fc_port_list = relationship("FcPortDTO", cascade="all, delete-orphan",
#                                back_populates='vio_server')

    #######################################
    ####  VioServerDTO2: Class methods  ####
    #######################################
    @classmethod
    def convert_dom_dict_to_dto(cls, context, dom_dict, session=None):
        """Implementation: see the parent's method for more information."""
        # Replace VIOS-ID by its 2 DTO constituents: 'host_name' & 'lpar_id'
        vios_id_key = VioServer.get_id_attr_name()
        vios_id = dom_dict.pop(vios_id_key, None)
        if vios_id:
            # host_name + lpar_id uniquely identifies a VIOS
            if VioServerDTO2.ID_SEPARATOR not in vios_id:
                dom_dict['lpar_id'] = vios_id
            else:
                vios_id = vios_id.split(VioServerDTO2.ID_SEPARATOR)
#                if len(vios_id) != 2:
#                    LOG.error("Bad split of vios_id= " + str(vios_id)
#                              + " from key: " + str(vios_id_key))
                dom_dict['host_name'] = vios_id[0]
                dom_dict['lpar_id'] = vios_id[1]
        # Now convert the HostAssociate level, e.g., 'host_name'
        temp_return = super(VioServerDTO2, cls).convert_dom_dict_to_dto(
            context, dom_dict, session)
#        LOG.info("convert_dom_dict_to_dto: DTO-converted dom_dict= "
#                 + str(dom_dict))
        return temp_return
#        return super(VioServerDTO2, cls).convert_dom_dict_to_dto(
#            context, dom_dict, session)

    @classmethod
    def convert_dto_dict_to_dom(cls, context, dto_dict, session=None):
        """Implementation: see the parent's method for more information."""
        super(VioServerDTO2, cls).convert_dto_dict_to_dom(context, dto_dict,
                                                          session)
        dto_dict.pop('_pk_id', None)
        id_key = VioServer.get_id_attr_name()
        # Convert VIOS-ID into its 'host_name' & 'lpar_id' constituents
        if 'host_name' in dto_dict and 'lpar_id' in dto_dict:
            dto_dict[id_key] = (dto_dict['host_name']
                                + VioServerDTO2.ID_SEPARATOR
                                + str(dto_dict['lpar_id']))
        return dto_dict

    ##########################################
    ####  VioServerDTO2: Instance methods  ####
    ##########################################
    @property
    def name(self):
        """
        Derived attribute: Provide the Server (abstract level) 'name' attribute
        from this 'lpar_name' (reverse of usual)
        """
        return self.lpar_name

    @property
    def id(self):
        return self.host_name + VioServerDTO2.ID_SEPARATOR + str(self.lpar_id)

    def save(self, context, session=None):
        super(VioServerDTO2, self).save(context, session)


######################################################
#############  Adapter Model Definition  #############
######################################################
class VioServerAssociateDTO(DtoBase_sqla):
    __abstract__ = True  # SqlA skips mapping this class to a table

    @declared_attr
    def _pk_id(self):
        return Column('pk_id', Integer, primary_key=True)

    # VA-Assoc-DTO model relationships
    @declared_attr
    def _vios_pk_id(self):
        return Column('vios_pk_id', Integer,
                      ForeignKey(TBL.vio_server + '.pk_id'), nullable=False)

    # DTO life-cycle functions
    def save(self, context, session=None):
        super(VioServerAssociateDTO, self).save(context, session)

    ################################################
    ####  VioServerAssociateDTO: Class methods  ####
    ################################################
    @classmethod
    def convert_dom_dict_to_dto(cls, context, dom_dict, session=None):
        """Implementation: see the parent's method for more information."""
        super(VioServerAssociateDTO, cls).convert_dom_dict_to_dto(context,
                                                                  dom_dict,
                                                                  session)
        # Convert DOM VIOS reference to '_vios_pk_id'
        vios_id_key = VioServerAssociate.get_vios_ref_attr_name()
        vios_id = dom_dict.pop(vios_id_key, None)
        if vios_id:
            # Use vios_id to get the VIOS and get its '_pk_id'
            vios_dto_pk_id = vios_dto_get_pk_id(context, vios_id)
            dom_dict['_vios_pk_id'] = vios_dto_pk_id

    @classmethod
    def convert_dto_dict_to_dom(cls, context, dto_dict, session=None):
        """Implementation: see the parent's method for more information."""
        vios_pk_id = dto_dict.pop('_vios_pk_id', None)  # attr def'd in VSA-DTO
        if vios_pk_id:
            try:
                vios_dto = VioServerDTO2.get_dto(
                    context, {'_pk_id': vios_pk_id}, session)
                dto_dict[VioServerAssociate.get_vios_ref_attr_name()] = \
                    vios_dto.id
            except ResourceNotFound:
                # TODO: 4 This path should be unnecessary in S3
#                query = model_query(context, VioServerDTO, session=session)
#                vios_dto = query.filter_by(_pk_id=vios_pk_id).first()

                dto_dict[VioServerAssociate.get_vios_ref_attr_name()] = \
                    DtoBase_sqla._vios_id_pk2dom_map[vios_pk_id]
        return super(VioServerAssociateDTO, cls).convert_dto_dict_to_dom(
            context, dto_dict, session)


################
class StorageConnectivityGroupDTO(DtoBase_sqla):
    """The SCG DTO class provides the ORM for the storage_conn_group Table"""
    __tablename__ = 'storage_connectivity_group'
    __table_args__ = ()
    #Storage Connectivity Group DTO Model Attributes
    _pk_id = Column('pk_id', Integer, primary_key=True)
    id = Column('id', String(UUID_LEN), nullable=False)
    display_name = Column('display_name', String(NAME_LEN), nullable=False,
                          unique=True)
    enabled = Column('enabled', Boolean)
    auto_defined = Column('auto_defined', Boolean)
    auto_add_vios = Column('auto_add_vios', Boolean)
    fc_storage_access = Column('fc_storage_access', Boolean)
    port_tag = Column('port_tag', String(NAME_LEN))
    cluster_provider_name = \
        Column('cluster_provider_name', String(NAME_LEN))
    priority_cluster = Column('priority_cluster', Integer)
    priority_external = Column('priority_external', Integer)
    #Storage Connectivity Group DTO Model Relationships
#    vios_list = relationship("VioServer",
#                             secondary=lambda: \
#                             ScgViosAssociationDTO.__table__,
#                             backref='scg_list')
    vios_list = relationship("VioServer",
                             secondary=lambda: ScgViosAssociationDTO.__table__)

    @property
    def vios_ids9(self):
        """A list of IDs for the associated VIOS
        """
        return [vios.id for vios in self.vios_list]

    @vios_ids9.setter
    def vios_ids9(self, new_vios_ids):
        """Sets the list of VIOS IDs
        NOTE: This is done indirectly by updating the underlying _vios_list.
        """
        for vios in self.vios_list:
            if vios.id not in new_vios_ids:
                self.vios_list.remove(vios)
        current_ids = self.vios_ids
        missing_ids = []
        for vid in new_vios_ids:
            if vid not in current_ids:
                missing_ids.append(vid)
        for vid in missing_ids:
            # TODO: 0 Re-enable with Nwk migration
#            vios_dto = VioServerDTO.get_dto_by_id(self._context, VioServer,
#                                                  vid, session=None)
#            self.vios_list.append(vios_dto)
            self.vios_list.append("VIOSES-DISABLED")

    @reconstructor
    def _init_reconstructor_for_sqla(self):
        """No-arg initializer invoked on SA's DB loads instead of __init__."""
        super(StorageConnectivityGroupDTO, self)._init_reconstructor_for_sqla()
        self.vios_ids = []

    ######################################################
    ####  StorageConnectivityGroupDTO: Class methods  ####
    ######################################################
    # Currently NO SCG class methods


class FcPortDTO(VioServerAssociateDTO):
    """The FCPort DTO class provides the ORM for the port_fc Table"""
    __tablename__ = 'port_fc'
    __table_args__ = ()
    #FC Port DTO model attributes
    id = Column('id', String(NAME_LEN), nullable=False)  # Need LEN >= 128
    name = Column('name', String(NAME_LEN))
    status = Column('status', String(STATUS_LEN))
    enabled = Column('enabled', Boolean)
    wwpn = Column('wwpn', String(UUID_LEN))
    adapter_id = Column('adapter_id', String(UUID_LEN), nullable=False)
    port_tag = Column('port_tag', String(NAME_LEN))
    fabric = Column('fabric', String(NAME_LEN))
    #FC Port DTO model relationships
    vio_server = relationship("VioServer", back_populates='fc_port_list')

    ####################################
    ####  FcPortDTO: Class methods  ####
    ####################################
    # Currently NO class methods


def vios_dto_get_pk_id(context, vios_id, session=None, vios_dto_class=None):
    """Returns the identified VioServerDTO2 or exception if not exactly one.
    """
    # TODO: 4 Remove vios_dto_class in S3
    if vios_dto_class is None:
        vios_dto_class = VioServerDTO2
    query = nova_session.get_session().query(vios_dto_class._pk_id)
    filters = VioServerDTO2.convert_dom_dict_to_dto(
        context, {VioServer.get_id_attr_name(): vios_id}, session)
    result = query.filter_by(**filters).first()
    return result[0]

# ===={ END: New DTOs }============================


class ScgViosAssociationDTO(DtoBase_sqla):
    """The ScgViosAssocDTO class provides the ORM for the scg_vios Table"""
    __tablename__ = 'scg_vios_association'
    __table_args__ = ()
    #SCG to VIO Server Association DTO Model Attributes
    _pk_id = Column('pk_id', Integer, primary_key=True)
    _scg_pk_id = Column('scg_pk_id', Integer, ForeignKey(
                        StorageConnectivityGroupDTO.__tablename__ + '.pk_id'),
                        nullable=False)
    _vios_pk_id = Column('vios_pk_id', Integer,
                         ForeignKey(TBL.vio_server + '.pk_id'),
                         nullable=False)


class Transaction_sqla(Transaction):
    """
    Root-level implementation of Transaction based on SQLAlchemy's Session.
    """

    def __init__(self, session=None):
        """
        """
        self._session = session

    @property
    def session(self):
        if self._session is None:
            self._session = nova_session.get_session()
        return self._session

    @session.setter
    def session(self, new_session):
        self._session = new_session

    ###############################################
    ### Defining Resource Transaction Interface ###
    ###############################################
    def begin(self, *args, **kwargs):
        """Implementation: Uses 'sqlalchemy.orm.session.Session' """
        return self.session.begin(*args, **kwargs)

    def commit(self, *args, **kwargs):
        """Implementation: Uses 'sqlalchemy.orm.session.Session' """
        return self.session.commit(*args, **kwargs)

    def rollback(self, *args, **kwargs):
        """Implementation: Uses 'sqlalchemy.orm.session.Session' """
        return self.session.rollback(*args, **kwargs)

    def close(self, *args, **kwargs):
        """Implementation: Uses 'sqlalchemy.orm.session.Session' """
        return self.session.close(*args, **kwargs)

    ###############################################
    ####  Internal Transaction Helper Methods  ####
    ###############################################
    def __enter__(self):
        """Implementation: Uses 'sqlalchemy.orm.session.Session' """
        return self.session.__enter__()

    def __exit__(self, a_type, value, traceback):
        """Implementation: Uses 'sqlalchemy.orm.session.Session' """
        return self.session.__exit__(a_type, value, traceback)

    ##########################################
    ###  Transaction_sqla: Class methods  ####
    ##########################################
    @classmethod
    def find_session(cls, transaction, create_if_not_found=False):
        """
        Returns the txn's session if possible or optionally creates a new one.
        """
        if (transaction is not None and
                hasattr(transaction, 'session') and
                transaction.session is not None):
            return transaction.session
        elif create_if_not_found:
            return nova_session.get_session()
        return None


############################
####  Runtime Settings  ####
############################
ResourceFactory.DEFAULT_RESOURCE_TRANSACTION_CLASS = Transaction_sqla
