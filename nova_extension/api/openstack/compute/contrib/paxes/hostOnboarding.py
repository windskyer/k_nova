#
#
# =================================================================
# =================================================================

"""An API Extension for being able to On-board VM's for a Compute Host"""

import webob.exc
from xml.dom import minidom

from nova.api.openstack import extensions
from nova.api.openstack import wsgi
from nova.api.openstack import xmlutil
from nova.openstack.common import log as logging

from paxes_nova.db import api as powervc_db
from paxes_nova.objects.compute import dom as compute_dom

from paxes_discovery.onboarding import importer_messages as msg
from paxes_discovery.onboarding.compute import vm_importer
from paxes_discovery.locale import _

LOG = logging.getLogger(__name__)
AUTHORIZE = extensions.extension_authorizer('compute', 'host_registration')


class HostVmListTemplate(xmlutil.TemplateBuilder):
    """Utility Class to Convert a JSON Response to XML for listing VM's"""

    def construct(self):
        """Template Constructor to convert the JSON to an XML Response"""
        attrs = ['id', 'uuid', 'name', 'status', 'managed']
        #For XML there is 2 levels, the VM's first then the VM
        root = xmlutil.TemplateElement('servers')
        elem = xmlutil.SubTemplateElement(root, 'server', selector='servers')
        #For each of the possible attributes, set this as a valid mapping
        for attr in attrs:
            elem.set(attr)
        return xmlutil.MasterTemplate(root, 1)


class HostOnboardDeserializer(wsgi.XMLDeserializer):
    """Utility Class to Convert an XML Request to JSON for Host On-boarding"""

    def default(self, string):
        """Deserializer to convert the XML Request to JSON"""
        vms = []
        #Parse the XML String into a Document Structure for Traversal
        root_node = minidom.parseString(string)
        #For XML we have 2 layers, the VM's tag, then the VM tag
        main_node = self.find_first_child_named(root_node, 'servers')
        vm_nodes = self.find_children_named(main_node, 'server')
        #Loop through each VM (UUID) adding it to the list to return
        for vm_node in vm_nodes:
            vms.append(self.extract_text(vm_node))
        return {'vms': vms}


class HostUnmanageDeserializer(wsgi.XMLDeserializer):
    """Utility Class to Convert an XML Request to JSON for VM unmanage"""

    def default(self, string):
        """Deserializer to convert the XML Request to JSON"""
        vms = []
        #Parse the XML String into a Document Structure for Traversal
        root_node = minidom.parseString(string)
        #For XML we have 2 layers, the VM's tag, then the VM tag
        main_node = self.find_first_child_named(root_node, 'servers')
        vm_nodes = self.find_children_named(main_node, 'server')
        #Loop through each VM (UUID) adding it to the list to return
        for vm_node in vm_nodes:
            vms.append(self.extract_text(vm_node))
        return {'vms': vms}


