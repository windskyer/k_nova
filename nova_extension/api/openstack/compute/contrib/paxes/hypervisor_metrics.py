#
#
# =================================================================
# =================================================================

"""An os-hypervisors API extension to report additional hypervisor metrics."""

from nova.api.openstack import extensions
from nova.api.openstack import wsgi
from nova.api.openstack import xmlutil
from nova import compute
from nova.openstack.common import jsonutils
from nova.openstack.common import log as logging
from oslo.config import cfg
#from powervc_health import _


# Load the over-commit ratios for CPU and memory.
CONF = cfg.CONF
CONF.import_opt('cpu_allocation_ratio', 'nova.scheduler.filters.core_filter')
CONF.import_opt('ram_allocation_ratio', 'nova.scheduler.filters.ram_filter')


LOG = logging.getLogger(__name__)
authorize = extensions.extension_authorizer('compute', 'hypervisor_metrics')


"""
 The key is the name of the field in the database.
 In the dictionary for each key:
      display_name is the name of the field to be surfaced through this
                   extension
      converter is a function that takes a string value and converts it to
                the data type for the field
      default_value is the default value to be surfaced for the display_name
                    key for this metric
"""
HYPERVISOR_TYPE_POWERVM = 'powervm'
HYPERVISOR_TYPE_POWERKVM = 'QEMU'
METRICS = {
    # Not intended to be a host-level metric at this time (i.e., use default)
    'cpu_allocation_ratio': {
        'display_name': 'cpu_allocation_ratio',
        'converter': float,
        'default_value': CONF.cpu_allocation_ratio,
        'supported_platforms': [HYPERVISOR_TYPE_POWERKVM]
    },
    # Not intended to be a host-level metric at this time (i.e., use default)
    'memory_allocation_ratio': {
        'display_name': 'memory_allocation_ratio',
        'converter': float,
        'default_value': CONF.ram_allocation_ratio,
        'supported_platforms': [HYPERVISOR_TYPE_POWERKVM]
    },
    'host_memory_reserved': {
        'display_name': 'memory_mb_reserved',
        'converter': int,
        'default_value': None,
        'supported_platforms': [HYPERVISOR_TYPE_POWERVM]
    },
    'memory_mb_used': {
        'display_name': 'memory_mb_used',
        'converter': int,
        'default_value': None,
        'supported_platforms': [HYPERVISOR_TYPE_POWERVM,
                                HYPERVISOR_TYPE_POWERKVM]
    },
    'proc_units_reserved': {
        'display_name': 'proc_units_reserved',
        'converter': None,
        'default_value': None,
        'supported_platforms': [HYPERVISOR_TYPE_POWERVM]
    },
    'proc_units_used': {
        'display_name': 'proc_units_used',
        'converter': None,
        'default_value': None,
        'supported_platforms': [HYPERVISOR_TYPE_POWERVM]
    },
    'proc_units': {
        'display_name': 'proc_units',
        'converter': None,
        'default_value': None,
        'supported_platforms': [HYPERVISOR_TYPE_POWERVM]
    },
    'lmb_size': {
        'display_name': 'lmb_size',
        'converter': int,
        'default_value': None,
        'supported_platforms': [HYPERVISOR_TYPE_POWERVM]
    },
    'active_lpar_mobility_capable': {
        'display_name': 'active_lpar_mobility_capable',
        'converter': int,
        'default_value': None,
        'supported_platforms': [HYPERVISOR_TYPE_POWERVM]
    },
    'compatibility_modes': {
        'display_name': 'compatibility_modes',
        'converter': None,
        'default_value': [],
        'supported_platforms': [HYPERVISOR_TYPE_POWERVM]
    },
    'active_migrations_supported': {
        'display_name': 'active_migrations_supported',
        'converter': int,
        'default_value': None,
        'supported_platforms': [HYPERVISOR_TYPE_POWERVM]
    },
    'active_migrations_in_progress': {
        'display_name': 'active_migrations_in_progress',
        'converter': int,
        'default_value': None,
        'supported_platforms': [HYPERVISOR_TYPE_POWERVM]
    },
    'inactive_migrations_supported': {
        'display_name': 'inactive_migrations_supported',
        'converter': int,
        'default_value': None,
        'supported_platforms': [HYPERVISOR_TYPE_POWERVM]
    },
    'inactive_migrations_in_progress': {
        'display_name': 'inactive_migrations_in_progress',
        'converter': int,
        'default_value': None,
        'supported_platforms': [HYPERVISOR_TYPE_POWERVM]
    },
    'max_procs_per_aix_linux_lpar': {
        'display_name': 'max_procs_per_aix_linux_lpar',
        'converter': None,
        'default_value': None,
        'supported_platforms': [HYPERVISOR_TYPE_POWERVM]
    },
    'max_vcpus_per_aix_linux_lpar': {
        'display_name': 'max_vcpus_per_aix_linux_lpar',
        'converter': None,
        'default_value': None,
        'supported_platforms': [HYPERVISOR_TYPE_POWERVM]
    },
    'disk_available': {
        'display_name': 'disk_available',
        'converter': int,
        'default_value': None,
        'supported_platforms': [HYPERVISOR_TYPE_POWERVM]
    },
    'disk_total': {
        'display_name': 'disk_total',
        'converter': int,
        'default_value': None,
        'supported_platforms': [HYPERVISOR_TYPE_POWERVM]
    },
    'disk_used': {
        'display_name': 'disk_used',
        'converter': int,
        'default_value': None,
        'supported_platforms': [HYPERVISOR_TYPE_POWERVM]
    },
    'usable_local_mb': {
        'display_name': 'usable_local_mb',
        'converter': int,
        'default_value': None,
        'supported_platforms': [HYPERVISOR_TYPE_POWERVM]
    },
    'threads_per_core': {
        'display_name': 'threads_per_core',
        'converter': int,
        'default_value': None,
        'supported_platforms': [HYPERVISOR_TYPE_POWERKVM]
    },
    'split_core': {
        'display_name': 'split_core',
        'converter': int,
        'default_value': None,
        'supported_platforms': [HYPERVISOR_TYPE_POWERKVM]
    },
    'max_smt_per_guest': {
        'display_name': 'max_smt_per_guest',
        'converter': int,
        'default_value': None,
        'supported_platforms': [HYPERVISOR_TYPE_POWERKVM]
    },
    'vcpus': {
        'display_name': 'vcpus',
        'converter': int,
        'default_value': None,
        'supported_platforms': [HYPERVISOR_TYPE_POWERVM,
                                HYPERVISOR_TYPE_POWERKVM]
    },
    'memory_mb': {
        'display_name': 'memory_mb',
        'converter': int,
        'default_value': None,
        'supported_platforms': [HYPERVISOR_TYPE_POWERVM,
                                HYPERVISOR_TYPE_POWERKVM]
    },
    'local_gb': {
        'display_name': 'local_gb',
        'converter': int,
        'default_value': None,
        'supported_platforms': [HYPERVISOR_TYPE_POWERKVM]
    },
    'vcpus_used': {
        'display_name': 'vcpus_used',
        'converter': int,
        'default_value': None,
        'supported_platforms': [HYPERVISOR_TYPE_POWERVM,
                                HYPERVISOR_TYPE_POWERKVM]
    },
    'local_gb_used': {
        'display_name': 'local_gb_used',
        'converter': int,
        'default_value': None,
        'supported_platforms': [HYPERVISOR_TYPE_POWERKVM]
    },
    'hypervisor_type': {
        'display_name': 'hypervisor_type',
        'converter': None,
        'default_value': None,
        'supported_platforms': [HYPERVISOR_TYPE_POWERVM,
                                HYPERVISOR_TYPE_POWERKVM]
    },
    'hypervisor_version': {
        'display_name': 'hypervisor_version',
        'converter': float,
        'default_value': None,
        'supported_platforms': [HYPERVISOR_TYPE_POWERVM,
                                HYPERVISOR_TYPE_POWERKVM]
    },
    'hypervisor_hostname': {
        'display_name': 'hypervisor_hostname',
        'converter': None,
        'default_value': None,
        'supported_platforms': [HYPERVISOR_TYPE_POWERVM,
                                HYPERVISOR_TYPE_POWERKVM]
    },
    'cpu_info': {
        'display_name': 'cpu_info',
        'converter': None,
        'default_value': None,
        'supported_platforms': [HYPERVISOR_TYPE_POWERVM,
                                HYPERVISOR_TYPE_POWERKVM]
    },
    'disk_available_least': {
        'display_name': 'disk_available_least',
        'converter': None,
        'default_value': None,
        'supported_platforms': [HYPERVISOR_TYPE_POWERVM,
                                HYPERVISOR_TYPE_POWERKVM]
    },
}


