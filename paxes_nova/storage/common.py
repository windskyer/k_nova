#
# =================================================================
# =================================================================
from oslo.config import cfg

import nova.openstack.common.log as logging
from paxes_nova import _
LOG = logging.getLogger(__name__)

import paxes_nova.db.api as db
import paxes_nova.volume.cinder as pvc_cinder

import paxes_nova.objects.compute.dom as compute_dom
import paxes_nova.objects.storage.dom as storage_dom
import paxes_nova.storage.hmc_k2 as hk2

from paxes_nova.virt.ibmpowervm.hmc.k2oprguard import K2OperGuard
import paxes_nova.virt.ibmpowervm.hmc.hmc as pvm_hmc
import paxes_nova.virt.ibmpowervm.hmc.host as dvr_host


def get_host_topo_from_k2(context, host_name, skip_cluster=False,
                          max_cache_age=-1):
    """
    Build a host topology dictionary using data from K2.
    If host_name is None, then build topology for all hosts. In normal
    operations, this API is not called for all hosts, but the flow
    allows it because of the original impl and for future expansion.
    """
    # Initialize data vars
    topo_factory = storage_dom.HostStorageTopologyFactory.get_factory()
    static_topo = topo_factory.get_static_topology()
    topology = {'hosts': {}, 'skip-update-clusters': skip_cluster,
                'vios_by_id': {}, 'vios_by_uuid': {}, 'vios_num_trusted': 0,
                'seq': static_topo.bump_topology_sequence(),
                'static_topo': static_topo, 'nova_host': dvr_host.compute_host,
                'max_cache_age': max_cache_age}
    hosts_seen = set()

    if topology['nova_host']:
        # We can assume we are being called from within the compute
        # driver process. In this case, the compute host decides it's own
        # HMC and whether it can fall back to a different one.
        # So we construct a dummy db hmc list for the HMC loop below.
        hmc_obj = dvr_host.compute_host.select_active_hmc()
        db_hmcs = [{'access_ip': hmc_obj.ipaddr if hmc_obj else 'unknown',
                    'managed_hosts': [topology['nova_host'].mtm_serial]}]
        LOG.debug("Compute host based invocation.")
    else:
        # Not being called from within compute host, so get HMCs from db
        LOG.debug("Nova-api based invocation.")
        hmcfact = compute_dom.ManagementConsoleFactory.get_factory()
        if host_name is None:
            db_hmcs = hmcfact.find_all_hmcs(context)
        else:
            db_hmcs = hmcfact.find_all_hmcs_by_host(context, host_name)
        if not db_hmcs:
            msg = _("There are no HMC connections registered. The host is "
                    "%s. No topology will be collected.") % (host_name)
            LOG.warn(msg)
            topology['error'] = [_('%s') % msg]
            return topology

    # Loop over the HMCs
    for db_hmc in db_hmcs:
        LOG.debug("HMC Loop: access_ip=%s, managed_hosts=%s, passed_host_name"
                  "='%s', hosts_seen_set=%s." %
                  (db_hmc['access_ip'], db_hmc['managed_hosts'], host_name,
                   hosts_seen))
        # Get the list of hosts that the HMC provides.
        host_set = set()
        for host in db_hmc['managed_hosts']:
            if not host_name or host == host_name:
                host_set.add(host)

        # Because of HMC redundancy, we need to avoid dup host processing
        host_set = host_set - hosts_seen
        if not host_set:
            LOG.debug("Hosts for HMC '%s' already handled or no longer "
                      "exist in the database." % db_hmc['access_ip'])
            continue

        # Get the K2 Operator client.
        try:
            if topology['nova_host']:
                # We can assume we are being called from within the compute
                # driver process. In this case, there is a single shared K2
                # transaction that is to be used.
                oper_guard = K2OperGuard(host=topology['nova_host'])
            else:
                # We are running in a different process like nova-api.
                # Check the list and add a new HMC if needed. We don't
                # worry about removing unmanaged HMCs. This is a debug flow
                # and the normal flow goes through the 'if nova_host' branch.
                if static_topo.hmc_list is None:
                    static_topo.hmc_list = pvm_hmc.HMCList()
                hmc = static_topo.hmc_list.get(db_hmc['access_ip'])
                if not hmc:
                    LOG.debug("Add new HMC object to in-memory list.")
                    hmc = pvm_hmc.HMC(db_hmc['access_ip'], db_hmc['user_id'],
                                      db_hmc['password'])
                    static_topo.hmc_list.add(db_hmc['access_ip'],
                                             hmc)
                oper_guard = K2OperGuard(hmc=hmc)

            # Use the operator guard as the K2 operator for all downstream
            # activity. This will maintain a single transaction for compute.
            with oper_guard as oper:
                if oper is None:
                    # Log the error and continue to next HMC if possible.
                    LOG.warn(_("Could not get an HMC K2 Operator instance "
                               "for managed host '%s'.") %
                             topology['nova_host'].mtm_serial)
                    continue

                LOG.debug("Got K2 operator for HMC %s with K2Session object "
                          "%s. The host_set is: %s" %
                          (db_hmc['access_ip'], str(oper.session), host_set))

                #############################################################
                # Per topology collection run (per host), we have paired
                # the K2 calls down to two at most:
                # 1. Request a VIOS Feed for the host with a single extended
                #    group of [ViosStorage].
                # 2. If needed, request a Cluster feed, to get VIOS
                #    membership information per defined cluster.
                #
                # Based on the K2 behavior seen during PVC 1.2.1 development,
                # the order of the above calls has been switched to be the
                # order listed above. The reason is that the Cluster call is
                # generally more expensive and it is a wasted call if all
                # of the VIOSes are not in a good state (including RMC). If
                # the set of VIOSes are not healthy, then the information
                # returned from K2 will be stale with respect to information
                # the HMC receives by talking to the VIOS operating system.
                # And this includes cluster membership. Even if everything
                # is configured properly, we have seen the set of host VIOSes
                # spontaneously go into an rmc_state of 'busy', sometimes
                # remaining in that state, and sometimes recovering quickly.
                # In either case, it is not worthwhile for the periodic task
                # to go after cluster membership information during this cycle.
                # We will leave the membership in the nova DB as is to
                # avoid thrashing, even though from a technical standpoint,
                # the VIOSes may not be members if they don't see one another
                # because of a slow response due to connectivity or VIO
                # Server busy-ness.
                ##############################################################
                # 1. Get the topology for host(s), which is essentially the
                #    VIOSs topology, and add it to the topology dictionary.
                ##############################################################
                collect_hosts_topology(oper, topology, list(host_set))

                ##########################################################
                # 2. Get VIOS cluster membership and add to topology.
                #
                #    The method will no-op for a variety of cluster
                #    skip conditions. Note that the skip conditions of
                #    the called method here handle most cases for the
                #    defect 19372. And the remaining timing window case
                #    between the VIOS and Cluster feed calls is handled
                #    by the computed 'trust_seq' property added to VIOS
                #    topology entries.
                ##########################################################
                hk2.map_vios_to_cluster(oper, topology)

                # Now add hosts handled to hosts_seen
                hosts_seen |= host_set
        except Exception as ex:
            LOG.exception(ex)
            msg = _("There was a problem getting host or Virtual I/O "
                    "Server information from HMC '%s'." % db_hmc['access_ip'])
            LOG.warn(msg)
            topology['error'] = [msg, _('%s') % ex]

    LOG.debug("Acquired HOST STORAGE TOPOLOGY from K2 for VIOS IDs %s. "
              "is_error_set=%s" % (topology['vios_by_id'].keys(),
                                   ('error' in topology)))
    return topology


