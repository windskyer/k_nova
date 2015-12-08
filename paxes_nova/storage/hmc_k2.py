#
#
# =================================================================
# =================================================================

"""
IBM PowerVC Storage utilities for retrieving and parsing HMC (K2)
response data for topology resources.
"""

import time
from nova.openstack.common import log as logging
from powervc_k2.k2operator import K2Error
from powervc_nova import _
from oslo.config.cfg import CONF
import powervc_nova.objects.storage.dom as storage_dom
from powervc_nova.virt.ibmpowervm.vif.hmc import K2_RMC_READ_SEC
from powervc_nova.virt.ibmpowervm.vif.hmc import K2_MNG_SYS_READ_SEC

LOG = logging.getLogger(__name__)
CONF.import_opt('ibmpowervm_register_ssps', 'powervc_nova.virt.ibmpowervm.hmc')
CONF.import_opt('ibmpowervm_ssp_hmc_version_check',
                'powervc_nova.virt.ibmpowervm.hmc')

VFCM_PORTELEMT_LEN = 2
XAG_DEFAULT = [None]
XAG_VIOS = ['ViosStorage']


def k2_read(operator, rootType, rootId=None, childType=None,
            suffixType=None, suffixParm=None, timeout=-1, age=-1, xag=[]):
    """
    We provide our own read abstraction interface on top of K2's here.
    This can be enhanced as needed, but it is mainly for our own custom
    logging.
    NOTES: This method will not be called if cached K2 feeds are being used
           from the compute driver.
           If this is called from the compute periodic task, then the
           operator should encapsulate a common session that is always
           re-used.
           The operator may do its own internal caching so an over-the-wire
           request may not be performed.
    """
    # Log the call
    LOG.debug("\nK2_OPER_REQ - rootType=%(rootType)s, rootId=%(rootId)s, "
              "childType=%(childType)s, suffixType=%(suffixType)s, "
              "suffixParm=%(suffixParm)s, timeout=%(timeout)d, age=%(age)d, "
              "xag=%(xag)s, ...operator_obj=%(operator)s" % locals())
    # Make the K2 call.
    ticks = time.time()
    resp = operator.read(rootType, rootId=rootId, childType=childType,
                         suffixType=suffixType, suffixParm=suffixParm,
                         timeout=timeout, age=age, xag=xag)
    # Set up timing object
    global logTiming
    try:
        logTiming
    except NameError:
        logTiming = restTiming()  # init if it does not exist yet
    # Log that the response was received. Note there is a special logger
    # that can be enabled for the operator to log the response itself.
    logTiming.log_return(time.time() - ticks, operator, rootType, rootId,
                         childType, suffixType)
    # return
    return resp


class restTiming():
    """ Simple class to track and log the timing of a K2 rest call """
    def __init__(self):
        self.hashdict = {}

    def log_return(self, seconds, operator, rootType,
                   rootId=None, childType=None, suffixType=None):
        # Create a hash key from a few pieces of info so that we can
        # compare query timings between 'types' of calls. Note that
        # operator.session.host will contain the HMC IP/hostname.
        pieces = [operator.session.host, rootType, rootId, childType,
                  suffixType]
        key = ''.join(map(lambda x: str(x) + "_" if x is not None else "*",
                          pieces))
        if key not in self.hashdict:
            self.hashdict[key] = {'samples': 1, 'sum': seconds}
        else:
            self.hashdict[key]['samples'] += 1
            self.hashdict[key]['sum'] += seconds
        avg = self.hashdict[key]['sum'] / self.hashdict[key]['samples']
        LOG.debug("\nK2_OPER_Timing=%.3f   Call_hash=%s, average=%.3f, "
                  "avg_delta=%+.3f, entry=%s.\n" %
                  (seconds, key, avg, seconds - avg, self.hashdict[key]))


