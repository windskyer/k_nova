#
# =================================================================
# =================================================================

"""
This is a Task Module that binds the host_seas Network REST API with
the DOM Model by creating AOMs that are used as view objects for
the user.
"""
from nova import db
from nova import exception as novaexception
from nova.openstack.common import lockutils
from nova.openstack.common import log as logging
from oslo.config import cfg

from powervc_nova.network.common.api_suppress_mixin import NetworkAPISuppressor
from powervc_nova.network.common import exception as net_excp
from powervc_nova.network.powerkvm.agent.common import exception as agt_excp
from powervc_nova.network.powerkvm import agent
from powervc_nova.objects.network import dom_kvm
from powervc_nova import compute
from powervc_nova.network.common import rpc_caller

from powervc_nova import _

LOG = logging.getLogger(__name__)
CONF = cfg.CONF


class HostOvsTask(NetworkAPISuppressor):
    """
    This class handles processing for the host-ovs REST API.
    """

    def __init__(self):
        # Invoke superclass constructor
        super(HostOvsTask, self).__init__()

    @lockutils.synchronized('host_ovs', 'host-ovs-')
    def create_host_ovs(self, context, req, body, force):
        """
        Create OpenVSwitch configuration.
        :param context: The HTTP request context.
        :param req: The HTTP request object.
        :param body: The HTTP request body.
        :param force: Whether the operation should be forced through, even if
                      potentially un-intended consequences are detected.
        :returns: An exception list.
        """
        # This class should only be used in PowerKVM environments
        self.raise_if_not_powerkvm()

        # Not yet implemented
        raise net_excp.IBMPowerVMHostOvsCreateNotImpl()

    @lockutils.synchronized('host_ovs', 'host-ovs-')
    def get_host_ovs(self, context, req, host_name=None):
        """
        Read OpenVSwitch configuration.
        :param context: The HTTP request context.
        :param req: The HTTP request object.
        :param host_name: An optional host name to filter data with.
        :returns: A dictionary of host-ovs data.
        """
        # This class should only be used in PowerKVM environments
        self.raise_if_not_powerkvm()

        # If a host was specified, verify it exists
        host_list = self._get_all_host_names(context)
        if host_name is not None:
            if host_name not in host_list:
                raise novaexception.ComputeHostNotFound(host=host_name)
            host_list = [host_name]

        LOG.debug("host-ovs GET for host(s) %s" % host_list)

        # Parse the vswitch_name from the request, if present
        vswitch_name = None
        if 'vswitch_name' in req.GET:
            vswitch_name = req.GET['vswitch_name']
        if vswitch_name == '':
            raise net_excp.IBMPowerKVMVswitchNotFound(ovs_name='')

        # Make the remote call to the endpoint(s) to fetch ovs data
        dom_list = rpc_caller.get_host_ovs(context, host_list)

        # Filter based on the vswitch name, if necessary
        self._filter_vswitch(dom_list, vswitch_name)

        # Filter empty linux bridges
        self._filter_empty_bridges(dom_list)

        # Convert DOM objects into REST API dictionary format
        host_dict = self._dom_to_dict(dom_list)
        return host_dict

    @lockutils.synchronized('host_ovs', 'host-ovs-')
    def update_host_ovs(self, context, req, id, body, force, rollback):
        """
        Update OpenVSwitch configuration.
        :param context: The HTTP request context.
        :param req: The HTTP request object.
        :param id: The host to update.
        :param body: The HTTP request body.
        :param force: Whether the operation should be forced through, even if
                      potentially un-intended consequences are detected.
        :param rollback: Whether the operation should be rolled back before
                         completion, often to test the rollback mechanism.
        :returns: A list of dictionaries with warnings and errors.
        """
        # This class should only be used in PowerKVM environments
        self.raise_if_not_powerkvm()

        # Verify the host we're trying to update exists
        host_list = self._get_all_host_names(context)
        if id not in host_list:
            raise novaexception.ComputeHostNotFound(host=id)

        # Performing an update requires knowledge of the current state of the
        # host, retrieve a DOM of the current state with a remote call.
        current_dom = rpc_caller.get_host_ovs(context, [id])[0]
        if len(current_dom.error_list) > 0:
            raise net_excp.IBMPowerKVMVswitchUpdateException()

        # Validate the JSON in the PUT request body
        error_warning_dict = self._validate_put_body(body, current_dom)
        if error_warning_dict:
            return [error_warning_dict]

        # Get a DOM of the data the caller wants to update to
        body = self._augment_put_request(body, id)
        desired_dom = self._dict_to_dom(body, [current_dom])[0]

        # Make the remote call to the endpoint(s) to set ovs data
        error_warning_dict = rpc_caller.update_host_ovs(context,
                                                        id,
                                                        desired_dom.to_dict(),
                                                        force,
                                                        rollback)
        return error_warning_dict

    @lockutils.synchronized('host_ovs', 'host-ovs-')
    def delete_host_ovs(self, context, req, body):
        """
        Delete OpenVSwitch configuration.
        :param context: The HTTP request context.
        :param req: The HTTP request object.
        :param body: The HTTP request body.
        :returns: An exception list.
        """
        # This class should only be used in PowerKVM environments
        self.raise_if_not_powerkvm()

        # Not yet implemented
        raise net_excp.IBMPowerVMHostOvsDeleteNotImpl()

    def _augment_put_request(self, body, host_name):
        """
        Augment the JSON PUT request body with other data needed internally
        by the REST API.  For example: the host_name is specified on the URL
        during the external call, but internally we need to add it to the body
        for processing.

        :param body: The JSON body that came in with the REST PUT.
        :param host_name: The name of the nova host that is being updated.
        :return: A dict of the augmented body.
        """
        if 'host-ovs' in body and len(body['host-ovs']) > 0:
            body['host-ovs'][0]['host_name'] = host_name
        return body

    def _dom_to_dict(self, host_list):
        """
        Convert a list of DOM objects to a REST API dictionary format.

        :param host_list: A list of host KVM DOM objects.
        :returns: A dictionary in the REST API format.
        """
        host_ovs_dict = {'host-ovs': []}
        for host_obj in host_list:
            host_dict = {}
            host_dict['host_name'] = host_obj.host_name
            host_dict['ovs'] = []
            host_dict['unused_components'] = []
            for ovs in host_obj.ovs_list:
                ovs_dict = {'name': ovs.name,
                            'state': ovs.get_state(),
                            'speed_mb': ovs.get_speed_mb(),
                            'components': []}
                for ovs_port in ovs.ovs_port_list:
                    ovs_port_dict = {'name': ovs_port.name,
                                     'state': ovs_port.get_state(),
                                     'speed_mb': ovs_port.get_speed_mb(),
                                     'components': []}
                    for adapter in ovs_port.port_list:
                        adapter_dict = self._adapter_dom_to_dict(adapter)
                        ovs_port_dict['components'].append(adapter_dict)
                    ovs_dict['components'].append(ovs_port_dict)
                if ovs.ip_addresses:
                    ovs_dict['ip_addresses'] = []
                    for ip_obj in ovs.ip_addresses:
                        ip_dict = {'address': ip_obj.address}
                        ovs_dict['ip_addresses'].append(ip_dict)
                host_dict['ovs'].append(ovs_dict)
            for unused_component in host_obj.unused_component_list:
                adapter_dict = self._adapter_dom_to_dict(unused_component)
                host_dict['unused_components'].append(adapter_dict)
            if host_obj.error_list:
                host_dict['errors'] = []
                for error_obj in host_obj.error_list:
                    error_dict = {'message': error_obj.message}
                    host_dict['errors'].append(error_dict)
            host_ovs_dict['host-ovs'].append(host_dict)
        return host_ovs_dict

    def _adapter_dom_to_dict(self, adapter):
        """
        Convert a LogicalEthernetAdapter DOM object to REST API dictionary
        format.

        :param adapter: A LogicalEthernetAdapter DOM object.
        :returns: A dictionary in the REST API format.
        """
        adapter_dict = {'name': adapter.name,
                        'state': adapter.get_state()}
        if adapter.get_speed_mb():
            adapter_dict['speed_mb'] = int(adapter.get_speed_mb())
        if hasattr(adapter, 'port_list') and adapter.port_list:
            comp_list = []
            for phs_port in adapter.port_list:
                comp_dict = self._adapter_dom_to_dict(phs_port)
                comp_list.append(comp_dict)
            adapter_dict['components'] = comp_list
        if adapter.ip_addresses:
            adapter_dict['ip_addresses'] = []
            for ip_obj in adapter.ip_addresses:
                ip_dict = {'address': ip_obj.address}
                adapter_dict['ip_addresses'].append(ip_dict)
        return adapter_dict

    def _dict_to_dom(self, host_dict, host_dom_list):
        """
        Convert a dictionary in REST API format to DOM objects.

        Unfortunately, this is not a trivial mapping because some data in the
        DOM objects is ignored when the REST API dicts are made.  This is done
        to hide complexity from the end user.  The specific data that is hidden
        is the adapter/component type (bridge vs. bond. vs. physical).  In
        order to correctly reconstruct the DOM objects based on a REST API
        dict, the host needs to be inspected for its DOM to get access to this
        missing data.  The component/adapter type cannot be inferred otherwise.

        Furthermore, it is possible that the dict contains data not currently
        present on the host.  Consider the case where a REST API POST/PUT dict
        is passed in, new OpenVSwitch ports may be needed.  In this case, the
        name of the OpenVSwitch port name will be generated.  The name of the
        child component will be used with a number appended if necessary.  If
        no child component name is provided, the OVSPort name will be generated
        using as 'pvc-ovs-port-X' where X is a sequentially incremented number.

        :param host_dict: A dictionary in the REST API format.
        :param host_dom_list: A list of host KVM DOM objects that represents
                              the current state of each host.  See above
                              explanation for why this is needed.
        :returns: A list of host KVM DOM objects.
        """
        host_list = []
        for host_dict in host_dict['host-ovs']:
            host_obj = dom_kvm.HostOVSNetworkConfig(host_dict['host_name'])

            # Build the OVS objects for this host
            for ovs_dict in host_dict.get('ovs', []):
                ovs_obj = dom_kvm.OpenVSwitch(ovs_dict['name'])
                for ovs_port_dict in ovs_dict['components']:
                    ovs_port_name = None
                    if('components' in ovs_port_dict and
                       len(ovs_port_dict['components']) > 0):
                        comps = [comp_dict.get('name', None)
                                 for comp_dict in ovs_port_dict['components']]
                        comps = filter(None, comps)
                        ovs_port_name = self._find_ovs_port(
                            ovs_port_dict.get('name', None),
                            comps,
                            host_obj.host_name,
                            host_dom_list)
                    ovs_port_obj = dom_kvm.OVSPort(ovs_port_name)
                    for component_dict in ovs_port_dict['components']:
                        comp_obj = self._adapter_dict_to_dom(
                            component_dict,
                            host_obj.host_name,
                            host_dom_list)
                        ovs_port_obj.port_list.append(comp_obj)
                    ovs_obj.ovs_port_list.append(ovs_port_obj)
                if 'ip_addresses' in ovs_dict and ovs_dict['ip_addresses']:
                    for ip_dict in ovs_dict['ip_addresses']:
                        ip_obj = dom_kvm.IPAddress(ip_dict['address'])
                        ovs_obj.ip_addresses.append(ip_obj)
                host_obj.ovs_list.append(ovs_obj)

            # Generate the list of unused components for this host
            cur_host_dom = None
            for h in host_dom_list:
                if h.host_name == host_dict['host_name']:
                    cur_host_dom = h
                    break
            for port in cur_host_dom.get_all_ports():
                if not host_obj.contains_port(port):
                    host_obj.unused_component_list.append(port)

            # Generate the error objects for this host
            for error_dict in host_dict.get('errors', []):
                if 'message' in error_dict:
                    error_obj = dom_kvm.Error(error_dict['message'])
                    host_obj.error_list.append(error_obj)

            host_list.append(host_obj)
        return host_list

    def _adapter_dict_to_dom(self, adapter_dict, host_name, host_dom_list):
        """
        Convert a REST API component dictionary to a LogicalEthernetAdapter DOM
        object.  The type of the adapter is inferred based on its attributes.

        :param adapter: A dictionary in the REST API format.
        :param host_name: The name of the compute host.
        :param host_dom_list: A list of host KVM DOM objects that represents
                              the current state of each host.
        :returns: A LogicalEthernetAdapter DOM object.
        """
        # Find the existing DOM object for this adapter to check its type
        existing_adapter_obj = self._find_adapter_obj(adapter_dict['name'],
                                                      host_name,
                                                      host_dom_list)

        # Parse adapter dict based on inferred type
        if isinstance(existing_adapter_obj, dom_kvm.EthernetBond):
            adapter_obj = dom_kvm.EthernetBond(adapter_dict['name'])
            # Changing bond components via the REST API is not supported, so we
            # can use the existing components instead of parsing the API
            # request.
            adapter_obj.port_list.extend(existing_adapter_obj.port_list)
        elif isinstance(existing_adapter_obj, dom_kvm.LinuxBridge):
            adapter_obj = dom_kvm.LinuxBridge(adapter_dict['name'])
            # Changing bridge components via the REST API is not supported, so
            # we can use the existing components instead of parsing the API
            # request.
            adapter_obj.port_list.extend(existing_adapter_obj.port_list)
        elif isinstance(existing_adapter_obj, dom_kvm.PhysicalPort):
            adapter_obj = dom_kvm.PhysicalPort(
                adapter_dict['name'], existing_adapter_obj.get_state())
            adapter_obj.speed_mb = existing_adapter_obj.get_speed_mb()

        if len(existing_adapter_obj.ip_addresses) > 0:
            for ip in existing_adapter_obj.ip_addresses:
                ip_obj = dom_kvm.IPAddress(ip.address)
                adapter_obj.ip_addresses.append(ip_obj)

        return adapter_obj

    def _find_adapter_obj(self, adapter_name, host_name, host_dom_list):
        """
        Find an adapter DOM object by name in a full DOM.

        :param adapter_name: The name of the adapter to find, such as 'eth1'.
        :param host_name: The name of the host the adapter is on.
        :param host_dom_list: A list of host KVM DOM objects that represents
                              the current state of each host.
        :return: A LogicalEthernetPort adapter object.
        """
        # Find the host DOM object that (allegedly) contains the adapter
        host_dom = None
        for h in host_dom_list:
            if h.host_name == host_name:
                host_dom = h

        # Find the first adapter that is not an OVSPort with the specified name
        for ovs in host_dom.ovs_list:
            for ovs_port in ovs.ovs_port_list:
                port = self._recursive_check_port_list(adapter_name,
                                                       ovs_port.port_list)
                if port:
                    return port
        port = self._recursive_check_port_list(adapter_name,
                                               host_dom.unused_component_list)
        if port:
            return port

        return None

    def _recursive_check_port_list(self, adapter_name, port_list):
        """
        Infer the type of an adapter based on its name and a DOM.

        :param adapter_name: The name of the adapter to find, such as 'eth1'.
        :param port_list: A list of LogicalEthernetAdapter KVM DOM objects.
        :return: A LogicalEthernetAdapter KVM DOM object
        """
        for port in port_list:
            if port.name == adapter_name:
                return port
            if hasattr(port, 'port_list'):
                child_port = self._recursive_check_port_list(adapter_name,
                                                             port.port_list)
                if child_port:
                    return child_port
        return None

    def _find_ovs_port(self, ovs_port_name, comp_name_list, host_name,
                       host_dom_list):
        """
        Search the host_dom_list for a host with host_name, and then find
        an OVSPort object with ovs_port_name. If an ovs_port_name was not
        provided, then find an OVSPort object that contains the passed
        comp_obj.  For details on why this method is necessary, see the method
        doc for _dict_to_dom().

        :param ovs_port_name: A name of an OVSPort to attempt to use if it
                              exists.
        :param comp_name_list: An list of adapter/component names, such as the
                               names of PhysicalPorts, an EthernetBonds or
                               LinuxBridges.
        :param host_name: The name of the compute host.
        :param host_dom_list: A list of host KVM DOM objects to search.
        :returns: The OVSPort name as a string.
        """
        host_obj = None
        for a_host_obj in host_dom_list:
            if a_host_obj.host_name == host_name:
                host_obj = a_host_obj
                break

        if not host_obj:
            # The host doesn't even exist, generate a port name
            return self._gen_ovs_port_name(None, None)

        if not comp_name_list or not len(comp_name_list):
            # Calling this method without providing child components makes no
            # sense, just generate a port name
            return self._gen_ovs_port_name(None, None)

        # If only a single component was specified, the OVSPort name must match
        if len(comp_name_list) == 1:
            return comp_name_list[0]

        # If multiple components were specified then an OVSPort bond will be
        # created.  This means the ovs_port_name cannot match any existing
        # component.  If the requested ovs_port_name matches an existing
        # component, just clear it out since we can't use it.
        if ovs_port_name and host_obj.contains_port_by_name(ovs_port_name):
            ovs_port_name = None

        # If an OVS port name was not specified, find an OVSPort object with
        # all specified comps as its children
        if not ovs_port_name:
            for ovs_obj in host_obj.ovs_list:
                for ovs_port_obj in ovs_obj.ovs_port_list:
                    ethernet_name_list = []
                    for ethernet_obj in ovs_port_obj.port_list:
                        ethernet_name_list.append(ethernet_obj.name)
                    contains_all = True
                    for comp_name in comp_name_list:
                        if comp_name not in ethernet_name_list:
                            contains_all = False
                            break
                    if contains_all:
                        ovs_port_name = ovs_port_obj.name

        # If multiple components were specified, an OVS bond will be created
        # and the bond name cannot match any component name
        for comp_name in comp_name_list:
            if comp_name == ovs_port_name:
                ovs_port_name = '%s%s' % (comp_name, '_bond')
                return self._gen_ovs_port_name(ovs_port_name, host_obj)

        # If an OVS port name was specified, see if it exists
        if ovs_port_name:
            for ovs_obj in host_obj.ovs_list:
                for ovs_port_obj in ovs_obj.ovs_port_list:
                    if ovs_port_obj.name == ovs_port_name:
                        return ovs_port_name

        # No match found so generate a name
        if ovs_port_name:
            # The caller provided some random name, try to accomodate it
            return self._gen_ovs_port_name(ovs_port_name, host_obj)
        else:
            # The caller provided no name, generate one based off a child
            bond_name = '%s%s' % (comp_name_list[0], '_bond')
            return bond_name

    def _gen_ovs_port_name(self, desired_name, host_obj):
        """
        Typically an OVSPort is named the same as the component it contains,
        however there be cases where this name is already taken.  This function
        handles generating an OVSPort name if needed.

        :param desired_name: The desired name for the OVSPort, typically this
                             is the name of the component contained by the
                             OVSPort.
        :param host_obj: A host KVM DOM object to search for existing ports.
        :returns: A string that can be used as an OVSPort name.
        """
        if not desired_name:
            desired_name = 'pvc-ovs-port'

        # Check to see if the desired name can be used
        if not self._does_ovs_port_exist(desired_name, host_obj):
            return desired_name

        # Increment a number after the desired name until a spot is found
        for i in range(0, 100000):
            name = desired_name + '-' + str(i)
            if not self._does_ovs_port_exist(name, host_obj):
                return name

        # Something is wrong, thousands of OVSPorts with this name exist.
        LOG.error(_('Failed to generate OVSPort name from desired_name %s') %
                  desired_name)
        return ''

    def _does_ovs_port_exist(self, port_name, host_obj):
        """
        Checks all OVSPorts on the host_obj to see if the port_name exists.

        :param port_name: The OVSPort name to search for
        :param host_obj: The host object to search through for port_name.
        :returns: True if port_name is found in host_obj, False otherwise.
        """
        if host_obj:
            for ovs_obj in host_obj.ovs_list:
                for ovs_port_obj in ovs_obj.ovs_port_list:
                    if port_name == ovs_port_obj.name:
                        return True
        return False

    def _get_all_host_names(self, context):
        """
        Finds a list of compute node names.
        :param context: DB context object.
        :returns: A list of compute node names.
        """
        compute_nodes = db.compute_node_get_all(context)
        return_nodes = []
        for compute in compute_nodes:
            if compute['service']:
                return_nodes.append(compute['service']['host'])
        return return_nodes

    def _filter_vswitch(self, dom_list, vswitch_name):
        """
        Filter the specified dom_list for entries that contain the specified
        vswitch_name.  This is for the ?vswitch_name= URL parameter.

        :param dom_list: A list of HostOVSNetworkConfig objects.  This param
                         will be modified by this function.
        :param vswitch_name: The name of the vswitch to filter host objects on.
        """
        if vswitch_name is None:
            return

        # Check to see if there are any vswitches to filter on
        ovs_pre_filter = False
        for host in dom_list:
            if len(host.ovs_list) > 0:
                ovs_pre_filter = True
                break

        # Check to see if any errors were found
        errors = False
        for host in dom_list:
            if len(host.error_list) > 0:
                errors = True
                break

        # Apply filter
        for host in dom_list:
            host.ovs_list[:] = [x for x in host.ovs_list
                                if x.name == vswitch_name]

        # Check to see if there are any vswitches after filtering
        ovs_post_filter = False
        for host in dom_list:
            if host.ovs_list and len(host.ovs_list) > 0:
                ovs_post_filter = True
                break

        # Raise exception if all vswitches were filtered out and no other
        # errors were found.
        if ovs_pre_filter and not ovs_post_filter and not errors:
            raise net_excp.IBMPowerKVMVswitchNotFound(ovs_name=vswitch_name)

    def _filter_empty_bridges(self, dom_list):
        """
        Filter the specified dom_list and remove linux bridges with no child
        components from the unused component list for each host.

        :param dom_list: A list of HostOVSNetworkConfig objects.  This param
                         will be modified by this function.
        """
        def _empty_bridge(x):
            if isinstance(x, dom_kvm.LinuxBridge) and not len(x.port_list):
                return True
            return False
        for host in dom_list:
            host.unused_component_list[:] = \
                [x for x in host.unused_component_list if not _empty_bridge(x)]

    def _validate_put_body(self, body, current_dom):
        """
        Validate the host-ovs PUT request body JSON.  The JSON is assumed to
        be syntactically valid (invalid JSON is caught by a higher layer).
        This method will check for things like incorrect adapters names,
        incorrect ovs structure, and unrecognized key/value pairs.

        :param body: A dict of the JSON PUT request body.
        :param current_dom: A DOM of the host identified by the id param.
        :return error_warning_dict: Any warnings or errors to send back to the
                                    REST API caller.
        """
        errors = {}

        if not body or not len(body):
            exc = agt_excp.IBMPowerKVMOVSNoValidDomSpecified()
            errors = self._put_error(errors, exc)
            return errors

        # Check for unknown keys in outermost dictionary
        for key in body:
            if key != 'host-ovs':
                exc = agt_excp.IBMPowerKVMUnknownKey(key=key)
                errors = self._put_error(errors, exc)
        if not body.get('host-ovs', None):
            return errors

        # Only one host at a time can be updated with host-ovs PUT
        if len(body['host-ovs']) != 1:
            exc = agt_excp.IBMPowerKVMOneHostAtTime()
            errors = self._put_error(errors, exc)
        if errors:
            return errors

        # Check for unknown keys on the host
        for key in body['host-ovs'][0]:
            if key != 'ovs':
                exc = agt_excp.IBMPowerKVMUnknownKey(key=key)
                errors = self._put_error(errors, exc)

        # Check for unknown keys on each vswitch and component
        for ovs_dict in body['host-ovs'][0]['ovs']:
            try:
                self._recurse_name_comp_keys(ovs_dict)
            except Exception as exc:
                errors = self._put_error(errors, exc)

        # Check for br-int (which might have a different name)
        for ovs_dict in body['host-ovs'][0]['ovs']:
            ovs_name = ovs_dict.get('name', None)
            if ovs_name == CONF.integration_bridge:
                exc = agt_excp.IBMPowerKVMOVSIntBridgeSpecifiedError(
                    bridge_name=ovs_name)
                errors = self._put_error(errors, exc)

        # Check for non-existent vswitches
        for ovs_dict in body['host-ovs'][0]['ovs']:
            ovs_name = ovs_dict.get('name', None)
            if(not current_dom.find_ovs_obj(ovs_name) and not
               ovs_name == CONF.integration_bridge):
                exc = agt_excp.IBMPowerKVMOVSDoesNotExistError(
                    switch_name=ovs_name)
                errors = self._put_error(errors, exc)

        # Check for non-existent components
        for ovs_dict in body['host-ovs'][0]['ovs']:
            for ovs_port_dict in ovs_dict.get('components', []):
                for port_dict in ovs_port_dict.get('components', []):
                    try:
                        self._recurse_port_exists(port_dict, current_dom)
                    except Exception as exc:
                        errors = self._put_error(errors, exc)

        # OVSPorts with one child port must have the name of the child port
        for ovs_dict in body['host-ovs'][0]['ovs']:
            for ovs_port_dict in ovs_dict.get('components', []):
                if len(ovs_port_dict.get('components', [])) == 1:
                    if ovs_port_dict.get('name', None):
                        child_name = ovs_port_dict['components'][0]['name']
                        if ovs_port_dict['name'] != child_name:
                            exc = agt_excp.IBMPowerKVMPortNameMismatch(
                                ovs_port_name=ovs_port_dict['name'],
                                port_name=child_name)
                            errors = self._put_error(errors, exc)

        return errors

    def _put_error(self, errors_dict, exception):
        """
        Convienence method for adding exception to a dictionary in the host-ovs
        REST API PUT response body format.

        :param errors_dict: The error dictionary to add the exception to.
        :param exception: The exception to add to the error dictionary.
        :return errors_dict: The updated errors dict.
        """
        if agent.ERRORS_KEY not in errors_dict:
            errors_dict[agent.ERRORS_KEY] = []
        errors_dict[agent.ERRORS_KEY].append({'message': '%s' % exception})
        return errors_dict

    def _recurse_name_comp_keys(self, sub_dict):
        """
        Recursively search a portion of a dictionary for specific keys, and
        raise exception if any other keys are found.

        :param sub_dict: A portion of a larger dictionary to search.
        """
        for key in sub_dict:
            if key != 'name' and key != 'components':
                raise agt_excp.IBMPowerKVMUnknownKey(key=key)
            if key == 'components':
                for comp_dict in sub_dict['components']:
                    self._recurse_name_comp_keys(comp_dict)

    def _recurse_port_exists(self, sub_dict, current_dom):
        """
        Recursively search a dictionary to check if the named components exist.

        :param sub_dict: A portion of a larger dictionary to search.
        """
        port_name = sub_dict.get('name', None)
        if not current_dom.contains_port_by_name(port_name):
            raise agt_excp.IBMPowerKVMOVSAdapterNotAvailableError(
                adapter_name=port_name)

        for port_dict in sub_dict.get('components', []):
            for child_port_dict in port_dict.get('components', []):
                self._recurse_port_exists(child_port_dict, current_dom)