def update_hosts_in_db_with_k2_data(context, host_name=None):
    """
    *****************************************************************
    ** This is the main entry point to this module of topo utils.  **
    *****************************************************************
    Using the data from K2, update the database entries.
    If host_name is passed, then only process that single host.
    """

    cfg.CONF.import_opt('powervm_mgr_type',
                        'powervc_nova.virt.ibmpowervm.ivm')

    #This only applies to HMC configurations.  Skip when in IVM config.
    if cfg.CONF.powervm_mgr_type.lower() == 'hmc':
        #################################################################
        # Get the topology from from the K2 operator. Depending on the
        # the call, it may be cached data.
        #################################################################
        LOG.debug("Retrieve Storage topology data from K2...")
        topology = get_host_topo_from_k2(context, host_name)
        if 'error' in topology:
            LOG.warn(_("Host storage topology returned with an error "
                       "condition set: %s") % topology['error'])
        LOG.debug("Storage Topology data retrieved.")

        # Get the ResourceTransaction (cf. DB2 session) for the nova db
        vios_factory = storage_dom.VioServerFactory.get_factory()
        txn = vios_factory.get_resource_transaction()
        with txn.begin():
            # Get the list of VIOSes from the nova DB.
            db_vio_servers = vios_factory.find_all_vioses_by_host(
                context, host_name, transaction=txn)

            if 'error' in topology or host_name is None or\
                    topology['skip-update-clusters']:
                LOG.debug("Skip cluster provider reconciliation since either "
                          "there was a failure, clusters are configured to "
                          "be skipped, no VIOSes are in a trusted state, "
                          "not reconciling topology for a single compute "
                          "host, or HMC level is below 8.1.10")
            else:
                # Update cluster related db tables if needed.
                update_cluster_storage_providers(context, topology,
                                                 db_vio_servers, host_name)

            # Call the helper method to save the VIOS ports in the DB and
            # update vios cluster membership if needed. It is important
            # that the update_cluster_storage_providers() call is made
            # beforehand, so that the cluster can exist in the DB for vios
            # entries to link to.
            LOG.debug("Reconcile VIOS cluster membership and FC Ports in DB "
                      "with Topology data...")
            update_vios_fc_ports_in_db(context, topology, db_vio_servers,
                                       transaction=txn)
            LOG.info(_("Host storage topology reconciled against Virtual I/O "
                       "Server IDs %s in collected topology data.") %
                     topology['vios_by_id'].keys())

        ######################################################################
        # The above transaction is now closed.
        # Call a method to reconcile SCG membership with DB vios data.
        # It will establish its own new transaction to the db,
        # and determine if extra reconciliation is needed for SCG membership.
        ######################################################################
        LOG.debug("Run SCG membership reconciliation...")
        update_scgs_vios_membership(context, host_name, db_vio_servers,
                                    topology)
        return topology
    else:
        LOG.debug("Not an HMC configuration."
                  "  Skipping Host-Storage topology reconciliation.")
        return None