def _find_managed_system(oper, host_dict):
    """
    Finds the ManagedSystem entry for this specific host that the HMC is
    managing.

    :param oper: The HMC operator for interacting with the HMC.
    :param host_dict: has the machine, model, and serial info for
                      managed system to look up
    :return: The K2 object for the managed system
    """
    # Get the ManagedSystem entry we want by Machine type, model, and serial
    # from the K2 API. We use the 'search' suffix to achieve this.
    k2resp = None
    try:
        suffix = '(MachineType==%s&&Model==%s&&SerialNumber==%s)' % \
            (host_dict['machine'], host_dict['model'], host_dict['serial'])
        k2resp = k2_read(oper, 'ManagedSystem', suffixType='search',
                         suffixParm=suffix, timeout=K2_MNG_SYS_READ_SEC,
                         xag=XAG_DEFAULT)
    except Exception as ex:
        LOG.exception(ex)

    if k2resp is None:
        LOG.error(_("Managed System K2 response was none or failed."))
        return None

    xpath = './MachineTypeModelAndSerialNumber/SerialNumber'
    entries = k2resp.feed.findentries(xpath, host_dict['serial'])
    if entries is None:
        LOG.warn(_("Managed System HMC response did not have any entries "
                   "for host '%s'.") % host_dict)
        return None

    # Confirm same model and type
    machine_xpath = './MachineTypeModelAndSerialNumber/MachineType'
    model_xpath = './MachineTypeModelAndSerialNumber/Model'
    for entry in entries:
        entry_machine = entry.element.findtext(machine_xpath)
        entry_model = entry.element.findtext(model_xpath)
        if (entry_machine == host_dict['machine'] and
                entry_model == host_dict['model']):
            host_dict['uuid'] = entry.properties['id']
            return entry
    LOG.warn(_("Managed System HMC response did not have an 'entry' "
               "element for host '%s'.") % host_dict)
    return None


def collect_host_vios_feeds(oper, topology):
    """
    This is an alternate method to the collect_vios_topology() method below.
    Instead of looping over the associated VIOS partitions for a
    ManagedSystem, we get the feed for all its VIOSes, then loop over the
    feed entries. This flow is for when we know the uuid of the host.
    The topology dictionary passed is updated with the collected and parsed
    information.
    """
    host_ref = topology['nova_host'].mtm_serial
    LOG.debug('Obtaining VIOS feed for Managed System: %s' % host_ref)
    k2resp = None
    try:
        k2resp = k2_read(oper, 'ManagedSystem',
                         rootId=topology['nova_host'].uuid,
                         childType='VirtualIOServer',
                         timeout=K2_MNG_SYS_READ_SEC, xag=XAG_VIOS,
                         age=topology['max_cache_age'])
    except Exception as ex:
        log_k2ex_and_get_msg(ex, _("Cannot get VIOSs for host %s.") % host_ref,
                             topology)
        return topology

    if k2resp is None:
        # should not get here
        msg = _("Managed System K2 response was none for host: "
                "%s.") % host_ref
        LOG.error(msg)
        topology['error'] = [msg]
        return topology

    if not k2resp.feed or not k2resp.feed.entries:
        LOG.info(_("No VIOS entries for host: %s") % host_ref)
        return topology

    # Loop over all vioses found
    num = 0
    for vios_entry in k2resp.feed.entries:
        num += 1
        # call the helper to get a dict for this vios entry
        vios_info = get_vios_info(vios_entry)
        vios_info['id'] = get_vios_id(host_ref, vios_info)
        # Add the vios entry to both indexes
        topology['vios_by_id'][vios_info['id']] = vios_info
        topology['vios_by_uuid'][vios_info['uuid']] = vios_info
        if 'state' in vios_info and vios_info['state'] == "running" and\
                'rmc_state' in vios_info and\
                vios_info['rmc_state'] == "active":
            topology['vios_num_trusted'] += 1
            vios_info['trusted'] = True
        else:
            # Some VIOS information like cluster membership and FC ports
            # should not be trusted if state info is not all OK.
            vios_info['trusted'] = False
        # Note the trust state marking below only makes a difference if the
        # vios is being tracked as a prior cluster member.
        if not topology['skip-update-clusters']:
            # The static_topo structure is not syncronized, so we skip
            # changing it if the 'skip' value is present, which deploy
            # processing will always set. This avoids periodic task
            # collections colliding with deploy collections.
            topology['static_topo'].mark_vios_trust_state(vios_info['id'],
                                                          vios_info['trusted'])
        LOG.debug("Added VIOS dict entry to topology: %s" % vios_info)
    LOG.debug("Returning %d trusted VIOS entries out of %d collected for "
              "compute host %s." % (topology['vios_num_trusted'], num,
                                    host_ref))
    return topology