class HostOnboardControllerExtension(wsgi.Controller):
    """Controller Class to extend the os-hosts API for On-boarding VM's"""

    @wsgi.action("index")
    def index(self, req, host_name_id):
        """Add Rest GET Method to list On-boarding Tasks"""
        tasks = []
        context = req.environ['nova.context']
        context = self._validate_authorization(context, 'show')
        #Retrieve the list of On-boarding Tasks from the Database
        result = powervc_db.onboard_task_get_all(context, host_name_id)
        for task in result:
            task_uri = self._get_task_uri(req, task['id'])
            tasks.append({'id': task['id'], 'links': [{'href': task_uri}]})
        return {'tasks': tasks}

    @wsgi.action("show")
    def show(self, req, host_name_id, id):
        """Add Rest GET Method to show a specified On-boarding Task"""
        context = req.environ['nova.context']
        context = self._validate_authorization(context, 'show')
        #Retrieve the list of On-boarding Tasks from the Database
        result = powervc_db.onboard_task_get(context, int(id))
        #If the Task wasn't found in the Database, then throw an exception
        if result is None or result['host'] != host_name_id:
            text = msg.BaseImporterMessageBundle().\
                get_message_text(context, msg.NO_IMPORT_TASK, taskid=id)
            raise webob.exc.HTTPNotFound(explanation=text)
        #Construct the Task dictionary to return in the REST response
        task = {'id': result['id']}
        task['links'] = [{'href': req.url}]
        task['servers'] = []
        #Loop through all the attributes on the task we want to include
        task_attrs = ['status', 'progress', 'started', 'ended']
        for attr in task_attrs:
            task[attr] = result[attr]
        #Add in the list of VM Results into the overall Task entry
        result_vms = result.get('servers', [])
        for result_vm in result_vms:
            vm = {'id': result_vm['server_uuid']}
            vm['name'] = result_vm['server_name']
            vm['status'] = result_vm['status']
            if result_vm.get('fault_message'):
                vm['fault'] = result_vm['fault_message']
            task['servers'].append(vm)
        return {'task': task}

    @wsgi.action("create")
    @wsgi.deserializers(xml=HostOnboardDeserializer)
    def onboard(self, req, host_name_id, body):
        """Add Rest POST Method to On-board VM's for a given Host"""
        context = req.environ['nova.context']
        context = self._validate_authorization(context, 'create')
        #Parse out the key attributes from the request body
        servers = body.pop('servers', None)
        synch = body.pop('synchronous', False) is True
        unsupported = body.pop('allow_unsupported', False) is True
        #Delegate to the VM Importer to actually perform the on-boarding
        importer = vm_importer.VmImporter(context, host_name_id)
        response = importer.import_resources(servers, body, synch, unsupported)
        #If this is asynchronous, then return the Task Identifier Info
        if not synch:
            #Build the Task URI (to use for progress) to return in the response
            task_uri = self._get_task_uri(req, response)
            return {'task': {'id': response, 'links': [{'href': task_uri}]}}
        #Otherwise we will get the Values back on the Synchronous call
        return {'servers': response}

    @staticmethod
    def _validate_authorization(context, action):
        """Internal Helper Method to Confirm the Requester is Authorized"""
        AUTHORIZE(context, action=action)
        return context.elevated()

    @staticmethod
    def _get_task_uri(req, task_id):
        """Internal Helper Method to format the URI for the On-boarding Task"""
        return '%s/%s' % (req.url, str(task_id))


class HostUnmanageControllerExtension(wsgi.Controller):
    """Controller Class to extend the os-hosts API for Unmanaging VM's"""

    @wsgi.action("create")
    @wsgi.response(204)
    @wsgi.deserializers(xml=HostUnmanageDeserializer)
    def unmanage(self, req, host_name_id, body):
        """Add Rest POST Method to Unmanage VM's for a given Host"""
        context = req.environ['nova.context']
        context = self._validate_authorization(context, 'create')
        force = body.get('force', False)
        #Delegate to the VM Importer to actually perform the unmanage
        importer = vm_importer.VmImporter(context, host_name_id)
        importer.unmanage_resources(body.get('servers'), bool(force))

    @staticmethod
    def _validate_authorization(context, action):
        """Internal Helper Method to Confirm the Requester is Authorized"""
        AUTHORIZE(context, action=action)
        return context.elevated()


class HostVmListControllerExtension(wsgi.Controller):
    """Controller Class to extend the os-hosts API for listing VM's"""

    @wsgi.action("index")
    @wsgi.serializers(xml=HostVmListTemplate)
    def index(self, req, host_name_id):
        """Add Rest GET Method to list existing VM's for a given Host"""
        context = req.environ['nova.context']
        context = self._validate_authorization(context, 'show')
        #Delegate to the VM Importer to actually perform the listing of vm's
        importer = vm_importer.VmImporter(context, host_name_id)
        vms = importer.discover_resources()
        return {'servers': vms}

    @staticmethod
    def _validate_authorization(context, action):
        """Internal Helper Method to Confirm the Requester is Authorized"""
        AUTHORIZE(context, action=action)
        return context.elevated()