def update_cluster_storage_providers(context, topology, db_vios_list,
                                     host_name):
    """Update Clusters in the DB only if HMC Version > 8.1.0"""
    k2_clusters = dict()
    k2host_clusters = list()
    prvfact = storage_dom.StorageProviderFactory.get_factory()
    hostfact = compute_dom.ManagedHostFactory.get_factory()
    hmc_uuids = None
    old_cluster_id = None
    if not topology['vios_by_id']:
        # Is this the behavior we want?
        LOG.debug("No VIOS partitions were retrieved from K2 for the host. "
                  "Prior cluster data will likely be removed from the DB.")

    #Get the list of Cluster Provider Names off of each VIOS for the given Host
    for k2vios in [v['vios_cluster'] for v in topology['vios_by_id'].values()
                   if 'vios_cluster' in v]:
        if k2vios['cluster_id'] not in k2host_clusters:
            k2host_clusters.append(k2vios['cluster_id'])
            k2_clusters[k2vios['cluster_id']] = \
                dict(cluster_name=k2vios['cluster_name'],
                     backend_id=k2vios['backend_id'])

    #Build a mapping of what all hosts are registered for each cluster
    db_clusters, dbhost_clusters = get_db_host_cluster_maps(context, host_name)
    LOG.debug("k2_clusters=%s, k2host_clusters=%s, db_clusters=%s, "
              "dbhost_clusters=%s" % (k2_clusters, k2host_clusters,
                                      db_clusters, dbhost_clusters))

    #######################
    # Add processing      #
    #######################
    #   Note that we can get away with doing add processing before remove
    #   processing because:
    #   (1) Logic was added to the StorageConnectivityGroupFactory to first
    #       remove any SCG with the same default name before inserting the
    #       new SCG for a provider with the same display name. AND
    #   (2) We assume that any new cluster will have a unique cluster_id.
    #       Although this is true, default volume types adhere to a cluster
    #       by the cluster's display name AND it is awkward to see two
    #       providers with the same display name in a table (and template
    #       names). So we check for the case where a provider with the same
    #       name is in the DB already, but not coming back from K2 and we
    #       wait until it is removed and add it on the next run.
    #       It could be special cased and added after the remove, or the
    #       removes could happen first, but the amount of restructuring
    #       needed is too risky - I'd rather wait until post 1.2.1.
    #
    #Loop through the Clusters and see if we need to add any Storage Providers
    for cluster_id in k2_clusters:
        backend_id = k2_clusters[cluster_id]['backend_id']
        cluster_name = k2_clusters[cluster_id]['cluster_name']
        if cluster_id not in dbhost_clusters:
            LOG.debug("K2 cluster ID %s is not in DB cluster list." %
                      cluster_id)
            # Get the UUID used for the HMC for the given Host
            if not hmc_uuids:
                hmc_uuids = hostfact.find_host_by_name(context,
                                                       host_name).hmc_uuids
            try:
                # Check if there are registered clusters with the same
                # display name.
                matches = prvfact.find_providers_by_display_name(
                    context, display_name=cluster_name)
                LOG.debug("find_providers_by_display_name [%s]: %s." %
                          (cluster_name, matches))
                for clst in matches:
                    if clst['host_name'] not in k2_clusters:
                        LOG.info(_("Cluster ID %(old)s is not in "
                                   "K2 data for this collection, so not "
                                   "adding new cluster for ID %(new)s until "
                                   "the old one is removed.") %
                                 dict(old=clst['host_name'], new=cluster_id))
                        old_cluster_id = clst['host_name']
                        break
                if old_cluster_id:
                    continue
                #Add Cluster to this Host in the Host Clusters Database Table
                values = dict(hmc_uuid=hmc_uuids[0], host_name=host_name,
                              cluster_id=cluster_id)
                db.host_cluster_create(context, values)
                #Add the Cluster as a Storage Provider if it isn't already one
                if prvfact.find_provider_by_name(context, cluster_id) is None:
                    LOG.info(_("Register new VIOS cluster provider "
                               "%(cluster_id)s with name '%(name)s'.") %
                             dict(cluster_id=cluster_id, name=cluster_name))
                    prvfact.construct_vios_cluster(context, cluster_id,
                                                   hmc_uuids, cluster_name,
                                                   backend_id,
                                                   auto_create=True)
                # If we executed the above block, then it could have been
                # long running to create the SSP provider. So we need to
                # re-acquire current DB data again, as other hosts may have
                # updated it.
                db_clusters, dbhost_clusters = get_db_host_cluster_maps(
                    context, host_name)
            except Exception as ex:
                LOG.warn('Error adding VIOS Cluster ' + cluster_id +
                         ' for host ' + host_name + '.')
                LOG.exception(ex)

    #######################
    # Removal processing  #
    #######################
    static_topo = topology['static_topo']
    questionable_clusters = static_topo.get_clusters_in_ques(dbhost_clusters)
    #Loop through the Clusters with a check to delete any not returned by k2
    for cluster_id in dbhost_clusters:
        if cluster_id not in k2host_clusters:
            LOG.debug("DB cluster ID %s is not in k2 cluster list." %
                      cluster_id)
            ################################################################
            # The cluster id may not be in the k2host topology data because
            # the VIOSs had trouble reporting cluster membership to HMC.
            # In this case, we choose not to remove the DB association
            # and provider entry just yet.
            #
            # The flip side of this is what if the cluster membership
            # remains in question indefinitely? For example: K2 repeatedly
            # errors out, or the VIOSs remain in 'busy' state, or VIOS
            # partitions are deleted & different ones created to join or
            # not to join the cluster.
            # For the latter case, we probably would want to eventually
            # perform the cleanup in this code block if there was not going
            # to be new VIOSes from the host joining the cluster, but
            # ferreting out that particular case was not attempted as part
            # of 19372.
            ##############################################################
            # Additional note: We special case old_cluster_id that may
            # have been looked up earlier. We don't care if it is still
            # not "trusted". We implicitly trust that it needs to be
            # removed because a new cluster is coming in with the same name.
            ################################################################
            if cluster_id in questionable_clusters and\
                    cluster_id != old_cluster_id:
                LOG.info(_("Skipping cluster '%(cluster)s' removal from "
                           "host %(host)s as cluster membership for the host "
                           "VIOSs may have dropped to 0 for temporary "
                           "environmental reasons.") % dict(cluster=cluster_id,
                                                            host=host_name))
                continue
            try:
                #Remove Cluster from Host in the Host Clusters Database Table
                LOG.info(_("Remove cluster %(cluster)s from host %(host)s in "
                           "database.") % dict(cluster=cluster_id,
                                               host=host_name))
                db.host_cluster_delete(context, cluster_id, host_name)
                #Remove Host from Storage Provider if it has no other hosts
                if len(db_clusters[cluster_id]) <= 0:
                    LOG.debug("There are no other hosts claiming cluster_id "
                              "%s." % cluster_id)
                    if prvfact.\
                       find_provider_by_name(context, cluster_id) is not None:
                        LOG.info(_("Remove VIOS cluster %(cluster)s from "
                                   "cinder volume providers.") %
                                 dict(cluster=cluster_id))
                        prvfact.delete_provider(context, cluster_id)
                    # If successful, remove our cache entry for it
                    if cluster_id in static_topo.cluster_keyed:
                        LOG.debug("Remove cached entry: %s." %
                                  static_topo.cluster_keyed[cluster_id])
                        del static_topo.cluster_keyed[cluster_id]
                else:
                    LOG.debug("There is still a claim on cluster_id %s "
                              "from: %s" % (cluster_id,
                                            db_clusters[cluster_id]))
            except Exception as ex:
                LOG.warn('Error removing VIOS Cluster ' + cluster_id +
                         ' for host ' + host_name + '.')
                LOG.exception(ex)
    return


def get_db_host_cluster_maps(context, host_name):
    """ Return a mapping of clusters to other hosts. Also return a list of
        clusters for this host """
    db_clusters = dict()
    dbhost_clusters = list()
    for dbclust in db.host_cluster_find_all(context):
        if dbclust['cluster_id'] not in db_clusters:
            db_clusters[dbclust['cluster_id']] = list()
        #If the Cluster Mapping is to this Host, add it to this Hosts list
        if dbclust['host_name'] == host_name:
            dbhost_clusters.append(dbclust['cluster_id'])
        #Add to the db host list if the Cluster Mapping is to a different host
        else:
            db_clusters[dbclust['cluster_id']].append(dbclust['host_name'])
    return (db_clusters, dbhost_clusters)


def collect_hosts_topology(oper, topology, db_hosts):
    """
    Build a topology for a dict of hosts with their related VIOS information.
    The provider, if one, is set as one of the properties of the vios.
    If nova_host is set, then db_hosts is ignored as this process is
    collecting topology info for only the nova_host.
    """
    # If nova_host is passed, then we are running in a compute host process
    # and we call an optimized method to get the vios topology
    if topology['nova_host']:
        host_dict = {'name': topology['nova_host'].mtm_serial,
                     'uuid': topology['nova_host'].uuid}
        topology['hosts'][topology['nova_host'].mtm_serial] = host_dict
        hk2.collect_host_vios_feeds(oper, topology)
        return topology

    # If the database has hosts
    if db_hosts:
        # Loop through each of the hosts. For the normal periodic task,
        # there should be just one host.
        for db_host in db_hosts:
            LOG.debug("Processing host '%s'..." % db_host)
            # Build the initial host dictionary
            host_dict = {'name': db_host}
            # Set the MTMS information based on the host_name
            parse_name_to_mtms(db_host, host_dict)
            # Collect the vios topo info for the db_host.
            hk2.collect_vios_topology(oper, topology, host_dict)
            # Add each host's info to the host_list.
            topology['hosts'][db_host] = host_dict
    else:
        LOG.warn(_("Database contains no hosts."))
    # Return the updated topology back
    return topology