def collect_vios_topology(oper, topology, host_dict):
    """
    Connect to the HMC K2 API and request the Managed System information.
    The Managed System is represented by K2 Response, K2 Entry, and K2 Element
    class objects.  The data for the VIOS can be obtained by traversing
    the object hierarchy. The topology dictionary passed is updated with the
    obtained and parsed info.
    """
    LOG.debug('Listing VIOSes by Managed System.')
    # call the helper to get the K2 managed system
    managedSysEntry = _find_managed_system(oper, host_dict)
    if managedSysEntry is None:
        return topology  # Nothing to do

    associatedVirtualIOServers = \
        managedSysEntry.element.find('./AssociatedVirtualIOServers')
    if associatedVirtualIOServers is None:
        LOG.debug("There are no AssociatedVirtualIOServers from K2 for host: "
                  "%s." % host_dict)
        return topology
    vios_links = associatedVirtualIOServers.findall('./link')
    if vios_links is None:
        LOG.warn(_("HMC Problem: Associated VIOSes were returned, but the "
                   "links are missing."))
        return topology
    # LOOP over the VIOS links
    for vios_link in vios_links:
        href = vios_link.get('href')
        if href is None:
            LOG.error(_('vios_link \'%s\' has no href element.')
                      % vios_link.gettext())
            continue

        LOG.debug('Found href property: %s.' % href)
        # Split the URL into elements.
        href_elements = href.split('/')

        # Ensure the list has at least one element.
        if len(href_elements) == 0:
            LOG.error(_("VIOS href property '%s'"
                        " does not have any elements." % href))
            continue
        # We only care about the last element in
        # the list, which should be the uuid.
        if href_elements[-1] is not None:
            vios_uuid = href_elements[-1]
            LOG.debug("Found vios uuid: %s" % vios_uuid)

            # Request the VIOS data from the K2 operator
            viosResponse = k2_read(oper, 'VirtualIOServer',
                                   rootId=vios_uuid,
                                   timeout=K2_RMC_READ_SEC, xag=XAG_VIOS,
                                   age=topology['max_cache_age'])
            if viosResponse is None:
                LOG.warn(_("Unexpected HMC response condition: No VIOS "
                           "Element found for VIOS '%s'.") % vios_uuid)
                continue

            # Get the rest of the VIOS information
            vios_info = get_vios_info(viosResponse.entry)
        else:
            LOG.error(_("Error parsing VIOS uuid from href '%s'.") % href)
            continue  # Don't append if vios props aren't set

        # Add current vios dict to the mapping by the 'id' impl we define.
        vios_info['id'] = get_vios_id(host_dict['name'], vios_info)
        if vios_info['id'] in topology['vios_by_id']:
            LOG.debug("VIOS %s already collected by another HMC. Skip." %
                      vios_info)
            continue
        # Add the vios entry to both indexes
        topology['vios_by_id'][vios_info['id']] = vios_info
        topology['vios_by_uuid'][vios_info['uuid']] = vios_info
        if 'state' in vios_info and vios_info['state'] == "running" and\
                'rmc_state' in vios_info and\
                vios_info['rmc_state'] == "active":
            topology['vios_num_trusted'] += 1
            vios_info['trusted'] = True
        else:
            vios_info['trusted'] = True
        # Note the trust state marking below only makes a difference if the
        # vios is being tracked as a prior cluster member.
        topology['static_topo'].mark_vios_trust_state(vios_info['id'],
                                                      vios_info['trusted'])
        LOG.debug("Added VIOS dict entry to topology: %s" % vios_info)
    # End for vios loop
    return topology


