#
#
# =================================================================
# =================================================================
'''
@author: dnip

An API Extension for being able to Report Additional Hypervisor State of a
OS-Hypervisor.
'''

from os import listdir
from nova import compute
from nova import db
from nova.api.openstack import extensions
from nova.api.openstack import wsgi
from nova.api.openstack import xmlutil
from nova.openstack.common import jsonutils
from nova.openstack.common import log as logging

LOG = logging.getLogger(__name__)
authorize = extensions.extension_authorizer('compute', 'hypervisor_state')


class HypervisorStateTemplate(xmlutil.TemplateBuilder):

    def construct(self):
        root = xmlutil.TemplateElement('hypervisor', selector='hypervisor')
        root.set('hypervisor_state')
        return xmlutil.SlaveTemplate(root, 1)


class HypervisorsStateTemplate(xmlutil.TemplateBuilder):

    def construct(self):
        root = xmlutil.TemplateElement('hypervisors')
        elem = xmlutil.SubTemplateElement(
            root, 'hypervisor', selector='hypervisors')
        elem.set('hypervisor_state')
        return xmlutil.SlaveTemplate(root, 1)


class HypervisorStateControllerExtension(wsgi.Controller):

    def __init__(self):
        self.host_api = compute.HostAPI()
        self.latest_kvm_version = None

    @wsgi.extends
    def show(self, req, resp_obj, id):
        self._log("Enter: show")
        resp_obj.attach(xml=HypervisorStateTemplate())
        # Retrieve the Hypervisor Instance specified from the DB and populate
        # the State
        context = req.environ['nova.context']
        dbHypInst = None
        try:
            #dbHypInst = db.compute_node_get(req.environ['nova.context'],
            #int(id))
            dbHypInst = self.host_api.compute_node_get(context, int(id))
        except Exception as e:
            self._log("ERROR: show(self, req, resp_obj, id) caught an " +
                      "unexpected exception %s retrieving %s" % (e, id))
        respHypInst = resp_obj.obj['hypervisor']
        if(dbHypInst):
            self._populateStatusAttributes(respHypInst, dbHypInst)
            self._populateVersionAttributes(respHypInst, dbHypInst)
        else:
            respHypInst['hypervisor_state'] = 'UNKNOWN'

    @wsgi.extends
    def detail(self, req, resp_obj):
        self._log("Enter: detail")
        resp_obj.attach(xml=HypervisorsStateTemplate())
        # Loop through all the Hypervisors that the main Hypervisor API is
        # returning
        context = req.environ['nova.context']
        dbHypInst = None
        for hypervisor in list(resp_obj.obj['hypervisors']):
            # Retrieve the Hypervisor Instance specified from the DB and
            # populate the State
            try:
                #dbHypInst = db.compute_node_get(req.environ['nova.context'],
                #hypervisor['id'])
                dbHypInst = self.host_api.compute_node_get(context,
                                                           hypervisor['id'])
            except Exception as e:
                self._log("ERROR: detail(self, req, resp_obj) caught an " +
                          "unexpected exception %s retrieving %s" %
                          (_('%s') % e, hypervisor['id']))
            if(dbHypInst):
                self._populateStatusAttributes(hypervisor, dbHypInst)
                self._populateVersionAttributes(hypervisor, dbHypInst)
            else:
                hypervisor['hypervisor_state'] = 'UNKNOWN'

    def _populateStatusAttributes(self, respHypInst, dbHypInst):
        self._log("Enter: _populateStatusAttributes")
        #Set the Hypervisor State on the Response to the value retrieved from
        #the Database
        hypState = self._getHypervisorStatsData(dbHypInst, 'hypervisor_state')
        if(hypState):
            respHypInst['hypervisor_state'] = hypState
        else:
            respHypInst['hypervisor_state'] = 'UNKNOWN'
            self._log("WARNING: _populateStatusAttributes unable to get " +
                      "hypervisor state from DB")

    def _populateVersionAttributes(self, respHypInst, dbHypInst):
        """Helper method to populate the KVM Version attributes"""
        mgmt_version = None
        hypervior_type = dbHypInst.get('hypervisor_type')
        # We only want to add the Version attributes if it is KVM
        if not hypervior_type or hypervior_type.lower() != 'qemu':
            return
        # If there are any stats available, parse the Management Version
        if dbHypInst.get('stats'):
            statsEntries = jsonutils.loads(dbHypInst.get('stats'))
            mgmt_version = statsEntries.get('mgmt_version')
        # Add the latest PowerVC version available for a KVM Host
        respHypInst['latest_mgmt_version'] = self._get_latest_mgmt_version()
        respHypInst['current_mgmt_version'] = mgmt_version

    def _getHypervisorStatsData(self, dbHypInst, stats_key):
        """
        get the 'stats' data value from the hypervisor DB instance based on the
        specified stats_key
        """
        hypStatsData = None
        statsEntries = dbHypInst.get('stats')
        if(statsEntries is None):
            self._log("WARNING _getHypervisorStatsData unable to get stats " +
                      "entries from hypervisor DB")
            return hypStatsData

        # Since the Stats are now a JSON string, parse into Dictionary format
        statsEntries = jsonutils.loads(statsEntries)
        hypStatsData = statsEntries.get(stats_key)
        if(hypStatsData):
            self._log("HypervisorStats key:" + str(stats_key) + "=" +
                      str(hypStatsData))
        else:
            self._log("_getHypervisorStatsData unable to get " +
                      "HypervisorStats for key:" + str(stats_key))
            for key, val in statsEntries.iteritems():
                entry = dict(key=key, value=val)
                self._log("_getHypervisorStatsData stats entry=" + str(entry))

        return hypStatsData

    def _get_latest_mgmt_version(self):
        """Helper method to get the latest available PowerVC version for KVM"""
        # Only retrieve the version once for the process and cache it
        if self.latest_kvm_version is None:
            latest_version = None
            # Loop through the possible KVM images, determining the latest one
            for filename in listdir('/opt/ibm/powervc/images/kvm'):
                if ((filename.startswith('powervc-powerkvm-compute-') or
                     filename.startswith('powervc-x86kvm-compute-'))):
                    if filename.endswith('.tgz') is False:
                        continue
                    # Parse the version out of the name of the file
                    version = filename[filename.rfind('-') + 1:len(filename)-4]
                    # If this version is greater, use this one instead
                    if self._compare_version(version, latest_version) > 0:
                        latest_version = version
            self.latest_kvm_version = latest_version
        return self.latest_kvm_version

    @staticmethod
    def _compare_version(new_version, old_version):
        """Utility to compare 2 versions and see if the first is newer"""
        # If the old version isn't specified, the new version is greater if set
        if not old_version:
            return 1 if new_version else 0
        # If no new version is provided, the old is newer since it must be set
        if not new_version:
            return -1
        # We need to break apart the version to single digits for comparison
        old_split = old_version.split('.')
        new_split = new_version.split('.')
        max_digits = max(len(new_split), len(old_split))
        # Loop through each of the digits and do the comparison
        for index in range(max_digits):
            old_num = int(old_split[index]) if len(old_split) > index else 0
            new_num = int(new_split[index]) if len(new_split) > index else 0
            # If a digit is different, then return since 1 since greater
            if old_num != new_num:
                return 1 if new_num > old_num else -1
        # If all digits were the same, the 2 versions must be equal
        return 0

    def _log(self, log_str):
        log_msg = "powerVC-HypervisorState-LOG: '%s'" % log_str
        if(log_str.startswith('ERROR')):
            LOG.error(log_msg)
        elif(log_str.startswith('WARNING')):
            LOG.warning(log_msg)
        else:
            LOG.debug(log_msg)


class Hypervisor_state(extensions.ExtensionDescriptor):
    """Extended State/Status Information about the Hypervisor."""
    name = "PowerVC Hypervisor State"
    alias = "powervc-hypervisors-state"
    namespace = "http://www.ibm.com/openstack/compute/contrib/powervc/v1.0"
    updated = "2013-02-12T21:00:00-06:00"

    def get_controller_extensions(self):
        """Provide a Controller Extension to the os-hypervisors Resource"""
        extension_list = []
        extension_list.append(
            extensions.ControllerExtension(
                self,
                'os-hypervisors',
                HypervisorStateControllerExtension()
            )
        )
        return extension_list