def parse_name_to_mtms(host_name, host_dict):
    """
    Given a host's machine type, model and serial as they're formatted
    in the host_name, parse the individual attributes.
    These are needed to find the correct Managed System via the K2 API
    """
    # Split the machine type/model from the serial number.
    mtm, host_dict['serial'] = host_name.split('_', 1)
    # Save the model part.
    host_dict['model'] = mtm[4:7]
    # Save the machine type part.
    host_dict['machine'] = mtm[0:4]


def update_vios_fc_ports_in_db(context, topology, db_vios_list,
                               transaction):
    """
    Given the topology (built from data in the K2 responses) and the list of
    vioses from the database, format the data appropriately to persist the
    FC port information with their related VIOSes.
    Also, if the VIOS cluster membership has changed, update that as well.
    """
    # dict to host mapping from wwpn to fcport DOM without fabric set
    wwpn_to_dom = {}
    # Loop through the VIOSes from the DB. The assumption is that storage
    # will not be adding or removing VIOS entries in the DB as the network
    # reconciliation flow handles that. So we just key off of what is already
    # there.
    for db_vios in db_vios_list:
        db_vios_id = db_vios.id
        LOG.debug("LOOP entry: db_vios='%s', db_vios.id=%s."
                  % (db_vios.lpar_name, db_vios.id))

        if db_vios_id not in topology['vios_by_id']:
            LOG.warn(_("The Virtual I/O Server with id '%s' was"
                       " not retrieved in the "
                       "HMC reconciliation data. Skipping.") % db_vios_id)
            continue
        vios = topology['vios_by_id'][db_vios_id]
        LOG.debug("DB VIOS '%s' was matched to K2 vios: %s." %
                  (db_vios_id, vios))

        #Update the db_vios object with the cluster if needed.
        update_vios = False
        static_topo = topology['static_topo']
        if not topology['skip-update-clusters']:
            if static_topo.is_trust_criteria_met(db_vios_id):
                if 'vios_cluster' in vios:
                    k2_clust = vios['vios_cluster']['cluster_id']
                else:
                    k2_clust = None
                    # This is a trusted value, so clear out its tracked
                    # entry if any. The static topology can be used to
                    # remove or add vioses to SCGs later.
                    static_topo.remove_vios_from_cluster(db_vios_id)

                if db_vios.cluster_provider_name != k2_clust:
                    update_vios = True
                    msg = _("Updating cluster_provider_name from "
                            "'%(old_id)s' to '%(new_id)s' on VIOS DB entry "
                            "with ID [host]##[lpar_id]: '%(vios)s'.") % \
                        dict(old_id=db_vios.cluster_provider_name,
                             new_id=k2_clust, vios=db_vios_id)
                    if(db_vios.cluster_provider_name and not k2_clust):
                        LOG.warn(msg)  # cluster going away from vios
                    else:
                        LOG.info(msg)
                    db_vios.cluster_provider_name = k2_clust
                else:
                    LOG.debug("VIOS id '%s' has cluster already set "
                              "correctly." % db_vios_id)
            else:
                LOG.debug("Trust criteria not met for VIOS %s." % db_vios_id)
                # The next if check is bootstrap logic for the static topo
                # where the cluster for a VIOS never came back from K2, but
                # but it is still in the DB. We can set this as the initial
                # cluster it was a member of if it was never cached in the
                # static topology before, and on subsequent topology
                # collections, this value will go through the normal trust/
                # not-trusted determinations.
                if db_vios.cluster_provider_name and\
                        'vios_cluster' not in vios and\
                        static_topo.vios_keyed[db_vios_id]['last_cluster']\
                        is None:
                    static_topo.set_cluster_for_vios(
                        db_vios_id, db_vios.cluster_provider_name, None)
        else:
            LOG.debug("Cluster information not collected for VIOS %s." %
                      db_vios_id)

        # I see that the rmc state can thrash between busy and not busy.
        # Since network and potentially compute topology is already setting
        # these, we won't here as well, but we'll output a log message to
        # correlate to a similar message network puts out.
        if 'state' in vios.keys():
            if (vios['state'] is not None and
                    vios['state'] != db_vios.state):
                LOG.debug("discrepancy: lpar %s VIOS '%s' live state=%s, "
                          "but DB state=%s." % (vios['lpar_id'], vios['name'],
                                                vios['state'], db_vios.state))

        #Update the db_vios object with the rmc state
        if 'rmc_state' in vios.keys():
            if (vios['rmc_state'] is not None and
                    vios['rmc_state'] != db_vios.rmc_state):
                LOG.debug("discrepancy: lpar %s VIOS '%s' live rmc_state=%s, "
                          "but DB rmc_state=%s." %
                          (vios['lpar_id'], vios['name'], vios['rmc_state'],
                           db_vios.rmc_state))

        # Update the VIOS in the db if needed.
        if update_vios:
            db_vios.save(context=context, transaction=transaction)

        ################################
        # SET FC Port info as needed   #
        ################################
        # Get VIOS fc port DOM resources
        fcport_factory = storage_dom.FcPortFactory.get_factory()
        db_vios_fcports = fcport_factory.find_all_fcports_by_vios(
            context, db_vios.id, transaction=transaction)
        LOG.debug("Found %d FC ports in VIOS db entry." % len(db_vios_fcports))

        k2fcports = vios['fcports']
        # Loop through the FC Port database entries
        # and delete entries that no longer exist on the HMC
        for db_fcport in db_vios_fcports:
            LOG.debug("Processing db_fcport: %s." %
                      storage_dom.fcport_to_dict(db_fcport))
            if db_fcport.id not in k2fcports.keys():
                # The existing DB port entry was not found in the dictionary
                # of FC ports obtained through K2.
                xml = None  # for log messages
                if 'k2element' in vios:
                    xml = vios['k2element'].element.toxmlstring()
                if 'IOAdapters' in vios and (db_fcport.adapter_id in
                                             vios['IOAdapters']):
                    # Work around the problem where the K2 VIOS request does
                    # not return PhysicalFibreChannelAdapters periodically.
                    # But there was an IOAdapter element with the same
                    # DeviceName (udid) returned as for this port's adapter.
                    # We do not want to remove the existing port from the DB
                    # for this type of hiccup and it does not necessarily
                    # happen when the VIOS rmc_state is busy. Log the
                    # condition to allow future debug gathering.
                    LOG.info(_("PhysicalFibreChannelAdapter element is "
                               "missing from K2 operator feed for an existing "
                               "FC port db entry [%(port)s] where its adapter"
                               "_id matches a returned IOAdapter entry "
                               "[%(adapter)s]. Not removing the database port "
                               "entry for this case because the HMC may not "
                               "have been able to correlate FC adapters to a "
                               "temporarily unavailable VIOS. The VIOS "
                               "'%(viosn)s' has state '%(state)s' and RMC "
                               "state '%(rmc)s'.") %
                             dict(port=db_fcport.id,
                                  adapter=db_fcport.adapter_id,
                                  viosn=vios['name'], state=vios['state'],
                                  rmc=vios['rmc_state']))
                    if 'trusted' in vios and vios['trusted']:
                        # Don't both dumping if VIOS not trusted.
                        LOG.debug("VIOS topology=%s, K2 element=%s" % (vios,
                                                                       xml))
                    # need to continue instead of breaking here so another
                    # port is not left to be created in the next loop
                    continue
                ########################
                # Delete FC Port case  #
                ########################
                # K2 did not report this FCport even though it did provide
                # vios information. The normal case here is to remove it
                # from the database.
                LOG.warn(_("Removing FC port from DB: %s") %
                         storage_dom.fcport_to_dict(db_fcport))
                db_fcport.delete(context, transaction)
                if 'k2element' in vios:
                    LOG.info(_("K2 feed element used for VIOS when ports "
                               "found to be gone: %s") % xml)
            else:
                ########################
                # Update FC port case  #
                ########################
                update_needed = False
                k2fcport = k2fcports[db_fcport.id]
                LOG.debug("Found an existing port with "
                          "id '%s': %s." % (db_fcport.id, k2fcport))
                if db_fcport.name != k2fcport['name']:
                    db_fcport.name = k2fcport['name']
                    update_needed = True
                    LOG.info(_("FC Port DB update needed per 'name' prop: "
                               " %s.") % k2fcport)

                if db_fcport.enabled is None:
                    db_fcport.enabled = True
                    update_needed = True
                if db_fcport.wwpn != k2fcport['wwpn']:
                    db_fcport.wwpn = k2fcport['wwpn']
                    update_needed = True
                    LOG.info(_("FC Port DB update needed per 'wwpn' prop: "
                               " %s.") % k2fcport)
                if db_fcport.adapter_id != k2fcport['adapter_id']:
                    db_fcport.adapter_id = k2fcport['adapter_id']
                    update_needed = True
                    LOG.info(_("FC Port DB update needed per 'adapter_id' "
                               "prop: %s.") % k2fcport)
                if db_fcport.fabric is None:
                    db_fcport.fabric = "None"
                    update_needed = True
                if db_fcport.status != k2fcport['status']:
                    db_fcport.status = k2fcport['status']
                    update_needed = True

                if update_needed:
                    LOG.debug("Updating DB FC Port '%s' with K2 data: %s."
                              % (storage_dom.fcport_to_dict(db_fcport),
                                 k2fcport))
                    db_fcport.save(context=context, transaction=transaction)

                # Add port to tracking dict even if a fabric is already set
                wwpn_to_dom[db_fcport.wwpn] = db_fcport
                # Remove port from k2 dictionary to indicate it's 'processed'
                del k2fcports[db_fcport.id]
        # End for each DB FC port for the cur VIOS

        # Any ports left in k2fcports are new ones to insert in the db.
        for new_k2port in k2fcports.values():
            #########################
            # Create FC Port case   #
            #########################
            # Format the FC Port entry to match the criteria
            # of the Factory and provide defaults.
            fabric_default = "None"
            fcport_data = {'id': new_k2port['id'],
                           'vios_id': db_vios.id,
                           'adapter_id': new_k2port['adapter_id'],
                           'wwpn': new_k2port['wwpn'],
                           'name': new_k2port['name'],
                           'enabled': True,
                           'status': new_k2port['status'],
                           'fabric': fabric_default}
            LOG.info(_("Insert new FC Port in DB: %s.") % fcport_data)
            dom_fcport = fcport_factory.construct_fcport(
                context, auto_create=True, transaction=transaction,
                **fcport_data)
            # Save the FC port in the DB
            dom_fcport.save(context=context, transaction=transaction)
            wwpn_to_dom[new_k2port['wwpn']] = dom_fcport
        # End for new FC Port creation loop
    # End for each vios in DB for host being processed.

    if wwpn_to_dom.keys():
        # There are potentially ports to sync the fabric on - call helper.
        fcport_factory.sync_fcports_to_fabric(context, transaction,
                                              wwpn_to_dom)
    LOG.debug("Completed reconciliation to DB for FC Ports.")