def get_vios_info(vios_entry):
    """
    Get the specific data for the VIOS from the K2 VIOS entry.
    """
    # Initialize the empty dictionary.
    vios_info = {}
    if not vios_entry:
        LOG.warn(_("No Virtual I/O Servers returned in HMC response."))
        return vios_info

    vios_info['k2element'] = vios_entry  # track for later logging
    # Get the Partition Name Element
    partitionNameElement = \
        vios_entry.element.find('./PartitionName')
    # If the Partition Name element exists
    if partitionNameElement is not None:
        # Save the partition name into the vios info object.
        vios_info['name'] = partitionNameElement.gettext()
    else:
        vios_info['name'] = None

    vios_info['uuid'] = uuid = vios_entry.properties['id']
    LOG.debug("Processing VIOS partition '%s' with uuid: %s" %
              (vios_info['name'], uuid))
    #Get the Partition State Element
    partitionStateElement = \
        vios_entry.element.find('./PartitionState')

    if partitionStateElement is not None:
        #Save the partition State into the vios info object.
        vios_info['state'] = partitionStateElement.gettext()

    #Get the RMC State Element
    RMCStateElement = \
        vios_entry.element.find('./ResourceMonitoringControlState')

    if RMCStateElement is not None:
        #Save the RMC State into the vios info object.
        vios_info['rmc_state'] = RMCStateElement.gettext()

    # Get the Partition ID Element.
    partitionIDElement = vios_entry.element.find('./PartitionID')
    # If the partition ID Element exists
    if partitionIDElement is not None:
        # Save the partition id to the vios info object.
        vios_info['lpar_id'] = partitionIDElement.gettext()

        # For each VIOS, also call the helper method to extract
        # the FC Port info from call already done.
        LOG.debug("Getting FC ports for vios '%s'." % uuid)
        vios_info['fcports'] = parse_fcports(vios_entry, vios_info)
    else:
        LOG.warn(_("HMC Problem: No PartitionID element for VIOS '%s'.") %
                 uuid)

    return vios_info


def parse_fcports(vios, vios_info):
    """
    Get a list of the FC Ports related to a specific VIOS.
    'vios' passed in is the K2 response VIOS entry of a previous call.
    We traverse the hierarchy of objects to find the desired
    information.
    """
    LOG.debug('Extracting FC Ports from VIOS K2 entry.')
    # Create an empty dictionary for the FC port mapping (UDID --> port info)
    fcports = {}
    # Traverse the hierarchy to find the FC port information.
    # First, check under PhysicalFibreChannelAdapter
    adapterPath = str('./PartitionIOConfiguration/ProfileIOSlots/'
                      'ProfileIOSlot/AssociatedIOSlot/RelatedIOAdapter/'
                      'PhysicalFibreChannelAdapter')
    fcAdapters = vios.element.findall(adapterPath)
    if fcAdapters:
        LOG.debug('Found element(s): %s.' % adapterPath)
        for fcAdapter in fcAdapters:
            portElements = fcAdapter.findall(
                './PhysicalFibreChannelPorts/PhysicalFibreChannelPort')
            if portElements:
                for portElement in portElements:
                    LOG.debug('Found PhysicalFibreChannelPort element.')
                    _parse_single_fc_port(portElement, fcports, fcAdapter)
            else:
                LOG.debug("PhysicalFibreChannelPort elements not found for "
                          "vios '%s'." % vios_info['name'])
    else:
        LOG.debug("PhysicalFibreChannelAdapter elements not found for vios "
                  "'%s'." % vios_info['name'])

        # Check for the parent RelatedIOAdapters as we have seen cases
        # where these exist for the FC adapters, but the FC adapter children
        # elements are not returned. We can log this case.
        ioAdaptPath = str('./PartitionIOConfiguration/ProfileIOSlots/'
                          'ProfileIOSlot/AssociatedIOSlot/RelatedIOAdapter/'
                          'IOAdapter')
        ioAdapters = vios.element.findall(ioAdaptPath)
        if ioAdapters:
            vios_info['IOAdapters'] = []
            for ioAdapter in ioAdapters:
                if ioAdapter.find('./DeviceName') is not None:
                    adapterID = ioAdapter.find('./DeviceName').gettext()
                    desc = ioAdapter.find('./Description').gettext()
                    vios_info['IOAdapters'].append(adapterID)
                    if 'Fibre' in desc:
                        LOG.debug("No FC adapters reported, but found "
                                  "IOAdapter %s with description: %s." %
                                  (adapterID, desc))
        # Removed code that scans through VirtualFibreChannelMappings
        # if no physical adapter elements are found as this code is
        # obsolete. With the current K2 schema we cannot fall back on this
        # since there may be no virtual machines and thus no mappings (but
        # we still need to collect the port inventory).

    # Return the mapping of all the ports for the VIOS.
    LOG.debug("Finished parsing FC ports on VIOS: %s" % vios_info['name'])
    return fcports


