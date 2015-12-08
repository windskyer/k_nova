
import webob

from nova.api.openstack import extensions
from nova.api.openstack import wsgi
from paxes_nova import compute
from nova import exception
from nova.openstack.common.gettextutils import _


authorize = extensions.extension_authorizer('compute', 'sshconsoles')


class SSHConsolesController(wsgi.Controller):
    def __init__(self, *args, **kwargs):
        self.compute_api = compute.API()
        super(SSHConsolesController, self).__init__(*args, **kwargs)

    @wsgi.action('os-getSSHConsole')
    def get_ssh_console(self, req, id, body):
        """Get ssh connection information to access a server."""
        context = req.environ['nova.context']
        authorize(context)

        # If type is not supplied or unknown, get_vnc_console below will cope
        console_type = body['os-getSSHConsole'].get('type')

        try:
            instance = self.compute_api.get(context, id, want_objects=True)
#             output = self.compute_api.get_vnc_console(context,
#                                                       instance,
#                                                       console_type)
            output = self.compute_api.get_ssh_console(context,
                                                      instance,
                                                      console_type)
        except exception.InstanceNotFound as e:
            raise webob.exc.HTTPNotFound(explanation=e.format_message())
        except exception.InstanceNotReady as e:
            raise webob.exc.HTTPConflict(
                    explanation=_('Instance not yet ready'))
        except NotImplementedError:
            msg = _("Unable to get vnc console, functionality not implemented")
            raise webob.exc.HTTPNotImplemented(explanation=msg)

        return {'console': {'type': console_type, 'url': output['url']}}


class Ssh_consoles(extensions.ExtensionDescriptor):
    """Interactive Console support."""
    name = "Ssh_consoles"
    alias = "os-sshconsoles"
    namespace = "http://docs.openstack.org/compute/ext/os-sshconsoles/api/v2"
    updated = "2011-12-23T00:00:00+00:00"

    def get_controller_extensions(self):
        controller = SSHConsolesController()
        extension = extensions.ControllerExtension(self, 'servers', controller)
        return [extension]