class HypervisorMetricsTemplate(xmlutil.TemplateBuilder):

    def construct(self):
        root = xmlutil.TemplateElement('hypervisor', selector='hypervisor')
        for key in METRICS.keys():
            root.set(METRICS.get(key)['display_name'])
        return xmlutil.SlaveTemplate(root, 1)


class HypervisorsMetricsTemplate(xmlutil.TemplateBuilder):

    def construct(self):
        root = xmlutil.TemplateElement('hypervisors')
        elem = xmlutil.SubTemplateElement(
            root, 'hypervisor', selector='hypervisors')
        for key in METRICS.keys():
            elem.set(METRICS.get(key)['display_name'])
        return xmlutil.SlaveTemplate(root, 1)


class HypervisorMetricsControllerExtension(wsgi.Controller):

    def __init__(self):
        self.host_api = compute.HostAPI()

    @wsgi.extends
    def show(self, req, resp_obj, id):
        self._log("Enter: show")
        resp_obj.attach(xml=HypervisorMetricsTemplate())
        # Retrieve the hypervisor instance specified in the database and
        # populate the state and metrics.
        context = req.environ['nova.context']
        compute_node = None
        try:
            compute_node = self.host_api.compute_node_get(context, int(id))
            self._populate_metric_attrs(resp_obj.obj['hypervisor'],
                                        compute_node)
        except Exception as e:
            self._log("ERROR: show(self, req, resp_obj, id) caught an "
                      "unexpected exception %s retrieving %s" % (str(e), id))

    @wsgi.extends
    def detail(self, req, resp_obj):
        self._log("Enter: detail")
        resp_obj.attach(xml=HypervisorsMetricsTemplate())
        # Loop through the hypervisors that the primary hypervisor API returns.
        context = req.environ['nova.context']
        compute_node = None
        for hypervisor in list(resp_obj.obj['hypervisors']):
            # Retrieve the hypervisor instance specified from the database and
            # populate the metrics.
            try:
                compute_node = self.host_api.compute_node_get(context,
                                                              hypervisor['id'])
                self._populate_metric_attrs(hypervisor, compute_node)
            except Exception as e:
                self._log("ERROR: detail(self, req, resp_obj) caught an "
                          "unexpected exception %s retrieving %s" %
                          (str(e), hypervisor['id']))

    def _populate_metric_attrs(self, hypervisor, compute_node):
        self._log("Enter: _populate_metric_attrs")
        # Set the hypervisor metrics on the response.
        if compute_node:
            # Since the Stats are now a JSON string, parse into a Dictionary
            compute_stats = compute_node.get('stats')
            compute_stats = '{}' if compute_stats is None else compute_stats
            compute_stats = jsonutils.loads(compute_stats)
            #hypervisor_type = compute_stats.get('hypervisor_type',
            #                                    HYPERVISOR_TYPE_POWERVM)
            hypervisor_type = compute_node.get('hypervisor_type',
                                               HYPERVISOR_TYPE_POWERVM)
            LOG.debug("Platform is " + hypervisor_type)
            for key, value in compute_stats.iteritems():
                if key in METRICS.keys():
                    metric = METRICS.get(key)
                    if hypervisor_type in metric['supported_platforms']:
                        # Compatibility modes is a special case.In the database
                        # it can only be a string,so we will convert the comma-
                        # separated string into a list.
                        if key == 'compatibility_modes':
                            value = value.split(',')

                        converter = metric['converter']
                        if converter:
                            hypervisor[metric['display_name']] = \
                                converter(value)
                        else:
                            hypervisor[metric['display_name']] = value

            for item in METRICS.items():
                metric = item[1]
                if hypervisor_type in metric['supported_platforms']:
                    metric_display_name = metric['display_name']
                    if metric_display_name not in hypervisor:
                        compute_node_id = compute_node['id']
                        self._log("_populate_metric_attrs database "
                                  "for %s does not contain %s" %
                                  (compute_node_id, metric_display_name))
                        hypervisor[metric_display_name] = \
                            metric['default_value']
        else:
            for item in METRICS.items():
                metric = item[1]
                if hypervisor_type in metric['supported_platforms']:
                    metric_display_name = metric['display_name']
                    if metric_display_name not in hypervisor:
                        hypervisor[metric_display_name] = \
                            metric['default_value']

    def _log(self, log_str):
        pass
        # log_msg = _("powerVC-Hypervisor-Metric-LOG: '%s'") % log_str
        # if log_str.startswith('ERROR'):
        #    LOG.error(log_msg)
        # elif log_str.startswith('WARNING'):
        #    LOG.warning(log_msg)
        # else:
        #    LOG.debug(log_msg)


class Hypervisor_metrics(extensions.ExtensionDescriptor):
    """Extended Metric Information about the Hypervisor."""
    name = "PowerVC Hypervisor Metrics"
    alias = "powervc-hypervisors-metrics"
    namespace = "http://www.ibm.com/openstack/compute/contrib/powervc/v1.0"
    updated = "2013-03-13T21:00:00-06:00"

    def get_controller_extensions(self):
        """Provide a Controller Extension to the os-hypervisors Resource"""
        extension_list = []
        extension_list.append(
            extensions.ControllerExtension(
                self,
                'os-hypervisors',
                HypervisorMetricsControllerExtension()
            )
        )
        return extension_list
