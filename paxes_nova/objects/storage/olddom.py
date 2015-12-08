#
# =================================================================
# =================================================================

import abc
from copy import deepcopy
import nova.context
from nova.openstack.common import log as logging
from nova.db.sqlalchemy import api as db_session
from paxes_nova import _
from oslo.config import cfg
import re
import uuid

from paxes_nova import logcall
from paxes_nova.db import api as db
import paxes_nova.objects.olddom as pvc_dom
from paxes_nova.storage import exception as stgex
from paxes_nova.virt.ibmpowervm.common.constants \
    import CONNECTION_TYPE_NPIV, CONNECTION_TYPE_SSP
from paxes_nova.volume.cinder import PowerVCVolumeAPI


LOG = logging.getLogger(__name__)
__SCG_CHECK__ = [None]


class StorageFactory(pvc_dom.ResourceFactory):
    """
    Nova component storage DOM factory.
    """
    __metaclass__ = abc.ABCMeta

    @abc.abstractmethod
    def construct_fc_port(self, context, vios, adapter_id, session=None,
                          **kwargs):
        """
        Factory method to construct a FC port in the DB. This has no current
        impl in favor of using the generic construct_resource() factory
        method.
        TODO: Remove??
        """
        return

    SORTTYPE_EXTERNAL = 'external'
    SORTTYPE_CLUSTER = 'cluster'

    @abc.abstractmethod
    def find_all_scgs(self, context, sort_type=None, enabled_only=False,
                      cluster_provider=None, session=None):
        """
        Factory query method to retrieve all storage connectivity groups
        matching the given criteria.
        :param context: The authorization context object.
        :param sort_type: Either None, 'external', or 'cluster'. If external
                    or cluster, then the matching SCGs will be returned
                    in priority order for their respective type.
        :param enabled_only: Only return SCGs that have 'enabled' set to True
        :param cluster_provider: Only valid if sort_type is 'cluster'.
                    This value should be the clusterID of the provider to
                    match on.
        :parm session: Optional active database session to use for transaction
        :returns: list of match SCG DOM objects.
        """
        # Make select's where/filter matches (to be AND'ed together)
        where_all_filter_dict = {}
        priority_selector = lambda scg: scg.priority_cluster
        if sort_type == self.SORTTYPE_EXTERNAL:
            where_all_filter_dict['fc_storage_access'] = True
            priority_selector = lambda scg: scg.priority_external
        if enabled_only:
            where_all_filter_dict['enabled'] = True
        if cluster_provider is not None:
            where_all_filter_dict['cluster_provider_name'] = \
                cluster_provider
        if sort_type == self.SORTTYPE_CLUSTER:
            scg_list = self.find_all_scgs_with_cluster(context, session,
                                                       where_all_filter_dict)
        else:
            scg_list = self.find_all_resources(context,
                                               StorageConnectivityGroup,
                                               session, where_all_filter_dict)
        return sorted(scg_list, key=priority_selector)

    @abc.abstractmethod
    def find_all_scgs_with_cluster(self, context, session=None,
                                   where_equal_all_dict=None):
        """
        TODO: COMMENT        REMOVE??
        """
        return

    def add_default_fc_scg(self):
        """
        Insert the auto-defined (default) SCG for registered Fibre
        Channel controller/provider access. This SCG is for all VIOSes
        that get registered with PowerVC. Called by the factory constructor.
        We cannot raise exceptions from this method since a compute service
        could die, so use a general exception handler and LOG errors.
        """
        LOG.debug("Check if default FC SCG needs to be populated.")
        try:
            if __SCG_CHECK__[0] is None:
                __SCG_CHECK__[0] = "done"
            else:
                LOG.debug("Already went down auto-defined creation. Return.")
                return
            cfg.CONF.import_opt('powervm_mgr_type',
                                'powervc_nova.virt.ibmpowervm.ivm')
            if cfg.CONF.powervm_mgr_type.lower() == 'ivm':
                LOG.debug("SCGs are not supported in IVM mode. Skip.")
                return

            context = nova.context.get_admin_context()
            DEFAULT_NAME = "Any host, all VIOS"
            auto_dict = {"enabled": True, "auto_add_vios": True,
                         "auto_defined": True, "display_name": DEFAULT_NAME,
                         "fc_storage_access": True,
                         "priority_external": 1}
            # Construct and use a transactional session.
            LOG.debug("Establish DB session to check if default SCG is "
                      "needed.")
            session = db_session.get_session()
            with session.begin():
                # Get current list of SCGs
                scgs = self.find_all_scgs(context, session=session)
                dscgs = [x for x in scgs if x.fc_storage_access
                         and x.auto_defined
                         and x.display_name == DEFAULT_NAME]
                LOG.debug("Existing FC SCG: %s" % dscgs)
                if len(dscgs) > 1:
                    # Duplicate case
                    LOG.warn(_("Multiple auto-defined FC storage connectivity"
                               "groups is unexpected. One will be removed: "
                               "%s" % dscgs[0].to_dict(session, context)))
                    dscgs[0].delete(session=session)
                    return
                elif len(dscgs) == 1:
                    # Already exists case
                    LOG.debug("Default SCG already exists: %s." % DEFAULT_NAME)
                    return
                ###########################
                # Add Default FC SCG case #
                ###########################
                LOG.debug("Add default SCG for %s." % DEFAULT_NAME)
                db_vioses = db.vio_server_find_all(context, session=session)
                scg = self.construct_resource(context,
                                              StorageConnectivityGroup,
                                              session=session, **auto_dict)

                # replace the SCG vios list in context of a session
                LOG.debug("VIOS membership list: %s" %
                          [v.name for v in db_vioses])
                scg.vios_list = db_vioses
                # Now persist the SCG object
                scg.save(session)
                LOG.debug("Constructed new default SCG '%s'." % str(scg))
        except Exception as ex:
            import traceback
            #type_, value_, traceback_ = sys.exc_info()
            LOG.warn(_("A problem was encountered trying to check or add an "
                     "auto-defined storage connectivity group (SCG). This "
                     "could happen if the group was added by a different "
                     "thread or for a product install that does not support "
                     "SCGs. The error is: %s.") % ex)
            stack = traceback.format_exc()
            LOG.debug("Debug exception stack: %s." % stack)

    def remove_cluster_scgs(self, provider_id, context=None):
        """
        Remove SCGs defined for the given cluster id when the cluster
        provider is un-registered. This code gets called by the
        un-registration process.
        """
        LOG.debug("Establish DB session to check for cluster "
                  "SCGs to remove due to cluster id %s de-registration." %
                  provider_id)
        if context is None:
            context = nova.context.get_admin_context()
        session = db_session.get_session()
        with session.begin():
            # Create the filter: provider_id actually maps to
            # cluster_provider_name.
            scg_filter = {'cluster_provider_name': provider_id}
            scgs = self.find_all_resources(context,
                                           StorageConnectivityGroup,
                                           session,
                                           where_equal_all_dict=scg_filter)
            LOG.debug("Existing Cluster SCGs to remove: %s" % scgs)
            for scg in scgs:
                scg.delete(session=session)
        LOG.debug("**EXIT remove_cluster_scgs **")

    def add_default_cluster_SCG(self, provider_id,
                                cluster_display_name, backend_id,
                                context=None):
        """
        Insert the auto_defined (default) SCG for the cinder SSP provider
        that is passed in. If no exception, assume success.
        :param provider_id: The cinder host name (not the display name)
                    for the SSP driver (storage_hostname).
        :param cluster_display_name: The display name of the cluster/cinder
                    host. In 'storage-providers' API, this would be:
                        storage_providers-->service-->host_display_name
        :param backend_id: The backend id (k2 uuid) for the cluster.
        :param context: The authorization context. If not provided the admin
                    context will be obtained.
        :returns: {"id": <'id' of SCG created>} if done, otherwise
                  {"message": <reason-no-created>}
                  It is not an error to not have the default created since
                  a default could already exist.
                  The registration caller will handle exceptions coming from
                  this method.
        """
        if context is None:
            context = nova.context.get_admin_context()
        # Auto-defined name format shortened for readability
        NAME = "Any host in " + cluster_display_name
        auto_dict = {"enabled": True, "auto_add_vios": True,
                     "auto_defined": True, "display_name": NAME,
                     "fc_storage_access": True,
                     "cluster_provider_name": provider_id,
                     "priority_cluster": 1}
        # Construct and use a transactional session.
        LOG.debug("Establish DB session to check if default SCG for "
                  "provider_id=%(provider_id)s, cluster_display_name="
                  "%(cluster_display_name)s, backend_id=%(backend_id)s."
                  % locals())
        session = db_session.get_session()
        with session.begin():
            # Create the filter: provider_id actually maps to
            # cluster_provider_name.
            scg_filter = {'auto_defined': True,
                          'cluster_provider_name': provider_id}
            scgs = self.find_all_resources(context,
                                           StorageConnectivityGroup,
                                           session,
                                           where_equal_all_dict=scg_filter)
            LOG.debug("Existing Cluster SCGs: %s" % scgs)
            if len(scgs) > 1:
                # Duplicate case
                msg = _("Multiple default Cluster storage connectivity "
                        "groups is unexpected. One will be removed: %s" %
                        scgs[0].to_dict(session))
                LOG.warn(msg)
                scgs[0].delete(session=session)
                return {"message": msg}
            elif len(scgs) == 1:
                # Already exists case
                msg = _("Default Cluster storage connectivity group"
                        " already exists: %s."
                        % scgs[0].to_dict(session))
                LOG.debug(msg)
                return {"message": msg}
            ################################
            # Add Default Cluster SCG case #
            ################################
            LOG.debug("Add default SCG: %s." % NAME)
            # initial construction (don't need session for this)
            scg = self.construct_resource(context,
                                          StorageConnectivityGroup,
                                          **auto_dict)
            # Set the vios_list
            db_vioses = db.vio_server_find_all(context, session=session)
            LOG.debug("VIOSes: %s" % [{'name': v.name, 'id': v.id}
                                      for v in db_vioses])
            ssp_vioses = [v for v in db_vioses
                          if v.cluster_provider_name == provider_id]
            LOG.debug("VIOS membership list: %s" %
                      [v.name for v in ssp_vioses])
            scg.vios_list = ssp_vioses
            # Now persist the SCG object using session
            scg.save(session)
            LOG.debug("Constructed new default cluster SCG '%s'." % scg.id)
            return {"id": scg.id}


