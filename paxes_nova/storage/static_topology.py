# vim: tabstop=4 shiftwidth=4 softtabstop=4
# =================================================================
# =================================================================

from nova.openstack.common import log as logging
from paxes_nova import _


LOG = logging.getLogger(__name__)
# The following number could be configurable, but I don't see a
# need for that at this point. If VIOSes were collected in a trusted
# state for 2 times in a row and don't report in a cluster, then
# that should be sufficient to trust that the VIOS is not a member.
DESIRED_TRUSTED_TIMES = 2


class StaticStgTopo():
    """
    This Static storage topology class is intended to maintain some
    topology state information that is needed across topology collections.
    It is intended to have a singleton instance for the process and can
    be used by the singleton HostStorageTopolgoyFactory instance for the
    process. For a particular storage topology collection activity, use
    the HostStorageTopology DOM (to-do). The memory footprint of the data
    tracked by this class should be kept minimal.
    Data synchronization is not needed for this class as it is only updated
    in the host periodic task for collecting and reconciling topology.
    """

    def __init__(self):
        """ Initialize singleton instance vars. These have been collected
            from several different static locations being used in the past.
            TODO: vios_keyed, cluster_keyed, and vios_sets can likely be
                  consolidated into one structure in the future.
        """
        self.vios_keyed = {}  # vios-to-cluster information
        self.cluster_keyed = {}  # cluster-to-vios mapping
        self.sequence_num = 0  # Topo collection number since process start
        self.hmc_list = None
        self.host_vios_sets = {}  # host to vios map for SCG membership track

    def bump_topology_sequence(self):
        """ Bumps the host storage topoology collection sequence for this
            process and returns the current sequence number.
        """
        self.sequence_num += 1
        return self.sequence_num

    def set_cluster_for_vios(self, vios_id, cluster_id, cluster_name):
        """
        When a VIOS reports as a member of a cluster, this method is called
        to update the tracked mappings for the vios and cluster.
        """
        # first update the vios_keyed dict
        if vios_id not in self.vios_keyed:
            self.vios_keyed[vios_id] = {}
            old = self.vios_keyed[vios_id]['last_cluster'] = None
        else:
            old = self.vios_keyed[vios_id]['last_cluster']
        if old != cluster_id:
            LOG.info(_("VIOS id %(id)s changed membership from cluster ID "
                       "%(old)s to %(new)s with display name '%(name)s'.") %
                     dict(id=vios_id, old=old, new=cluster_id,
                          name=cluster_name))
            self.vios_keyed[vios_id]['last_cluster'] = cluster_id
            # remove from the cluster side too
            if old is not None and old in self.cluster_keyed:
                if vios_id in self.cluster_keyed[old]['set']:
                    self.cluster_keyed[old]['set'].remove(vios_id)
        ###################################################################
        # set_cluster_seq is the collection sequence number that the VIOS
        # last reported the cluster membership for.
        # trust_seq is reset to 0 when the VIOS reports as a member for a
        # Cluster feed request. It is bumped up independently during a VIOS
        # feed request (which occurs prior to cluster feed in a topology
        # collection) if the VIOS has a good state and rmc_state.
        # This means that if the VIOS has not reported as being a member of
        # the cluster for some number of iterations, but the trust_seq
        # has bumped up to some small number, then we can "trust" that
        # the vios really is not a member of the cluster, and not just
        # experiencing a connectivity problem due to the network or heavy
        # load.
        ###################################################################
        self.vios_keyed[vios_id]['trust_seq'] = 0  # reset
        if cluster_id is None:
            self.vios_keyed[vios_id]['set_cluster_seq'] =\
                (self.sequence_num - 1)  # set sequence in past for None case
            return  # Don't need to update cluster_keyed dict.
        self.vios_keyed[vios_id]['set_cluster_seq'] = self.sequence_num

        # Now update the cluster_keyed dict
        if cluster_id not in self.cluster_keyed:
            entry = {'set_cluster_seq': self.sequence_num, 'set': set()}
            self.cluster_keyed[cluster_id] = entry
        else:
            entry = self.cluster_keyed[cluster_id]
        entry['display_name'] = cluster_name
        LOG.debug("Vios_id=%s, Vios_keyed after update=%s, Cluster entry "
                  "before update for cluster %s: %s." %
                  (vios_id, self.vios_keyed[vios_id], cluster_id, entry))
        if entry['set_cluster_seq'] != self.sequence_num:
            # new topology collection sequence - reset membership
            entry['set'] = set()
            entry['set'].add(vios_id)
            entry['set_cluster_seq'] = self.sequence_num
            LOG.debug("Reset %s cluster membership for sequence %d to %s." %
                      (cluster_id, self.sequence_num, entry['set']))
        else:
            entry['set'].add(vios_id)
            LOG.debug("Add VIOS %s to cluster %s: %s." %
                      (vios_id, cluster_name, entry['set']))

    def get_cluster_for_vios(self, vios_id):
        """
        Return the mapped cluster information dictionary as keyed by the
        VIOS ID passed in.
        """
        if vios_id not in self.vios_keyed:
            return None
        if 'last_cluster' not in self.vios_keyed[vios_id]:
            return None
        return self.vios_keyed[vios_id]['last_cluster']

    def remove_vios_from_cluster(self, vios_id):
        """ Remove the vios entry from the static tracked dictionaries of
            this class. We could keep them around for debug purposes, but
            in general I think they would clutter things up.
            Assert that this is not called during a topology collection
            sequence prior to get_clusters_in_ques() being called as a
            removed entry could negatively impact that method's computation.
        """
        if vios_id in self.vios_keyed:
            LOG.info(_("Removing tracked VIOS cluster entry for ID %s.") %
                     self.vios_keyed[vios_id])
            del self.vios_keyed[vios_id]
            # remove from the cluster side too
            for c in self.cluster_keyed.values():
                if vios_id in c['set']:
                    c['set'].remove(vios_id)

    def mark_vios_trust_state(self, vios_id, is_trusted):
        """
        Method to independently bump the trust sequence of a VIOS or
        reset it to 0 if not trusted.
        """
        if vios_id not in self.vios_keyed:
            # initialize the entry
            self.set_cluster_for_vios(vios_id, None, None)

        if is_trusted:
            self.vios_keyed[vios_id]['trust_seq'] += 1
        else:
            self.vios_keyed[vios_id]['trust_seq'] = 0
        LOG.debug("VIOS %s: %s" % (vios_id, self.vios_keyed[vios_id]))

    def is_trust_criteria_met(self, vios_id):
        """ Return whether the tracked VIOS has met the trust criteria """
        if vios_id in self.vios_keyed:
            v = self.vios_keyed[vios_id]
            return (v['set_cluster_seq'] == self.sequence_num or
                    v['trust_seq'] >= DESIRED_TRUSTED_TIMES)
        LOG.debug("VIOS id %s is not being tracked for membership." % vios_id)
        return True  # if vios not tracked, then it is trusted by def

    def get_clusters_in_ques(self, dbhost_clusters):
        """
        Compute and return the set of clusters that at one time contained
        VIOS members, which now do not meet the trust criteria such that
        we are not sure if they remain members of the cluster.
        Note that if a VIOS had a non-trusted state since the compute host
        process started, then we aren't tracking what the prior cluster
        membership is (as persisted in nova DB). If clusters from the DB
        are being examined and they do not appear to be previously referenced
        by the vios data structure here, then they should also be considered
        in-question, and they are added to the set.
        """
        questionable = set()
        for vios_id, v_entry in self.vios_keyed.iteritems():
            if v_entry['last_cluster']:
                if not self.is_trust_criteria_met(vios_id):
                    LOG.debug("VIOS id %s cluster membership is not trusted "
                              "in collection sequence %d." %
                              (vios_id, self.sequence_num))
                    questionable.add(v_entry['last_cluster'])
        # Any to add from database that we've never had trusted data for?
        # Can't answer this without keeping track of data for all prev
        # clusters. Might be a to-do, but should not be necessary since if
        # there was a cluster query error, then we should not get this far.
        #questionable.update(set(dbhost_clusters) - vios_cluster_set)
        LOG.debug("Return questionable clusters: %s." % questionable)
        return questionable