def _parse_single_fc_port(portElement, fcports, adapterElement=None):
    """
    If portElement is a valid, NPIV-capable FCPort, add its information
    to fcports as a new dictionary port entry.
    NOTE: This function modifies 'fcports' by adding an element for a
    suported FC port, rather than returning a dictionary to add later.
    Could refactor in the future to be more openstack-esque.
    """
    uniqueDeviceIDElement = portElement.find('./UniqueDeviceID')
    portNameElement = portElement.find('./PortName')
    # AdapterUUID have been removed from schema. Keep it
    # as a placeholder.
    # adapter_uuidElement = portElement.find('./AdapterUUID')
    wwpnElement = portElement.find('./WWPN')
    total_portsElement = portElement.find('./TotalPorts')
    avail_portsElement = portElement.find('./AvailablePorts')
    adapterID = '[unknown]'
    status = "Down"
    # use exception to find invalid attribute.
    try:
        if adapterElement is not None:
            # adapter id is not needed for Paxes at this time,
            # it is part of the DB DTO for the fcport object, and if the
            # parent adapter element is passed, we use the DeviceName from
            # that.
            adapterID = adapterElement.find('./DeviceName').gettext()
        # The portElement contains non-NPIV ports which
        # has no TotalPorts and AvailablePorts attributes.
        # Filter out non-NPIV ports other wise VFC map
        # will fail.
        fcport = {'id': uniqueDeviceIDElement.gettext(),
                  'name': portNameElement.gettext(),
                  'adapter_id': adapterID,
                  'wwpn': wwpnElement.gettext()
                  }

        # WARNING! We need to be very careful in checking k2 elements.
        #   The following code line is not equivalent to:
        #        if total_portsElement and avail_portsElement
        #   This is because these K2 elemnts have a __len()__ method that
        #   evaluates to 0, meaning they have no nested entries. But the
        #   objects still exist!
        if total_portsElement is not None and avail_portsElement is not None:
            fcport['total_vports'] = int(total_portsElement.gettext())
            fcport['available_vports'] = int(avail_portsElement.gettext())
            if fcport['available_vports'] > 0:
                # put number of VFCs left on the port for OK status as
                # this info can be split out and used for API info.
                status = "OK:%d" % fcport['available_vports']
            else:
                status = "Full"
        else:
            LOG.debug("Not a currently supported FC Port since "
                      "TotalPorts and AvailablePorts are missing properties"
                      "from the element: %s" % portElement.toxmlstring())
            status = "Unsupported/Offline"
        fcport['status'] = status
        if not None in fcport.values():
            LOG.debug("Adding FC port: %(fcport)s" % locals())
            fcports[fcport['id']] = fcport
        else:
            LOG.info("HMC Problem: Not adding FC Port, which has a 'None' "
                     "value: %(fcport)s") % dict(fcport=fcport)
    except Exception:
        # One of the required port elements are missing.
        LOG.warn(_("HMC Problem: Found non-NPIV capable port due to "
                   "unexpected format. Skip portElement: %(portelem)s") %
                 {'portelem': portElement.toxmlstring()})