def get_host_topology_from_db(context, host_name=None, port_tag=None,
                              provider_check_type=None):
    """
    Build a list of host topology entries based on the PVC database only.
    The logic here must align with that of the SCG DOM.to_dict_with_ports(ctx).
    :param host_name: If passed, then the topology is built for the single
                host (in case this can help optimize a flow). The host_name
                is the MTMS name, not the display name.
    :param port_tag:  If passed, then use the port_tag to further filter the
                counts of ready ports, and thus storage-ready vioses.
    :param provider_check_type: If not None, then VIOSes will NOT be considered
                'storage-ready' if the backend provider is in error
                state. A matching option has been added to the REST API
                (host-storage-topologies) so that the console can get
                'storage-ready' values that are consistent with values
                obtained from the storage-connectivity-groups API.
                Allowable values for this option are:

                    None - don't make VIOS storage readiness depend on
                           provider readiness. This is backwards compatible
                           with how the API worked before.
                    'cluster' - If a VIOS is a member of a cluster, then that
                           cluster must be not in error state for the VIOS
                           to be storage-ready.
                    'external' - There must be at least one registered SAN
                           provider not in error state for the VIOS to be
                           considered storage ready. This aligns with
                           NPIV-only connectivity.
                    'any' - If either the cluster provider or any SAN provider
                           is not in error state, then the VIOS can be
                           storage-ready, provided it passes the other
                           checks.
    :returns:   A python data structure that can be converted to a json REST
                API response for storage topology information.
    """
    #Initialize empty list
    host_list = []

    # Get storage provider information to correlate cluster providers
    # and to evaluate storage provider readiness.
    cinder_api = pvc_cinder.PowerVCVolumeAPI()
    stg_providers = cinder_api.get_stg_provider_info(context,
                                                     exclude_in_error=True)
    hmcfact = compute_dom.ManagementConsoleFactory.get_factory()

    host_set = set()  # set of hosts already seen

    #Get the ResourceTransaction (cf. DB2 session) for the nova db
    txn = hmcfact.get_resource_transaction()

    with txn.begin():
        #Get the list of HMCs from the nova database
        db_hmcs = hmcfact.find_all_hmcs(context)

        #Get the list of VIOSes once from the nova DB.
        vios_factory = storage_dom.VioServerFactory.get_factory()
        vio_servers = vios_factory.find_all_vioses_by_host(
            context, host_name, transaction=txn)
        LOG.debug("len(vio_servers) = %s." % len(vio_servers))
        #Loop through all the HMCs
        for hmc in db_hmcs:
            #Get the list of hosts that the HMC provides.
            for host in hmc['managed_hosts']:
                if host_name and host != host_name:
                    LOG.debug("Skip filtered host %s." % host)
                    continue
                if host in host_set:
                    LOG.debug("Already processed host '%s' with a previous "
                              "HMC. Continue." % host)
                    continue
                host_dict = {}
                host_set.add(host)
                host_dict['hmc_uuid'] = hmc['hmc_uuid']
                host_dict['name'] = host
                LOG.debug('host name = %s' % host)
                host_dict['port_ready_count'] = 0

                #Initialize the vios list to empty.
                vios_list = []
                host_dict['vios_ready_count'] = 0
                # Loop over known vioses
                for vio_server in vio_servers:
                    host_of_vios = vio_server.host_name
                    LOG.debug('host_of_vios = %s' % host_of_vios)
                    if (host_of_vios != host):
                        # skip since not applicable for this host
                        continue
                    fcport_factory = storage_dom.FcPortFactory.get_factory()
                    vios_dict = _get_dbvios_dict(vio_server, stg_providers,
                                                 txn, context,
                                                 fcport_factory,
                                                 provider_check_type,
                                                 port_tag, cinder_api)
                    if vios_dict['storage_ready']:
                        host_dict['vios_ready_count'] += 1

                    host_dict['port_ready_count'] += \
                        vios_dict['port_ready_count']
                    # Add the vios object to the vios list in all cases
                    # since it was decided that users would prefer to see
                    # the VIOS and address the issues it may have.
                    vios_list.append(vios_dict)
                #Add the vios list to the host object
                host_dict['vios_list'] = vios_list
                LOG.debug("host_dict = %s" % host_dict)
                #Add the host object to the host list.
                host_list.append(host_dict)
    return host_list


