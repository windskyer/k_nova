#
#
# =================================================================
# =================================================================

import httplib
import json
import webob
from webob import Response
from nova import db
from nova import exception as novaexception
from nova import network as novanetwork
from nova.api.openstack import extensions
from nova.api.openstack import wsgi
from nova.openstack.common import log as logging
from oslo.config import cfg
from paxes_nova import network as paxes_nova_network
from paxes_nova.virt.ibmpowervm.vif.common import ras

auth_show = extensions.extension_authorizer('compute',
                                            'host_network_placement:show')
auth_index = extensions.extension_authorizer('compute',
                                             'host_network_placement:index')

LOG = logging.getLogger(__name__)
CONF = cfg.CONF


class HostNetworkPlacementController(wsgi.Controller):
    """
    This Controller handles GET requests to query the database and return JSON
    data to map hosts to networks for use by deploy/placement.
    """

    def __init__(self):
        self.placement_module = paxes_nova_network.placement_module()

    @wsgi.extends
    def index(self, req):
        """
        This method handles GET requests to fetch placement data.
        :param req: HTTP Request
        :returns: HTTP Response with host network placement data.
        """
        context = self._validate_authorization(req.environ['nova.context'],
                                               authorize_method=auth_index)

        # Parse the network_id from the URI, if present
        network_id = None
        if 'network_id' in req.GET:
            network_id = req.GET['network_id']

        # Parse the list_only param from the URI, if present
        list_only = None
        if 'return_only_id_list' in req.GET:
            list_only = req.GET['return_only_id_list'].lower()

        # If a network was provided, validate that the network exists
        if network_id or network_id == '':
            try:
                novanetwork.API().get(context, network_id)
            except:
                return Response(request=req,
                                status=httplib.NOT_FOUND,
                                content_type='application/json',
                                body='')

        if list_only == 'true' and not network_id:
            msg = ras.vif_get_msg('error', 'PLACEMENT_PARAM_MISSING')
            return Response(request=req,
                            status=httplib.BAD_REQUEST,
                            content_type='application/json',
                            body=json.dumps({'message': msg}))

        # Get the placement dictionary
        placement_dict = self.placement_module.get_placement(context,
                                                             None,
                                                             network_id,
                                                             list_only)

        # Return the results
        return webob.Response(request=req,
                              status=httplib.OK,
                              content_type='application/json',
                              body=json.dumps(placement_dict))

    @wsgi.extends
    def show(self, req, id):
        """
        This method handles GET requests to fetch placement data for a specific
        host.
        :param req: HTTP Request
        :param id: host
        :returns: HTTP Response with placement data.
        """
        context = self._validate_authorization(req.environ['nova.context'],
                                               authorize_method=auth_show)

        # Validate the specified host exists
        hosts = self._find_all_host_names(context)
        if not id in hosts:
            raise novaexception.ComputeHostNotFound(host=id)

        # Parse the network_id from the URI, if present
        network_id = None
        if 'network_id' in req.GET:
            network_id = req.GET['network_id']

        # If a network was provided, throw an error
        if network_id:
            msg = ras.vif_get_msg('error', 'PLACEMENT_PARAM_ERROR_BOTH')
            return Response(request=req,
                            status=httplib.BAD_REQUEST,
                            content_type='application/json',
                            body=json.dumps({'message': msg}))

        # Parse the list_only param from the URI, if present
        list_only = None
        if 'return_only_id_list' in req.GET:
            list_only = req.GET['return_only_id_list'].lower()

        # Get the placement dictionary
        placement_dict = self.placement_module.get_placement(context,
                                                             id,
                                                             None,
                                                             list_only)

        # Return the results
        return webob.Response(request=req,
                              status=httplib.OK,
                              content_type='application/json',
                              body=json.dumps(placement_dict))

    @staticmethod
    def _validate_authorization(context, authorize_method):
        """
        This method authorizes the request against the service's policy.
        :param context: API request context.
        :param authorize_method: Which method from this class to authorize.
        :returns: An authorized request context.
        """
        authorize_method(context)
        return context.elevated()

    def _find_all_host_names(self, context):
        """
        Finds a list of compute node names.
        :param context: DB context object.
        :returns: A list of compute node names.
        """
        compute_nodes = db.compute_node_get_all(context)
        return_nodes = []
        for compute in compute_nodes:
            if not compute['service']:
                LOG.warn("No service for compute ID %s" % compute['id'])
                continue
            return_nodes.append(compute['service']['host'])
        return return_nodes


class Host_network_placement(extensions.ExtensionDescriptor):
    """
    Capability to list Network Placement on all hosts.
    """
    name = "Compute Host Network Placement"
    alias = "host-network-placement"
    namespace = "http://docs.openstack.org/compute/ext/ibm_network_placement\
                 /api/v1.1"
    updated = "2013-12-09T11:00:00-06:00"

    def get_resources(self):
        """
        Provide a Resource Extension for host-network-mapping
        """
        return [extensions.ResourceExtension('host-network-placement',
                controller=HostNetworkPlacementController())]