class ServerFinishOnboardControllerExtension(wsgi.Controller):
    """Controller Class to extend the os-hosts API for listing VM's"""

    @wsgi.extends
    def show(self, req, resp_obj, id):
        """Rest GET Method to add the Pending On-board Actions attribute to"""
        ctxt = req.environ['nova.context']
        #Add the Pending On-board Actions attribute to the given Server
        self._add_pending_onboard_attribute(ctxt, req, resp_obj.obj['server'])

    @wsgi.extends
    def detail(self, req, resp_obj):
        """Rest GET Method to add the Pending On-board Actions attributes to"""
        context = req.environ['nova.context']
        #Add the Pending On-board Actions attribute to each of the Servers
        for server in resp_obj.obj['servers']:
            self._add_pending_onboard_attribute(context, req, server)

    @wsgi.extends
    def delete(self, req, id):
        """REST DELETE Method to verify that the Server can be deleted"""
        context = req.environ['nova.context']
        self._verify_valid_action(context, str(id), 'delete')
        yield

    @wsgi.extends(action='createImage')
    def capture(self, req, id, body):
        """REST POST Method to verify that the Server can be captured"""
        context = req.environ['nova.context']
        self._verify_valid_action(context, str(id), 'capture')
        yield

    @wsgi.extends(action='resize')
    def resize(self, req, id, body):
        """REST POST Method to verify that the Server can be resized"""
        context = req.environ['nova.context']
        self._verify_valid_action(context, str(id), 'resize')
        yield

    @wsgi.action('finishOnboard')
    @wsgi.response(204)
    def finish_onboard(self, req, id, body):
        """REST POST Method to finish the on-boarding process for the Server"""
        context = req.environ['nova.context']
        context = self._validate_authorization(context, 'create')
        #Parse the on-boarding info out of the request body
        onboard_info = body.get('finishOnboard')
        os_distro = onboard_info.get('os_distro')
        boot_volume_id = onboard_info.get('boot_volume_id')
        #Look up the host name for the VM that was specified
        instfact = compute_dom.ManagedInstanceFactory.get_factory()
        db_inst = instfact.find_instance_by_uuid(context, id)
        #Delegate to the VM Importer to actually finish the on-boarding
        importer = vm_importer.VmImporter(context, db_inst['host'])
        importer.finish_import(db_inst, os_distro, boot_volume_id)

    @staticmethod
    def _verify_valid_action(context, inst_uuid, action):
        """Internal Helper Method to verify that the Action is Valid"""
        power_specs = dict()
        try:
            #Retrieve the Instance that was specified from the Database
            spec = powervc_db.instance_power_specs_find(
                context, dict(instance_uuid=inst_uuid))
            power_specs = spec if spec is not None else dict()
        #If we couldn't get the Instance, let the main class throw exception
        except Exception:
            return
        #If this action is still pending finishing on-boarding, throw exception
        actions = power_specs.get('pending_onboard_actions')
        pending_actions = actions.split(',') if actions is not None else []
        if action in pending_actions:
            msgb = msg.BaseImporterMessageBundle()
            text = msgb.get_message_text(context, msg.INVALID_PENDING_ACTIONS,
                                         instuuid=inst_uuid, action=action)
            raise webob.exc.HTTPBadRequest(explanation=text)

    @staticmethod
    def _add_pending_onboard_attribute(context, req, server):
        """Internal Helper Method to add the Pending On-board Attribute"""
        try:
            #Retrieve the cached Server DB Instance for the given ID
            db_inst = req.get_db_instance(server['id'])
            power_specs = db_inst.get('power_specs')
            power_specs = power_specs if power_specs is not None else dict()
            pending_onboard = power_specs.get('pending_onboard_actions')
            #If the Pending On-board Actions is set on the Server Instance
            if pending_onboard is not None and len(pending_onboard) > 0:
                pending_onboard = pending_onboard.split(',')
                server['pending_onboard_actions'] = pending_onboard
        #If we got an exception, just let the Server API still succeed
        except Exception as exc:
            LOG.warn(_("Error retrieving Server "
                       "DB Instance: %s") % unicode(exc))

    @staticmethod
    def _validate_authorization(context, action):
        """Internal Helper Method to Confirm the Requester is Authorized"""
        AUTHORIZE(context, action=action)
        return context.elevated()


class HostOnboarding(extensions.ExtensionDescriptor):
    """Capability to On-board VM's for a Compute Host."""
    name = "Compute Host On-boarding"
    alias = "ibm-host-onboarding"
    namespace = "http://docs.openstack.org/compute/ext/" + \
                "ibm_host_onboarding/api/v1.2"
    updated = "2013-07-21T21:00:00-06:00"

    def get_controller_extensions(self):
        controller = ServerFinishOnboardControllerExtension()
        extension = extensions.ControllerExtension(self, 'servers', controller)
        return [extension]

    def get_resources(self):
        """Provide a Sub-Resource Extension to the os-hosts Resource"""
        resource1 = extensions.ResourceExtension(
            'onboard', controller=HostOnboardControllerExtension(),
            parent=dict(collection_name='os-hosts', member_name='host_name'))
        resource2 = extensions.ResourceExtension(
            'unmanage', controller=HostUnmanageControllerExtension(),
            parent=dict(collection_name='os-hosts', member_name='host_name'))
        resource3 = extensions.ResourceExtension(
            'all-servers', controller=HostVmListControllerExtension(),
            parent=dict(collection_name='os-hosts', member_name='host_name'))
        return [resource1, resource2, resource3]