def _get_dbvios_dict(vio_server, stg_providers, transaction, context,
                     fcport_factory, provider_check_type, port_tag,
                     cinder_api):
    """
    Helper method to build a vios dictionary for topology data from
    the DB. vio_server is the DB VIOS DTO and stg_providers is a dictionary
    of providers that comes from the volume/cinder.py helpers for use
    when SSPs are supported. Here is an example VIOS dict that is built:
        {
         "id": "789522X_067A34B##1"
         "name": "acme109",
         "storage_ready": true,
         "lpar_id": 1,
         "total_fcport_count": 1,
         "rmc_state": "active",
         "port_ready_count": 1,
         "state": "running",
         "fcport_list": [
          {
           "status": "OK",
           "fabric": "None",
           "vio_server": "acme109",
           "available_connections": 64,
           "enabled": true,
           "adapter_id": "U78AE.001.WZS02BR-P1-C19-L1",
           "wwpn": "21000024FF4182D8",
           "id": "1aU78AE.001.WZS02BR-P1-C19-L1-T1",
           "name": "fcs0"
          },...
         ],
        }
    The most recent adds are for storage_ready, total_fcport_count, and
    port_ready_count properties which are aids to consumers that are in
    a flow where they don't need to traverse the ports and port properties.
    We do the aggregate computations for them. The 'storage_ready' value
    of true does not predict a successful deployment using this VIOS as
    there are potentially many other factors (even VIOS related) that could
    impact deployment and/or relocation.

    TODO: This method along with the caller method above have a lot of
          overlap with what is being done in the SCG DOM to_dict() and
          to_dict_with_ports() methods, as logic as progressively moved
          back into the DOM. A future item should look at pulling out the
          common pieces and not duplicating effort on the vios and host
          dictionary representations.
    """
    #Build the vios object with elements.
    vios_dict = {'lpar_id': vio_server.lpar_id,
                 'name': vio_server.lpar_name, 'id': vio_server.id,
                 'state': vio_server.state, 'rmc_state': vio_server.rmc_state}

    applicable_providers = []
    provider_ready = True  # default to true
    # Cluster provider props if applicable
    if ((vio_server.cluster_provider_name is not None) and
            (vio_server.cluster_provider_name != '')):
        prov_name = vio_server.cluster_provider_name
        vios_dict['cluster_provider_name'] = prov_name
        if prov_name in stg_providers:
            p = stg_providers[prov_name]
            vios_dict['cluster_provider_display_name'] = p['host_display_name']
            applicable_providers.append(prov_name)
            vios_dict['cluster_provider_state'] = p['backend_state']
        # Note that it's not really possible for the vios to be related
        # to an unregistered cluster provider since the association is
        # to a table of registered providers.
        else:
            try:
                # The list we originally asked for excluded those in error
                # state. Get the in-error cluster provider record now from
                # the cache.
                p = cinder_api.get_stg_provider_info(
                    context, storage_hostname=prov_name, use_cache=True)
                vios_dict['cluster_provider_state'] = p['backend_state']
                vios_dict['cluster_provider_display_name'] =\
                    p['host_display_name']
            except Exception as ex:
                # OK, maybe it wasn't in-error. It could have been deleted
                # in a timing window and we don't want to fail the API in
                # this case.
                LOG.warn(_("Could not retrieve VIOS associated cluster "
                           "provider '%(name)s': %(ex)s") %
                         dict(name=prov_name, ex=ex))
                vios_dict['cluster_provider_state'] = 'unregistered/unknown'
            if provider_check_type == 'cluster':
                provider_ready = False

    #Get the FC ports related to this vios from the DB.
    vios_fcports = fcport_factory.find_all_fcports_by_vios(
        context, vio_server.id, transaction=transaction)

    LOG.debug("len(vios_fcports) = %s " % len(vios_fcports))
    #Initialize the fcport list to empty.
    fcport_list = []
    # ok_ports will track the number of ports that are deemed 'ready' for
    # use within an SCG that may contain this VIOS. The optional passed
    # port_tag can further limit the 'ready' ports.
    ok_ports = 0
    if vios_fcports:
        for fcport in vios_fcports:
            #Build the fc port object with elements.
            port = storage_dom.fcport_to_dict(fcport)
            LOG.debug("fcport_dict = %s" % port)
            fcport_list.append(port)
            # Track number of 'good' ports
            if (port['status'].startswith("OK") and
                    port['enabled'] and
                    port['wwpn'] and len(port['wwpn']) and
                    (port_tag is None or  # 'port_tag' not in port or
                     ('port_tag' in port and
                      port_tag == port['port_tag']))):
                ok_ports = ok_ports + 1
    else:
        LOG.debug("This VIOS SCG entry is not reporting any "
                  "Fibre Channel ports: %s." % vios_dict)
    vios_dict['port_ready_count'] = ok_ports
    vios_dict['total_fcport_count'] = len(vios_fcports)
    if ok_ports:
        fc_providers = [x for x, y in stg_providers.iteritems()
                        if y['stg_type'] == 'fc']
        if not fc_providers and provider_check_type == 'external':
            provider_ready = False
        else:
            applicable_providers.extend(fc_providers)
            if provider_check_type == 'any' and not applicable_providers:
                provider_ready = False
        LOG.debug("vios_id=%s, applicable_providers=%s" %
                  (vios_dict['id'], applicable_providers))

    # Track whether this VIOS is storage-ready. This needs to be
    # clearly defined for a customer, yet the implementation needs to
    # retain the responsibility of making this true/false determination.
    # I.e. it is not up to consumers/exploiters to assume 'readiness' based
    # on documented property values - the checks here may change and it
    # is not a defect if they do, though docs may need to be updated.
    if vio_server.state == 'running'\
            and vio_server.is_rmc_state_ok()\
            and ok_ports > 0 and provider_ready:
        vios_dict['storage_ready'] = True
    else:
        vios_dict['storage_ready'] = False

    # Add the fcport list to the vios object.
    vios_dict['fcport_list'] = fcport_list
    return vios_dict


