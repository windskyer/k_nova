#
#
# =================================================================
# =================================================================

import httplib
import json
import webob
from nova.api.openstack import extensions
from nova.api.openstack import wsgi
from oslo.config import cfg
from paxes_nova.network.ibmpowervm import host_seas_query
from paxes_nova.virt.ibmpowervm.vif.common import ras

AUTHORIZE = extensions.extension_authorizer('compute', 'host_seas')
CONF = cfg.CONF


class HostSeasController(wsgi.Controller):
    """
    This Controller handles GET requests to query the database and return
    json data to map hosts to SEAs, also setting a default flag on the SEA
    which contains the vlanid optionally passed in.  If there is no vlanid
    passed in, then the SEA with the lowest pvid will have it's default flag
    set to True.
    """

    def __init__(self):
        super(HostSeasController, self).__init__()
        self.task = host_seas_query.HostSeasQuery()

    @wsgi.extends
    def index(self, req):
        """
        Rest GET method to return SEAs for all hosts.

        :param req: WSGI request object
        """
        context = self._validate_authorization(req.environ['nova.context'])

        # Parse the vlanid from the URI, if present
        vlanid = None
        if 'vlanid' in req.GET:
            vlanid = req.GET['vlanid']

        network_id = None
        if 'network_id' in req.GET:
            network_id = req.GET['network_id']

        # Get the dictionary of all SEAs
        host_seas_dict = self.task.get_host_seas(context, None, None, vlanid,
                                                 network_id)

        # Return the results!
        return webob.Response(request=req,
                              status=httplib.OK,
                              content_type='application/json',
                              body=json.dumps(host_seas_dict))

    @wsgi.extends
    def show(self, req, id):
        """
        Rest GET method to return SEAs for one host.

        :param req: WSGI request object
        :param id: Specific resource (ie, host) to GET info on.
        """
        context = self._validate_authorization(req.environ['nova.context'])

        # Parse the vlanid from the URI, if present
        vlanid = None
        if 'vlanid' in req.GET:
            vlanid = req.GET['vlanid']

        network_id = None
        if 'network_id' in req.GET:
            network_id = req.GET['network_id']
        # Get the dictionary of hosts and SEAs
        host_seas_dict = self.task.get_host_seas(context, id, None, vlanid,
                                                 network_id)

        # Return the results!
        return webob.Response(request=req,
                              status=httplib.OK,
                              content_type='application/json',
                              body=json.dumps(host_seas_dict))

    @staticmethod
    def _validate_authorization(context):
        """Internal Helper Method to Confirm the Requester is Authorized"""
        AUTHORIZE(context)
        return context.elevated()


class Host_seas(extensions.ExtensionDescriptor):
    """Capability to list SEAs on all hosts."""
    name = "Compute Host SEAs"
    alias = "host-seas"
    namespace = "http://docs.openstack.org/compute/ext/ibm_host_seas/api/v1.1"
    updated = "2013-05-16T21:00:00-06:00"

    def get_resources(self):
        """Provide a Resource Extension for host-seas"""
        return [extensions.ResourceExtension('host-seas',
                                             HostSeasController())]
