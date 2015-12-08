

'''
Extend server create and resize action with resource allocation.
'''
import hashlib

from webob import exc

from nova.api.openstack import extensions
from nova.api.openstack import wsgi
from nova import compute
from nova.compute import flavors
from nova import db
from nova import exception
from nova.openstack.common import log as logging

from nova.openstack.common.gettextutils import _


LOG = logging.getLogger(__name__)
authorize = extensions.extension_authorizer('compute', 'flavor_dynamic')


class Flavor_dynamic(extensions.ExtensionDescriptor):
    """Support to create a dynamic flavor."""

    name = "FlavorDynamic"
    alias = "ibm-flavor-dynamic"
    namespace = "http://docs.openstack.org/compute/ext/" \
                "flavor_dynamic/api/v1.1"
    updated = "2013-01-13T00:00:00+00:00"

    def get_controller_extensions(self):
        controller = FlavorDynamicController()
        extension = extensions.ControllerExtension(self,
                                    'servers', controller)
        return [extension]


class FlavorDynamicController(wsgi.Controller):
    """Dynamic flavor API controller."""

    def __init__(self, **kwargs):
        super(FlavorDynamicController, self).__init__(**kwargs)
        self.compute_api = compute.API()

    def _get_server(self, context, req, instance_uuid):
        """Utility function for looking up an instance by uuid."""
        try:
            instance = self.compute_api.get(context, instance_uuid)
        except exception.NotFound:
            msg = _("Instance could not be found")
            raise exc.HTTPNotFound(explanation=msg)
        req.cache_db_instance(instance)
        return instance

    def _generate_unique_name(self, flavor_dict):
        memory_mb = flavor_dict.get('ram')
        vcpus = flavor_dict.get('vcpus')
        root_gb = flavor_dict.get('disk')
        ephemeral_gb = flavor_dict.get('OS-FLV-EXT-DATA:ephemeral')

        text_name = ("auto-flavor-%(vcpus)s-%(memory_mb)s-"
                     "%(root_gb)s-%(ephemeral_gb)s" %
                     {'vcpus': vcpus, 'memory_mb': memory_mb,
                      'root_gb': root_gb, 'ephemeral_gb': ephemeral_gb})

        extra_specs_dict = flavor_dict.get('extra_specs')

        if extra_specs_dict:
            sorted_keys = sorted(extra_specs_dict.keys())
            for key in sorted_keys:
                text_name = '-'.join([text_name, key,
                                      str(extra_specs_dict[key])])

        return hashlib.md5(text_name).hexdigest()

    def _add_extra_specs(self, context, flavor_dict, inst_type):
        extra_specs_dict = flavor_dict.get('extra_specs')
        inst_type_dict = dict(inst_type)
        if extra_specs_dict:
            try:
                db.flavor_extra_specs_update_or_create(
                                                    context,
                                                    inst_type_dict['flavorid'],
                                                    extra_specs_dict)
            except exception.MetadataLimitExceeded as error:
                raise exc.HTTPBadRequest(explanation=unicode(error))

    @wsgi.extends
    def create(self, req, body):
        """Extend Server create action with resource allocation.
        """
        if not self.is_valid_body(body, 'server'):
            raise exc.HTTPUnprocessableEntity()
        context = req.environ['nova.context']

        server_dict = body['server']
        flavor_dict = server_dict.get('flavor')

        if flavor_dict:
            # only authorize if they have the flavor_dict otherwise the policy
            # is always applied whether it is present or not
            authorize(context)

            # verify input parameters
            if server_dict.get('flavorRef'):
                msg = _("only one of flavorRef or flavor can be specified.")
                raise exc.HTTPBadRequest(explanation=msg)
            for opt in ['ram', 'vcpus', 'disk']:
                try:
                    val = int(flavor_dict.get(opt))
                    if opt == 'disk':
                        assert val >= 0
                    else:
                        assert val > 0
                except (ValueError, TypeError, AssertionError):
                    if opt == 'disk':
                        msg = _("%s argument must be an integer") % opt
                    else:
                        msg = _("%s argument must be a positive integer") % opt
                    raise exc.HTTPBadRequest(explanation=msg)

            memory_mb = flavor_dict.get('ram')
            vcpus = flavor_dict.get('vcpus')
            root_gb = flavor_dict.get('disk')
            ephemeral_gb = flavor_dict.get('OS-FLV-EXT-DATA:ephemeral')

            # query the flavor
            flavor_name = self._generate_unique_name(flavor_dict)
            inst_type = None
            try:
                inst_type = flavors. \
                    get_flavor_by_name(flavor_name, context)
            except exception.FlavorNotFoundByName:
                LOG.debug("Flavor not found. Creating...")

            # create flavor if no matched flavor
            if not inst_type:
                if ephemeral_gb is None:
                    ephemeral_gb = 0
                try:
                    inst_type = flavors.create(flavor_name,
                                          memory_mb,
                                          vcpus,
                                          root_gb,
                                          ephemeral_gb=ephemeral_gb,
                                          flavorid=None,
                                          swap=0,
                                          rxtx_factor=1.0,
                                          is_public=False)
                    admin_context = (context.is_admin and
                                    context or context.elevated())
                    flavors.add_flavor_access(inst_type['flavorid'],
                                admin_context.project_id, admin_context)
                    self._add_extra_specs(context, flavor_dict, inst_type)
                    req.cache_db_flavor(inst_type)
                except (exception.FlavorExists,
                        exception.FlavorIdExists):
                    try:
                        inst_type = flavors. \
                            get_flavor_by_name(flavor_name, context)
                    except exception.FlavorNotFoundByName:
                        raise exception.FlavorCreateFailed
                except exception.FlavorCreateFailed:
                    msg = _('An unknown db error has occurred. ')
                    raise exc.HTTPInternalServerError(explanation=msg)

            # update flavorRef parameters
            server_dict['flavorRef'] = inst_type['flavorid']
            del server_dict['flavor']
        elif not server_dict.get('flavorRef'):
            msg = _("Missing flavorRef or flavor attribute.")
            raise exc.HTTPBadRequest(explanation=msg)
        yield

    @wsgi.extends(action='resize')
    def _action_resize(self, req, id, body):
        """Extend Server resize action with resource allocation.
        """

        if not self.is_valid_body(body, 'resize'):
            raise exc.HTTPUnprocessableEntity()
        context = req.environ['nova.context']

        resize_server_dict = body['resize']
        flavor_dict = resize_server_dict.get('flavor')

        if flavor_dict:
            # only authorize if they have the flavor_dict otherwise the policy
            # is always applied whether it is present or not
            authorize(context)

            # verify input parameters
            if resize_server_dict.get('flavorRef'):
                msg = _("only one of flavorRef or flavor can be specified.")
                raise exc.HTTPBadRequest(explanation=msg)
            memory_mb = flavor_dict.get('ram')
            vcpus = flavor_dict.get('vcpus')
            root_gb = flavor_dict.get('disk')
            ephemeral_gb = flavor_dict.get('OS-FLV-EXT-DATA:ephemeral')

            if memory_mb is None:
                msg = _("The ram parameter in flavor{} must be specified")
                raise exc.HTTPBadRequest(explanation=msg)
            if vcpus is None:
                msg = _("The vcpus parameter in flavor{} must be specified")
                raise exc.HTTPBadRequest(explanation=msg)
            if root_gb is None:
                msg = _("The disk parameter in flavor{} must be specified")
                raise exc.HTTPBadRequest(explanation=msg)

            for opt in ['ram', 'vcpus', 'disk']:
                try:
                    val = int(flavor_dict.get(opt))
                    if opt == 'disk':
                        assert val >= 0
                    else:
                        assert val > 0
                except (ValueError, TypeError, AssertionError):
                    if opt == 'disk':
                        msg = _("%s argument must be an integer") % opt
                    else:
                        msg = _("%s argument must be a positive integer") % opt
                    raise exc.HTTPBadRequest(explanation=msg)

            server = self._get_server(context, req, id)
            if not server:
                msg = _("Can not get target server by server id %s") % id
                raise exc.HTTPBadRequest(explanation=msg)
            instance_type = server.get('instance_type', None)

            if instance_type:
                currentVcpus = int(instance_type['vcpus'])
                currentRam = int(instance_type['memory_mb'])
                currentDisk = int(instance_type['root_gb'])
                if int(vcpus) == currentVcpus:
                    if int(memory_mb) == currentRam:
                        if int(root_gb) == currentDisk:
                            msg = _("ram, vcpus, or disk allocation in %s has"
                             " not changed.") % instance_type['name']
                            raise exc.HTTPBadRequest(explanation=msg)
            # query the flavor
            flavor_name = self._generate_unique_name(flavor_dict)
            inst_type = None
            try:
                inst_type = flavors. \
                    get_flavor_by_name(flavor_name, context)
            except exception.FlavorNotFoundByName:
                LOG.debug("Flavor not found. Creating...")

            # create flavor if no matched flavor
            if not inst_type:
                if ephemeral_gb is None:
                    ephemeral_gb = 0
                try:
                    inst_type = flavors.create(flavor_name,
                                          memory_mb,
                                          vcpus,
                                          root_gb,
                                          ephemeral_gb=ephemeral_gb,
                                          flavorid=None,
                                          swap=0,
                                          rxtx_factor=1.0,
                                          is_public=False)
                    admin_context = (context.is_admin and
                                    context or context.elevated())
                    flavors.add_flavor_access(inst_type['flavorid'],
                                admin_context.project_id, admin_context)
                    self._add_extra_specs(context, flavor_dict, inst_type)
                    req.cache_db_flavor(inst_type)
                except (exception.FlavorExists,
                        exception.FlavorIdExists):
                    try:
                        inst_type = flavors. \
                            get_flavor_by_name(flavor_name, context)
                    except exception.FlavorNotFoundByName:
                        raise exception.FlavorCreateFailed
                except exception.FlavorCreateFailed:
                    msg = _('An unknown db error has occurred. ')
                    raise exc.HTTPInternalServerError(explanation=msg)

            # update flavorRef parameters
            resize_server_dict['flavorRef'] = inst_type['flavorid']
            del resize_server_dict['flavor']
        elif not resize_server_dict.get('flavorRef'):
            msg = _("Resize requests require flavorRef or flavor "
                    "attribute.")
            raise exc.HTTPBadRequest(explanation=msg)
        yield