def update_scgs_vios_membership(context, host_name, db_vioses, topology):
    """
    Like most of the other methods in this module, update_scgs_vios_membership
    gets run during the periodic task to collect topology information and
    and reconcile the PVC DB with it. There are four cases:

    (1) A VIOS exists in an SCG, but does not exist in the DB. The VIOS
        will be removed from the SCG, regardless of the SCG type or other
        properties since VIOSes should always be automatically removed.
        This case is HANDLED FOR US because of the PVC DB rules! The
        membership of a SCG is implemented as a special table association
        (many-to-many) such that if a VIOS member goes away, it's link
        in the SCG definition will also ago away.

    (2) An SCG is defined for Cluster-SSP access, and a VIOS is a member
        of the SCG, the VIOS exists in the DB, but that VIOS is not
        a member of the Cluster the SCG has access to. The VIOS will be
        removed from the SCG.
        TODO: Need to revist this. It does not seem safe to remove a VIOS
              member if the SCG is associated with one or more VMs that have
              a NPIV disk attached. The SCG membership may not match what
              is in use and the VM may no longer migrate because NPIV
              VIOS connectivity is removed for target hosts when the intent
              is to remove SSP connectivity only.

    (3) An SCG is defined for Registered controller access only and has
        'auto_add_vios' set to True. A VIOS exists in the DB, but is
        not a member of the SCG. The VIOS's 'created_at' timestamp
        is compared to the SCG's 'updated_at' timestamp (or it's
        'created_at' timestamp if 'update_at' is not set). If the VIOS
        was registered after the last update of the SCG, then the
        VIOS is added to the SCG membership. This logic is not just an
        impl decision, but needs to be tied to the basic definiton of
        'auto_add_vios'. Timestamp comparisons are used because there
        is no SCG membership history being preserved.

    (4) An SCG is defined for Cluster-SSP access (it may also have
        registered controller access) and it has 'auto_add_vios' set
        to True. At some point earlier in the topology reconcilation flow
        we recorded the fact that a VIOS changed its cluster membership -
        either it was added to the SCG's cluster, or it was both removed
        from a differnt cluster and added to the SCG's cluster.
        This VIOS is not currently a member of the SCG. It will be added.
        Note that timestamp comparisons cannot be used for this case.

    Notes:
      * If no VIOSes remain as members of the SCG, the SCG does not get
        deleted in this flow. It just has an empty membership list. The
        DB lifecycle constraints could be set to delete the SCG when the
        members drop to 0, but currently there are no plans to enforce such
        a constraint since it seems better to allow the UI to present
        the situation to the admin and the admin can choose to remove the
        SCG or conclude that the situation is expected because they plan
        to add additional VIOSes later and the SCG is set to automatically
        include them.
      * Individual DB transactions will be created for each SCG
        so that problems with updating once SCG will not abort updates for
        all SCGs. It could be that an SCG no longer exists by the time the
        transaction commits for it and this needs to be silently
        handled.
      * We want to sequence intellegent checks that decide on the four
        cases intellegently so as to avoid unecessary processing.
    """
    # establish the host key
    host_key = "ALL" if host_name is None else host_name

    # Create a dictionary of vios id to vios DTO mappings.
    # Python 2.6 does not have the dict comprehention, so use for loop
    # vios_map = {get_vios_id(vios): vios for vios in db_vioses}
    vios_map = {}
    for vios in db_vioses:
        vios_map[vios.id] = vios

    # Construct a set of vios ids from the map
    cur_set = set(vios_map.keys())

    # Initialize the cached set if needed.
    # The method static data has been moved to the static_topology object.
    # Get a static var that we can use as a cache while the process runs.
    # It contains the last processed set of VIOSes per host (key) and per
    # "ALL" special key to mean all hosts. This allows the SCG reconciliation
    # to function if being called through a single compute node (host)
    # process or if updating for all hosts through a global API.
    cache_sets = topology['static_topo'].host_vios_sets
    if host_key not in cache_sets:
        cache_sets[host_key] = set()
    cache_set = cache_sets[host_key]

    # The '^' operator will yield the VIOSes that are in either set but
    # not both.
    exclusion = cur_set ^ cache_set
    LOG.debug("Exclusion=%s, cur_set=%s, cache_set=%s, skip_clust=%s." %
              (exclusion, cur_set, cache_set,
               topology['skip-update-clusters']))
    # Now if both the exclusion set of vioses is empty and cluster updates
    # are being skipped, there is nothing that needs to be done as none
    # of the 4 cases are hit.
    #
    # Note that the 'cache' comparison is not telling us that for sure
    # something changed, but just that those VIOSes need to be looked at.
    # This could be the first execution of this code after a PVC restart,
    # in which case the cache will be empty and all the vioses need to be
    # looked at.
    if len(exclusion) == 0 and topology['skip-update-clusters']:
        LOG.debug("No SCG updates needed.")
        return

    # The new_since_cache_set set computation represents vioses that are
    # being looked at for the first time, which will be all vioses the
    # first time this code is run after PVC startup.
    new_since_cache_set = (cur_set - cache_set)
    cache_sets[host_key] = cur_set  # update
    cluster_ids = [vios_map[x].cluster_provider_name for x in
                   cur_set if vios_map[x].cluster_provider_name]
    cluster_set = set(cluster_ids)
    LOG.debug("new_since_cache_set=%s, cluster_set=%s." %
              (new_since_cache_set, cluster_set))

    # Get all the SCGs from the DB
    scg_factory = storage_dom.StorageConnectivityGroupFactory.get_factory()
    scgs_found = scg_factory.find_all_scgs(context)
    #######################
    #   Main SCG Loop     #
    #######################
    for scg_found in scgs_found:
        txn = scg_factory.get_resource_transaction()
        with txn.begin():
            # Lookup the current SCG in the context of transaction for
            # potential updating.
            scg = scg_factory.find_scg_by_id(context, scg_found.id,
                                             transaction=txn)
            if scg is None:
                LOG.info(_("The storage connectivity group has been removed"
                           " since the time it was previously looked up: "
                           "'%s'." % scg_found.display_name))
                # Drop out of transaction and continue to next SCG
                continue
            LOG.debug("***Examining SCG***: %s." %
                      scg.to_dict(context=context, transaction=txn,
                                  skip_extended_lookups=True))
            scg_name = scg.display_name
            none_before = False
            if scg.cluster_provider_name and\
                    scg.cluster_provider_name in cluster_set:
                cluster_set.remove(scg.cluster_provider_name)
            ##################################
            #   Exception Handling per SCG   #
            ##################################
            try:
                if not scg.vios_ids and scg.cluster_provider_name is not None:
                    none_before = True
                # call the helper
                _scg_case_helper(context, txn, scg, new_since_cache_set,
                                 host_name, vios_map, topology['static_topo'])
                if not scg.vios_ids and none_before and scg.auto_defined:
                    LOG.info(_("Attempt removal of auto_defined storage "
                               "connectivity group '%(scg)s' with no members "
                               "for previous cluster provider ID "
                               "%(cluster)s.") %
                             dict(scg=scg_name,
                                  cluster=scg.cluster_provider_name))
                    scg_factory.delete_scg(context, scg.id)
            except Exception as ex:
                LOG.exception(ex)
                LOG.warn(_("There was an error attempting to check or "
                           "update the membership of storage connectivity "
                           "group (SCG) '%s'. Continue "
                           "to the next SCG..." % scg_name))
        LOG.debug("Transaction finished for SCG '%s'." % scg_name)
    # Want to keep this commented code for a bit. It is a work-around in
    # case things go wrong with cluster creation.
    # if cluster_set:
    #     try:
    #         clust_id = cluster_set.pop()
    #         LOG.debug("Attempt creation of SSP-based SCG for cluster id: "
    #                   "%s. Tracked clusters: %s." %
    #                   (clust_id, topology['static_topo'].cluster_keyed))
    #         n = topology['static_topo'].cluster_keyed[clust_id]\
    #             ['display_name']
    #         scg_factory.create_default_cluster_scg(context, clust_id, n)
    #     except Exception as ex:
    #         # Log and continue...another host could have already done the
    #         # work so may be ignorable.
    #         LOG.exception(ex)
    LOG.debug("Finished reconciling SCGs.")


