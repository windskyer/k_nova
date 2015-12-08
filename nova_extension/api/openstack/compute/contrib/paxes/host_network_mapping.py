#
#
# =================================================================
# =================================================================

import httplib
import json
from webob import Response
from nova import exception as novaexception
from nova import network as novanetwork
from nova.api.openstack import extensions
from nova.api.openstack import wsgi
from nova.openstack.common import log as logging
from paxes_nova.virt.ibmpowervm.vif.common import ras
from paxes_nova.network.ibmpowervm import adapter_mapping


authorize_show = extensions.extension_authorizer(
    'compute', 'host_network_mapping:show')
authorize_index = extensions.extension_authorizer(
    'compute', 'host_network_mapping:index')
authorize_create = extensions.extension_authorizer(
    'compute', 'host_network_mapping:create')
authorize_update = extensions.extension_authorizer(
    'compute', 'host_network_mapping:update')


LOG = logging.getLogger(__name__)



class HostNetworkMappingController(wsgi.Controller):

    """
    This Controller handles GET, POST and PUT requests to query the database
    and return JSON data to map hosts to networks.
    This api returns or expects json data in the following format
     {
    "host-network-mapping": [
        {
            "host_name": "host1",
            "networks": [
                {
                    "network_id": "network1",
                    "primary_sea": {
                        "lpar_id": 1,
                        "name": "ent10"
                    }
                }
            ]
        },
        {
            "host_name": "host2",
            "networks": [
                {
                    "network_id": "network2",
                    "primary_sea": "not-applicable"
                }
            ]
        }
        }
    """

    def __init__(self):
        self.adapter_mapping_task = adapter_mapping.AdapterMappingTaskClass()

    @wsgi.action("create")
    def create(self, req, body):
        """
        This method handles POST request of the REST to create
        host network mappings , if force is true and any conflicts
        exists, conflicts will be ignored and update happens,
        otherwise user will be given a list of conflicts
        :param req: HTTP Request
        :param body: Body
        :returns: HTTP Response with status code
                  202 - Accpted
                  409 -Conflicts , with conflict message list
                  404 - Not Found , with host or network not found
        """

        context = self._validate_authorization(
            req.environ['nova.context'], authorize_method=authorize_create)
        host_network_mappings_dict = body['host-network-mapping']

        # Check for the force parameter on the URL
        force = False
        if 'force' in req.GET and req.GET['force'] == 'true':
            force = True

        return self._dom_task_put(context,
                                  host_network_mappings_dict,
                                  force,
                                  req)

    @wsgi.extends
    def index(self, req):
        """
        This method handles GET request of the REST to fetch
        host network mappings , if network_id is passed as a
        part of request then fetches for the given network_id
        otherwise fectches all
        :param req: HTTP Request
        :returns: HTTP Response with host network mappings
        """
        context = self._validate_authorization(
            req.environ['nova.context'], authorize_method=authorize_index)

        # Parse the network_id from the URI, if present
        network_id = None
        if 'network_id' in req.GET:
            network_id = req.GET['network_id']

        # Get the dictionary of network mapping
        return self._dom_task_query(req, context, None, network_id)

    @wsgi.extends
    def show(self, req, id):
        """
        This method handles GET request of the REST to fetch
        host network mappings of the given host , if network_id
        is also passed as a part of request then fetches for the
        given network_id and host
        otherwise fectches all
        :param req: HTTP Request
        :param host_id: host
        :returns: HTTP Response with host network mappings
        """
        context = self._validate_authorization(req.environ['nova.context'],
                                               authorize_method=authorize_show)

        # Parse the network_id from the URI, if present
        network_id = None
        if 'network_id' in req.GET:
            network_id = req.GET['network_id']

        # Get the dictionary of network mapping
        return self._dom_task_query(req, context, id, network_id)

    @wsgi.action("update")
    def update(self, req, body):
        """
        This method handles PUT request of the REST to update
        host network mappings , if force is true and any conflicts
        exists, conflicts will be ignored and update happens,
        otherwise user will be given a list of conflicts
        :param req: HTTP Request
        :param body: Body
        :returns: HTTP Response with status code
                  202 - Accepted
                  409 - Conflicts , with conflict message list
                  404 - Not Found , with host or network not found
        """
        context = self._validate_authorization(
            req.environ['nova.context'], authorize_method=authorize_update)
        # Check for the force parameter on the URL
        force = False
        if 'force' in req.GET and req.GET['force'] == 'true':
            force = True

        host_network_mappings_dict = body['host-network-mapping']

        return self._dom_task_put(context,
                                  host_network_mappings_dict,
                                  force,
                                  req)

    @staticmethod
    def _validate_authorization(context, authorize_method):
        """
        This method authorizes the request
        :param context: context
        :returns: authorized context
        """

        authorize_method(context)
        return context.elevated()

    def _format_error_messages(self, exceptions_list):
        """
        This method formats given exception_list into
        error message dict
        :param exception_list: exceptions list
        :returns: message dict
        """
        message_dict = {"messages": []}
        message_list = message_dict.get("messages")
        for exception in exceptions_list:
            if isinstance(exception, novaexception.ComputeHostNotFound):
                status = exception.code
            else:
                status = exception.status_code
            message_list.append(unicode(exception.message))
        return status, message_dict

    def _dom_task_query(self, req, context, host=None, neutron_network=None):
        """
        This method interfaces with dom task and returns
        network mappings as dict
        :param req: HTTP request object
        :param context: context
        :param host: host
        :param neutron_network: neutron_network
        :returns: HTTP response object
        """
        # Validate that host exists
        if host:
            hosts = self.adapter_mapping_task.find_all_host_names(context)
            if not host in hosts:
                raise novaexception.ComputeHostNotFound(host=host)

        # Validate that network exists
        if neutron_network:
            try:
                novanetwork.API().get(context, neutron_network)
            except:
                return Response(request=req,
                                status=httplib.NOT_FOUND,
                                content_type='application/json',
                                body='')

        host_network_mapping_dict = \
            self.adapter_mapping_task.network_association_find(
                context, neutron_network, host)

        # Return results in JSON body response
        return Response(request=req,
                        status=httplib.OK,
                        content_type='application/json',
                        body=json.dumps(host_network_mapping_dict))

    def _dom_task_put(self, context, host_network_mappings_dict, force, req):
        """
        This method interfaces with dom task and create/update given
        host network mappings
        :param context: context
        :param host_network_mappings_dict: host_network_mappings_dict
        :param force: force
        :param req: Http Request
        :returns: HTTP Response with the evaluated status
        """

        exception_list = self.adapter_mapping_task.network_association_put(
            context, host_network_mappings_dict, force)
        self.status = None
        if len(exception_list) == 0:
            # Return HTTP OK
            return Response(request=req,
                            status=httplib.OK,
                            content_type='application/json')
        else:
            self.status, messages = self._format_error_messages(
                exception_list)
            return Response(request=req,
                            status=self.status,
                            content_type='application/json',
                            body=json.dumps(messages))


class Host_network_mapping(extensions.ExtensionDescriptor):

    '''
    Capability to list Network Mapping on all hosts.
    '''
    name = "Compute Host Network Mapping"
    alias = "host-network-mapping"
    namespace = "http://docs.openstack.org/compute/ext/ibm_network_mapping/api\
                 /v1.1"
    updated = "2013-05-30T11:00:00-06:00"

    def get_resources(self):
        '''
        Provide a Resource Extension for host-network-mapping
        '''
        return [extensions.ResourceExtension('host-network-mapping',
                controller=HostNetworkMappingController(),
                collection_actions={'update': 'PUT'})]