def log_k2ex_and_get_msg(ex, prefix, topology):
    """ LOG K2 exception and extracted message. Return NLS message """
    LOG.exception(ex)
    detail = {}
    k2msg = _("None")
    if isinstance(ex, K2Error) and ex.k2response:
        detail['Request_headers'] = ex.k2response.reqheaders
        detail['Response_headers'] = ex.k2response.headers
        detail['Response_body'] = ex.k2response.body
        detail['Response_status'] = ex.k2response.status
        if hasattr(ex.k2response, 'k2err'):
            m = ex.k2response.k2err.find('./Message')
            if m is not None:
                k2msg = m.text
    msg = _("%(prefix)s ***K2 Operator Error***: %(ex_msg)s  [K2 Error body "
            "Message: %(k2msg)s]") %\
        dict(prefix=prefix, ex_msg=ex, k2msg=k2msg)
    LOG.error(msg)
    if detail:
        LOG.error(_("Error details: %s") % detail)
    if topology is not None:
        if 'error' in topology:
            topology['error'].append(msg)
        else:
            topology['error'] = [msg]
    return msg


def map_vios_to_cluster(oper, topology):
    """
    Given a K2 operator connection to an HMC, retrieve all the VIOS
    Clusters and loop over the member VIOSes, add the membership info
    to the passed topology data structure. This mapping
    will subsequently be used in the DB reconciliation to update the
    cluster membership field on the VIOS DB resource if needed.
    """
    if topology['skip-update-clusters']:
        LOG.debug("Skip getting Cluster feed info. Could be during deploy.")
        return
    if topology['vios_num_trusted'] == 0:
        LOG.debug("No VIOSes are reporting a trusted state for collection "
                  "sequence %d. Skip getting Cluster feed." % topology['seq'])
        topology['skip-update-clusters'] = True
        return
    #See if Registering Clusters is disabled in this Environment, if it
    #is then it isn't worth the Performance hit to query Clusters from K2
    if not CONF.ibmpowervm_register_ssps or \
       cluster_registration_verification(oper) is False:
        LOG.debug("Cluster registration is disabled, so no HMC cluster "
                  "retrieval.")
        topology['skip-update-clusters'] = True
        return

    LOG.debug("Retrieving all clusters and VIOS members for current hmc.")

    # Get the root cluster object from the K2 API.
    # NOTE: In HMC 810, the Cluster info does not have events and so the
    #       caching is not event based. The K2 operator will just employ
    #       a small fixed timeout for the Cluster cache.
    try:
        k2resp = k2_read(oper, 'Cluster', xag=XAG_DEFAULT,
                         age=topology['max_cache_age'])
    except Exception as ex:
        log_k2ex_and_get_msg(ex, _("Cannot retrieve any VIOS clusters."),
                             topology)
        # We want to skip updating the cluster providers if there is an error
        topology['skip-update-clusters'] = True
        return

    if len(k2resp.feed.entries) == 0:
        LOG.debug("No cluster entries returned from HMC.")
    cluster_members = {}
    topo_factory = storage_dom.HostStorageTopologyFactory.get_factory()
    static_topo = topo_factory.get_static_topology()

    # Traverse the hierarchy structure to find the VIOS objects.
    # The structure is:
    # k2resp --> entry --> Node --> Node --> VirtualIOServer
    for i in range(len(k2resp.feed.entries)):
        cluster = k2resp.feed.entries[i]
        cluster_id = cluster.element.findtext('./ClusterID')
        if cluster_id is None or len(cluster_id) == 0:
            LOG.warn(_("HMC Problem: Cluster entry does not have a ClusterID. "
                       "Skipping. Entry is '%s'." % cluster.gettext()))
            continue
        cluster_members[cluster_id] = set()
        cluster_name = cluster.element.findtext('./ClusterName')
        LOG.debug("Cluster entry #%d: id=%s, display_name=%s." %
                  (i, cluster_id, cluster_name))
        backend_id = cluster.properties['id']
        viosElements = cluster.element.findall('./Node/Node/VirtualIOServer')
        if not viosElements:
            # This may not be possible.
            LOG.info(_("The cluster '%s' has no member VIOSs reporting.") %
                     cluster_name)
        for j in range(len(viosElements)):
            vios_elmt = viosElements[j]
            href = vios_elmt.get('href')
            if href is None:
                LOG.warn(_("HMC Problem: VirtualIOServer element has no "
                           "'href': %s." % str(vios_elmt)))
                continue

            LOG.debug("Found href for VirtualIOServer member #%d: %s."
                      % (j, str(href)))
            # Split the URL into elements.
            href_components = href.split('/')
            # Ensure the list has at least one element.
            if len(href_components) > 0:
                # We only care about the last element in
                # the list, which should be the uuid.
                vios_uuid = href_components[-1]
                LOG.debug("Found uuid: '%s'." % vios_uuid)
                vios_cluster = {'cluster_id': cluster_id,
                                'cluster_name': cluster_name,
                                'backend_id': backend_id}
                if vios_uuid in topology['vios_by_uuid']:
                    vios = topology['vios_by_uuid'][vios_uuid]
                    vios['vios_cluster'] = vios_cluster
                    static_topo.set_cluster_for_vios(vios['id'], cluster_id,
                                                     cluster_name)
                    if not vios['trusted']:
                        # For debug purposes only
                        LOG.debug("VIOS is reporting cluster membership "
                                  "even though its state information in a "
                                  "separate K2 response has it marked not "
                                  "trusted: %s." % vios)
                else:
                    LOG.debug("Clustered VIOS uuid %s not under host for this "
                              "topology collection. Cluster = %s" %
                              (vios_uuid, vios_cluster))
        # end for each vios element

    # Log a missing cluster.
    for cluster in static_topo.cluster_keyed.values():
        if cluster['set_cluster_seq'] != static_topo.sequence_num:
            LOG.info(_("The cluster %(cluster_name)s, inventoried during "
                       "prior topology collection sequence %(old)d, did not "
                       "report members this time [%(new)d].") %
                     dict(cluster_name=cluster['display_name'],
                          old=cluster['set_cluster_seq'],
                          new=static_topo.sequence_num))
    LOG.debug("Tracked cluster membership: %s." % static_topo.cluster_keyed)


def cluster_registration_verification(oper):
    """Method to verify the HMC level is good for cluster reg. support"""
    if not CONF.ibmpowervm_ssp_hmc_version_check:
        return True
    else:
        try:
            #Get the ManagementConsole information from the K2 API.
            mc_info = oper.read('ManagementConsole')
            #Parse the feed for the HMC version info
            ver_info = mc_info.feed.entries[0].element.find('VersionInfo')
            #Get the maintenance number from the version info
            maintenance = ver_info.findtext('Maintenance')
            #Get the minor number from the version info
            minor = ver_info.findtext('Minor')
            #Get the version number from the version info
            version = ver_info.findtext('Version')
            #Combine to get the HMC release version
            release = version + maintenance + minor
            LOG.info(_("Management Console release is '%s'.") % release)
            return int(release) >= 810
        except Exception as exc:
            LOG.exception(exc)
            LOG.error("There was an error getting the HMC release version")
            return True


def get_vios_id(host_name, vios_dict):
    """ Generate the VIOS ID for REST API consumption based on K2 data """
    # When the VIOS uuid gets persisted with the vios, that could be used
    # by itself and this impl can change to address that field.
    return host_name + "##" + str(vios_dict['lpar_id'])