def _scg_case_helper(context, transaction, scg, db_vios_set, host_name,
                     vios_map, static_topo):
    """
    Helper to update the SCG with a new VIOS membership list if needed.
    The different cases are evaluated here.
    """
    LOG.debug("_scg_case_helper: db_vios_set=%s, vios_to_clstr=%s." %
              (db_vios_set, static_topo.vios_keyed))
    to_add = []
    to_remove = []
    scg_name = scg.display_name
    if hasattr(scg, 'updated_at') and scg.updated_at is not None:
        scg_time = scg.updated_at
    else:
        scg_time = scg.created_at

    #######################
    #   SCG VIOS Loop     #
    #######################
    for vios in scg.vios_list:
        vid = vios.id
        vlog_ref = "'" + vios.lpar_name + "'(" + vid + ")"
        ###################
        #  CASE 2 check   #
        ###################
        if (scg.cluster_provider_name is not None and
                scg.cluster_provider_name != vios.cluster_provider_name):
            # Note: The trusted criteria would already have been met for the
            #       vios cluster membership to be changed in the DB, so that
            #       need not be checked again.
            LOG.info(_("VIOS %(vios)s is no longer a member of "
                       "cluster '%(cluster)s'. Removing the member from "
                       "storage connectivity group '%(scg)s'.") %
                     dict(vios=vlog_ref, cluster=scg.cluster_provider_name,
                          scg=scg_name))
            to_remove.append(vios)
    # Do removals here to avoid changing for loop list
    for vios in to_remove:
        scg.vios_list.remove(vios)
    ###################
    #  CASE 3 check   #
    ###################
    scg_vios_set = set(scg.vios_ids)
    if (scg.fc_storage_access and not scg.cluster_provider_name and
            scg.auto_add_vios):
        for vios_id in (db_vios_set - scg_vios_set):
            if scg_time < vios_map[vios_id].created_at:
                LOG.info(_("Newly registered VIOS with ID %(vios)s will be "
                           "added to FC/NPIV-based storage connectivity "
                           "group '%(scg)s'.") %
                         dict(vios=vios_id, scg=scg_name))
                to_add.append(vios_id)
            elif scg.auto_defined:
                # If the SCG is auto-defined at start-up, then timestamps
                # don't matter because the VIOS needs to be added regardless.
                # The SCG may have been created after VIOS reg due to a
                # timing window.
                LOG.info(_("New VIOS with ID '%(vios)s' will be added to "
                           "auto-defined FC/NPIV-based storage connectivity "
                           "group '%(scg)s'.") %
                         dict(vios=vios_id, scg=scg_name))
                to_add.append(vios_id)
    ###################
    #  CASE 4 check   #
    ###################
    if scg.cluster_provider_name is not None and scg.auto_add_vios:
        scg_prov = scg.cluster_provider_name
        for db_vios_id in static_topo.vios_keyed.keys():
            if (db_vios_id not in scg_vios_set and
                    static_topo.is_trust_criteria_met(db_vios_id) and
                    static_topo.get_cluster_for_vios(db_vios_id) == scg_prov):
                LOG.info(_("The VIOS with ID '%(vios)s' is a member of "
                           "cluster '%(cluster)s' and will be added to "
                           "storage connectivity group '%(scg)s'.") %
                         dict(vios=db_vios_id, cluster=scg_prov, scg=scg_name))
                to_add.append(db_vios_id)
            else:
                LOG.debug("Current SCG vios membership OK (or still waiting "
                          "on VIOS trust criteria to be met). db_vios_id="
                          "%s, vios_cluster=%s, scg_cluster=%s." %
                          (db_vios_id,
                           static_topo.get_cluster_for_vios(db_vios_id),
                           scg.cluster_provider_name))

    if len(to_add) == 0:
        LOG.debug("No SCG vios members to add for cases 3 and 4.")
    else:
        # For cases 3 and 4, we don't have VIOS DTOs from the current txn
        # so we need to look each up.
        vios_factory = storage_dom.VioServerFactory.get_factory()
        txn_vioses = vios_factory.find_all_vioses_by_host(
            context, host_name, transaction=transaction)
        LOG.debug("txn_vioses=%s." % str(txn_vioses))
        for id_to_add in to_add:
            txn_vios = [v for v in txn_vioses if v.id == id_to_add]
            if not txn_vios:
                LOG.warn(_("Virtual I/O Server ID '%(vios)s' was tagged for "
                           "storage connectivity group '%(scg)s' inclusion, "
                           "but is no longer found in the database.") %
                         dict(vios=id_to_add, scg=scg_name))
            else:
                scg.vios_list.append(txn_vios[0])

    ################
    # SAVE the SCG #
    ################
    if len(to_add) > 0 or len(to_remove) > 0:
        scg.save(context=context, transaction=transaction)
        LOG.info(_("Performed save on updated storage connectivity group "
                   "'%(scg)s' with VIOS list: %(vioses)s.") %
                 dict(scg=scg_name, vioses=scg.vios_ids))
    else:
        LOG.debug("SCG '%s' not changed." % scg_name)