class StorageConnectivityGroup(pvc_dom.Resource):
    """
    DOM class for a storage connectivity group (SCG).
    """

    def __init__(self, context, dto=None, **kwargs):
        super(StorageConnectivityGroup, self).__init__(context, dto, **kwargs)
        if self.id is None:
            self.id = str(uuid.uuid4())
        self._dict_rep = None

    def refresh(self, session=None):
        """
        TODO: COMMENT
        """
        return

    def delete(self, session=None):
        """
        Delete an SCG. Use optional passed session and call the base class
        delete()
        """
        LOG.info(_("Request to remove storage connectivity group '%(scg)s'. "
                   "Details: %(details)s") %
                 dict(scg=self.display_name, details=self.to_dict(session)))
        if session is not None:
            del self.vios_list[:]
            super(StorageConnectivityGroup, self).delete(session)
            return
        # else get session and use
        session = self.provide_session_for_this_resource(session)
        with session.begin():
            del self.vios_list[:]
            super(StorageConnectivityGroup, self).delete(session)

##########################################################
# These methods will need to migrate to the new DOM.     #
##########################################################
    def to_dict(self, session=None, context=None):
        """
        Return a dictionary representation of this SCG resource object.
        If a session is passed, then associations will be looked up though
        the session object is not used for the lookup as the associations
        are "magically" linked if a session is in place.
        If a session is not passed, then first the instance var _dict_rep
        (dictionary representation is checked). If that is not None, then
        it is returned as the cached dictionary previously built. Otherwise,
        a new dictionary is constructed, stored in self._dict_rep and
        returned.
        :param session: Optional database session object that is used
                        in the way described above.
        :returns: A dictionary representation of this SCG instance that
                  has the form defined by callers of the REST
                  storage-connectivity-groups API.
        """
        if self._dict_rep is not None:
            return self._dict_rep
        # Otherwise construct the dictionary
        d = dict((key, value) for key, value in self._dto.__dict__.iteritems()
                 if not callable(value) and not key.startswith('_')
                 and value != "None" and value is not None
                 and key != "deleted")
        hosts = {}
        # Now convert the associated topology info into nested dictionary
        # data for the SCG
        if session is not None:
            if "vios_list" in d.keys():
                del d["vios_list"]
            vlist = self.vios_list if hasattr(self, "vios_list") else []
            for vios in vlist:
                h_name = vios.get_host_name()
                if h_name not in hosts.keys():
                    hosts[h_name] = {"name": h_name, "vios_list": []}
                # We are composing our own vios "id" here so that it is
                # unique and we can make progress.
                v = {"name": vios.name, "id":
                     h_name + "##" + str(vios.lpar_id),
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
            d["vios_cluster"] = {"provider_name": cluster}
            if pci and cluster in pci:
                d["vios_cluster"]["provider_display_name"] = \
                    pci[cluster]["host_display_name"]
                d["vios_cluster"]["backend_state"] = \
                    pci[cluster]["backend_state"]
                if pci[cluster]["backend_state"] != "error":
                    d['applicable_providers'].append(cluster)
        if "cluster_provider_name" in d.keys():
            del d["cluster_provider_name"]
        self._dict_rep = d
        return d

    @logcall
    def to_dict_with_ports(self, context, session=None, include_ports=True,
                           include_offline=False, host_name=None):
        """
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
        are included in the output.
        If include_ports is False, then use the port information only in
        calculating if the member VIOS is 'ready'
        If include_offline is True, then include ports without an "OK" status,
        but don't set the port_ready and vios_ready counts through the
        data.
        If host_name is not none, then scope SCG output to only the provided
        host. This is useful for checking whether a particular host has
        sufficient connectivity.
        """
        factory = pvc_dom.FactoryLocator.get_default_locator().locate_factory(
            StorageFactory)
        session = self.provide_session_for_this_resource(session)
        with session.begin(subtransactions=True):
            scg_dict = deepcopy(self.to_dict(session, context))
            scg_filter = {'id': self.id}
            scgs = factory.find_all_resources(
                context, StorageConnectivityGroup, session,
                where_equal_all_dict=scg_filter)
            if not scgs:
                scg_id = self.id
                msg = stgex.IBMPowerVCSCGNotFound.msg_fmt % locals()
                LOG.error(msg)
                raise stgex.IBMPowerVCSCGNotFound(msg)
            scg = scgs[0]
            vios_map = dict(map(lambda x: (str(x.get_host_name()) +
                                           "##" + str(x.lpar_id), x),
                                scg.vios_list))
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
                    port_list = db_vios.fc_port_list if db_vios.fc_port_list \
                        else []
                    filtered = self._filter_applicable_ports(port_list,
                                                             include_offline)
                    if include_ports:
                        vios['fcport_list'] = filtered
                    num_filtered = len(filtered)
                    vios['total_fcport_count'] = len(db_vios.fc_port_list)
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
                            # Uncomment the following 'elif' check to allow
                            # SSP connectivity without FC ports inventoried.
                            #  elif num_filtered == 0:
                            #      LOG.debug("Override the requirement for FC "
                            #                "ports in the SSP case.")
                            #      num_filtered = 1
                        elif 'applicable_providers' in scg_dict and \
                                not scg_dict['applicable_providers']:
                            # There is not at least one OK registered provider
                            provider_ok = False
                        # Track whether this VIOS is storage-ready. This is
                        # not necessarily a predictor of a successful deploy
                        # or relocation using this VIOS. Other factors may come
                        # into play.
                        if provider_ok and vios['state'] == 'running'\
                                and vios['rmc_state'] == 'active'\
                                and num_filtered > 0:
                            vios['storage_ready'] = True
                            vios_ready_count = vios_ready_count + 1
                        else:
                            vios['storage_ready'] = False
                        host_port_count = host_port_count + len(filtered)
                    LOG.debug("Length of db_vios.fc_port_list = %d. The "
                              "filtered list length = %d." %
                              (len(db_vios.fc_port_list), len(filtered)))
                if not include_offline:
                    if self.fc_storage_access:
                        host['port_ready_count'] = host_port_count
                    scg_port_count = scg_port_count + host_port_count
                    host['vios_ready_count'] = vios_ready_count
                    scg_dict['vios_ready_count'] += vios_ready_count
            if not include_offline and self.fc_storage_access:
                scg_dict['port_ready_count'] = scg_port_count
            if host_name is not None and not host_name_verified:
                error = _("The provided hypervisor host name '%(host)s' is "
                          "not a member of the storage connectivity group "
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

    def replace_vios_list(self, context, session, vios_ids):
        """
        Replace the SCG 'vios_list' field with the passed list.
        ***maybe wait with re-write until after DOM switchover***
        """
        # TODO: We compose our own unique vios "id" from:
        #       <host_name>##<lpar_id>
        #       Going forward, there should be common logic added to the
        #       parent dom factory for converting to/from vios unique ids.
        #       Not doing that work now since it is quite possible that
        #       the VIOS 'id' field will change to be a k2 uuid.
        host_map = {}
        for vios_id in vios_ids:
            LOG.debug("Input VIOS_id = '%s'" % vios_id)
            parts = vios_id.split("##")
            if len(parts) != 2:
                msg = _("vios_ids item not in correct format for storage "
                        "connectivity group creation: %s" % vios_id)
                raise stgex.IBMPowerVCStorageError(msg)
            if parts[0] not in host_map.keys():
                host_map[parts[0]] = [parts[1]]
            else:
                host_map[parts[0]].append(parts[1])
        LOG.debug("host_map:\n%s" % host_map)
        # first initialize the vios_list
        self.vios_list = []
        for host, vioses in host_map.iteritems():
            # append vioses
            db_vioses = db.vio_server_find_all(context, host_name=host,
                                               session=session)
            db_vios_by_id = [(db_v.id, db_v) for db_v in db_vioses]
            LOG.debug("db_vios_by_id=%s" % db_vios_by_id)
            #######################################
            # TODO: from Geraint...
            # I think a cleaner way here is to build a set of requested
            # vios IDs, and a set of available vios IDs, like:
            #
            # requested_set = set(vioses)
            # available_set = set( map( lambda x: x.id) db_vioses )
            # if requested_set.issubset(available_set):
            ########################################
            for v_id in vioses:
                v_obj_list = [x[1] for x in db_vios_by_id if x[0] == v_id]
                LOG.debug("passed vios_id '%s' maps to db vios obj %s"
                          % (v_id, str(v_obj_list)))
                if len(v_obj_list) > 0:
                    self.vios_list.append(v_obj_list[0])
                else:
                    vios_id = host + "##" + v_id
                    msg = stgex.IBMPowerVCViosNotFound.msg_fmt % locals()
                    ex = stgex.IBMPowerVCViosNotFound(msg)
                    raise ex

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

        topology = None
        if live_data:
            ####################################################
            # Get live topology data for normal deploy flows   #
            ####################################################
            try:
                from powervc_nova.storage import common as topo
                topology = topo.get_host_topo_from_k2(context, host_name)
                vios_map = topology[host_name]['vios_map']\
                    if host_name in topology else {}
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
                            LOG.debug("Topology port for id '%s' not found."
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
        # RMC connection is not active.
        p = re.compile('(running|active)$', re.IGNORECASE)
        scg_vioses = [x for x in scg_vioses if p.match(x['state'])
                      and p.match(x['rmc_state'])]
        LOG.debug("Active SCG member vios info for host '%s': %s." %
                  (host_name, scg_vioses))

        # Call private helper methods to add the connectivity information.
        # It is important to add the NPIV connectivity first in the
        # case of "dual-connectivity" because it has the most detailed
        # information and the helper methods assume this ordering for perf.
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
            msg = stgex.IBMPowerVCConnectivityError.msg_fmt % locals()
            LOG.error(msg)
            raise stgex.IBMPowerVCStorageError(msg)

        LOG.debug("connectivity_info for SCG '%s': %s." % (scg_name,
                                                           conn_info))
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
        scg_dict = self.to_dict_with_ports(context, include_offline=True)

        # Check that the passed host is a member of the SCG.
        for host in scg_dict['host_list']:
            if host['name'] == host_name:
                return host["vios_list"]

        error = _("The passed host_name '%(host)s' is not a member of the "
                  "storage connectivity group with id '%(scg_id)s'" %
                  dict(host=host_name, scg_id=self.id))
        msg = stgex.IBMPowerVCStorageError.msg_fmt % locals()
        ex = stgex.IBMPowerVCStorageError(msg)
        LOG.exception(ex)
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
                     "Storage Connectivity Group '%(name)s' and host "
                     "'%(host)s' that satisfy the connectivity criteria.")
            LOG.warning(warn % dict(name=self.display_name, host=host))
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
            #       could be accurately distinquished from a desired
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
                     (vios_dict['name'], self.display_name))
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

    def get_vios_ids(self):
        """
        Return a list of vios ids that are members of the SCG.
        """
        hosts = self.to_dict()['host_list']
        return [vios['id'] for host in hosts for vios in host['vios_list']]

    def can_access_cluster_and_fc(self, cluster_provider=None):
        """
        Returns True if SCG has access to a VIOS cluster and external
        storage controllers. If cluster_provider is given, then cluster
        provider name must be an exact match.
        :param scg: An SCG resource object
        :param cluster_provider: The cluster provider name to check against.
        :returns: true/false
        """
        scg_dict = self.to_dict()
        if (scg_dict['fc_storage_access'] is False or
                "vios_cluster" not in scg_dict.keys()):
            val = False
        elif (cluster_provider is not None and
              scg_dict['vios_cluster']['provider_name'] != cluster_provider):
            val = False
        else:
            val = True
        LOG.debug("Can SCG '%s' access both storage types? %s" %
                  (scg_dict['id']), str(val))
        return val
################################################################
# END additional methods that will need to migrate to new DOM  #
################################################################


class FcPort(pvc_dom.Resource):
    """
    DOM class for a storage Fibre Channel Port object.
    """

    def __init__(self, context, dto=None, **kwargs):
        super(FcPort, self).__init__(context, dto, **kwargs)
        if (dto is None or
                dto._pk_id is None):
            self.id = kwargs.get('id', str(uuid.uuid4()))
            self.vio_server = kwargs['vio_server']
            self.adapter_id = kwargs['adapter_id']

    def refresh(self, session=None):
        """
        TODO: COMMENT
        """
        return


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
        fcport_dict['vio_server'] = db_fcport.vio_server.name
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
