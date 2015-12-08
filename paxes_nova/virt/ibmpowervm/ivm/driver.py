#
#
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import json
import os
import time
import socket

#change for paxes
from nova.compute.ibm import configuration_strategy_common as config_strategy
from nova.compute.ibm import update_configuration_strategy as update_strategy
from nova.compute import power_state
from nova.compute import vm_states

from nova import conductor
from nova import context
from nova import exception as nova_exception
from nova import rpc
from nova.image import glance

from nova.openstack.common import excutils
from nova.openstack.common import importutils
from nova.openstack.common import log as logging

from nova.virt import driver

from oslo.config import cfg

from paxes_nova.objects.compute import dom as compute_dom
from paxes_nova.virt.ibmpowervm.common import exception as common_ex
from paxes_nova.virt.ibmpowervm.common import migrate_utils
from paxes_nova.virt.ibmpowervm.common import power_spec_utils
from paxes_nova import logcall
from paxes_nova.virt.ibmpowervm.ivm import constants
from paxes_nova.virt.ibmpowervm.ivm import exception
from paxes_nova.virt.ibmpowervm.ivm import operator
from paxes_nova.virt import discovery_driver as disc_driver
from paxes_nova.virt import set_instance_error_state_and_notify
from paxes_nova.volume import cinder as volume

from paxes_nova.compute import send_ui_notification
from paxes_nova.compute import notify_messages as notify_msg

from paxes_nova.network.ibmpowervm import net_assn_cleanup

from paxes_nova import _


LOG = logging.getLogger(__name__)

CONF = cfg.CONF


class IBMPowerVMDriver(driver.ComputeDriver,
                       disc_driver.ComputeDiscoveryDriver):

    """IBMPowerVM Implementation of Compute Driver."""

    def _debug(self):

        if CONF.ibmpowervm_remote_debug and \
                str.upper(CONF.ibmpowervm_remote_debug) == 'ON':

            try:
                from pydev import pydevd
                pydevd.settrace(CONF.ibmpowervm_remote_debug_host,
                                port=int(CONF.ibmpowervm_remote_debug_port),
                                stdoutToServer=True,
                                stderrToServer=True)
            except:
                LOG.exception(_("Error starting pydevd remote debugger."))
                raise

    def __init__(self, virtapi):
        self._debug()
        # Skip over the powervm CE initialization because it connects to the
        # IVM before we have a chance to create our operator.  Invoke it's
        # super instead.
        super(IBMPowerVMDriver, self).__init__(virtapi)
#        self._powervm = operator.BaseOperatorFactory.getOperatorInstance(
#            CONF.host_storage_type, virtapi)
        self._powervm = importutils.import_class(CONF.powervm_base_operator_factory).getOperatorInstance(
                            CONF.host_storage_type, virtapi)

        self.network_topo = None
        self.volume_api = volume.PowerVCVolumeAPI()

    @property
    def host_state(self):
        pass

    def init_host(self, host):
        """Initialize anything that is necessary for the driver to function,
        including catching up with currently running VM's on the given host.
        """
        ctxt = context.get_admin_context()
        # Build the Mapping of OpenStack VM Names to actual LPAR Names
        instfact = compute_dom.ManagedInstanceFactory.get_factory()
        instances = instfact.find_all_instances_by_host(ctxt, host)
        self._powervm.refresh_lpar_name_map(ctxt, instances)
        # Perform the Network Topology on Initialization to make sure is valid
        topo_class = CONF.powervm_vif_topo_driver
        topo_drv = importutils.import_object(topo_class, host_name=host)
        self.network_topo = topo_drv.get_current_config(False)

    def discover_instances(self, context):
        """
        Returns a list of all of the VM's that exist on the given Host.
        For each VM the driver needs to return a dictionary containing
        the following attributes:
            name:      The Name of the VM defined on the Hypervisor
            state:     The State of the VM, matching the power_state definition
            uuid:      The UUID of the VM when created thru OS (Optional)
            hostuuid:  The Hypervisor-specific UUID of the VM
            support:   Dictionary stating whether the VM can be managed
              status:  Whether or not it is "supported" or "not_supported"
              reasons: List of Text Strings as to why it isn't supported

        :param context:   The security context for the query
        """
        instList = self._powervm.list_lpar_for_onboard()

        # for inst in instList:
        #    inst['support'] = {'status': 'supported'}

        return instList

    def query_instances(self, context, instances):
        """
        Returns a list of VM's (matching those specified on input) with
        enough additional details about each VM to be able to import the
        VM's into the Nova Database such as OpenStack can start managing.
        For each VM the driver needs to return a dictionary containing
        the following attributes:
            uuid:      The UUID of the VM when created thru OS
            name:      The Name of the VM defined on the Hypervisor
            state:     The State of the VM, matching the power_state definition
            hostuuid:  The Hypervisor-specific UUID of the VM
            vcpus:     The number of Virtual CPU's currently allocated
            memory_mb: The amount of Memory currently allocated
            root_gb:   The amount of storage currently allocated
            ephemeral_gb:  The amount of ephemeral storage currently allocated
            flavor:    Dictionary containing the profile/desired allocations
              vcpus:     The number of Virtual CPU's defined for allocation
              memory_mb: The amount of Memory defined for allocation
              root_gb:   The amount of storage defined for allocation
              ephemeral_gb:  The ephemeral storage defined for allocation
              powervm:proc_units:  The number of proc_units defined (Optional)
            volumes:   List of dictionary objects with the attached volumes:
              uuid:      The UUID of the Volume when created thru OS (Optional)
              name:      The Name of the Volume defined on the Backend
              host:      The registered name of the Storage Provider
              provider_location:  The Volume ID known on the Backend (Optional)
            ports:     List of dictionary objects with the network ports:
              mac_address:  The MAC Address of the Port allocated to the VM
              status:       The Status of the Port, matching the definition
              segmentation_id: The VLAN ID for the Network the Port is part of
              physical_network: The Physical Network the Port is part of

        :param context:   The security context for the query
        :param instances: A list of dictionary objects for each VM containing:
            uuid:      The UUID of the VM when created thru OS
            name:      The Name of the VM defined on the Hypervisor
            hostuuid:  The Hypervisor-specific UUID of the VM
        """
        info = self._powervm.query_onboarding_instances(context, instances)

        return info

    def inventory_instances(self, context, instances):
        """
        Provides a mechanism for the Driver to gather Inventory-related
        information for the Instances provided off of the Hypervisor at
        periodic intervals.  The Driver is free from there to populate
        the information directly in the Database rather than return it.

        :param context: The security context for the query
        :param instances: A list of dictionary objects for each VM containing:
            uuid:      The UUID of the VM when created thru OS
            name:      The Name of the VM defined on the Hypervisor
            hostuuid:  The Hypervisor-specific UUID of the VM
        """
        self._powervm.refresh_lpar_name_map(context, instances)
        # Loop through each of the Instances getting the PowerSpecs for them
        for instance in instances:
            try:
                # Call the Operator to retrieve the PowerSpecs for the LPAR
                power_spec = self._powervm.get_power_specs(instance['name'])
                # Call the Common Utility to update the PowerSpcecs in the DB
                power_spec_utils.update_instance_power_specs(
                    context, self.virtapi, instance, power_spec.copy())
            # If an error occurred, just log it and move on to the next one
            except Exception as exc:
                LOG.warn(_('Error retrieving PowerSpecs for %s') %
                         instance['name'])
                LOG.exception(exc)

    def monitor_instances(self, context, instances):
        """
        Returns details about the VM's specified that can be considered
        an accurate inventory of the VM that is periodically collected.
        For each VM the driver needs to return a dictionary containing
        the following attributes:
            uuid:      The UUID of the Instance that was passed in
            name:      The Name of the Instance that was passed in
            cpu_utilization: The percent of CPU that is being used

        :param context: The security context for the query
        :param instances: A list of dictionary objects for each VM containing:
            uuid:      The UUID of the VM when created thru OpenStack
            name:      The Name of the VM when created thru OpenStack
            vm_uuid:   The Hypervisor-specific UUID of the VM (Optional)
        """
        data = self._powervm.get_cpu_utilization_of_instances(instances)

        return data

    def inventory_host(self, context):
        """
        Provides a mechanism for the Driver to gather Inventory-related
        information for the Host at periodic intervals.  The Driver is
        free from there to populate the information directly in the
        Database and should not return any information from the method.

        :param context: The security context for the query
        """
        topo_class = CONF.powervm_vif_topo_driver
        topo_drv = importutils.import_object(topo_class, host_name=CONF.host)
        network_topo, self.network_topo = (self.network_topo, None)
        # Collect the Network Topology Information and update it in the DB
        try:
            LOG.info(_('Collecting Host-Network Topology...'))
            # We retrieved the Network Topology on init_host to verify the
            # configuration is valid, so we will use that one if it is cached
            if network_topo is None:
                network_topo = topo_drv.get_current_config(False)
            # Call through the VirtAPI (switch to DOM) to update the DB
            self.virtapi.host_reconcile_network(context, network_topo)
            LOG.info(_('Done Collecting Host-Network Topology.'))
        # Just log the exception and continue on if Network Topology failed
        except Exception as e:
            LOG.error(_("Reconciling Host-Network Topology failed."))
            LOG.exception(e)

    def unmanage_host(self, context):
        """
        Tries to clean up cache area of the host being de-registered.
        Ignores all exceptions which may occur during clean up process.
        """
        try:
            # initiate clean up
            self._powervm._clean_up_images_from_host(context)
        except Exception as ex:
            LOG.debug("Cache clean up failed with exception %s" % ex)

    def get_num_instances(self):
        return len(self.list_instances())

    def instance_exists(self, instance_name):
        return self._powervm.instance_exists(instance_name)

    def list_instances(self):
        return self._powervm.list_instances()

    def get_host_cpu_stats(self):
        """Get the currently known host CPU stats."""
        freq = self._powervm.get_host_cpu_frequency()

        util_data = self._powervm.get_host_cpu_util_data()
        kernel = util_data['used_cycles']
        idle = util_data['total_cycles'] - kernel

        # NOTE(jwcroppe): Because the community code modeled these stats
        # directly after KVM, there aren't good PowerVM equivalents. For
        # purposes of re-using the community CPU monitors, we will just assume
        # that all of the used cycles map to "kernel" and we will forcibly set
        # "user" and "iowait" cycles to 0.
        return {'kernel': kernel,
                'idle': idle,
                'user': 0L,
                'iowait': 0L,
                'frequency': freq}

    def get_host_stats(self, refresh=False):
        """Return currently known host stats."""
        return self._powervm.get_host_stats(refresh=refresh)

    def get_host_uptime(self, host):
        """Returns the result of calling "uptime" on the target host."""
        return self._powervm.get_host_uptime(host)

    def macs_for_instance(self, instance):
        return self._powervm.macs_for_instance(instance)

    @send_ui_notification(notify_msg.RESTART_ERROR, notify_msg.RESTART_SUCCESS,
                          log_exception=True)
    def reboot(self, ctxt, instance, network_info, reboot_type,
               block_device_info=None, bad_volumes_callback=None):
        """Reboot the specified instance.
        :param ctxt: context.
        :param instance: Instance object as returned by DB layer.
        :param network_info:
           :py:meth:`~nova.network.manager.NetworkManager.get_instance_nw_info`
        :param reboot_type: Either a HARD or SOFT reboot
        :param block_device_info: Info pertaining to attached volumes
        :param bad_volumes_callback: Function to handle any bad volumes
            encountered
        """
        notifier = rpc.get_notifier(service='compute', host=instance['host'])
        # [11184] Changed from Context to ctxt as there is an existing
        # attribute in the imports. from nova import context.
        try:
            if ctxt is None:
                ctxt = context.get_admin_context()

            instance_name = instance['name']
            LOG.debug("Instance name: %s" % instance_name)
            lpar_state = self._powervm.get_lpar_state(instance_name)
            powerstate_str = constants.\
                IBM_POWERVM_POWER_STATE.get(lpar_state, power_state.NOSTATE)
            # Allowing reboot(only power on) in case of hard reboot.
            if ((powerstate_str is power_state.SHUTDOWN) and
                    reboot_type.lower() == 'hard'):
                try:
                    self._powervm.power_on(instance_name)
                except Exception as exc:
                    error = (_('Cannot restart virtual machine %(lpar_name). '
                               'Instance (%lpar_name) failed to boot')
                             % {'lpar_name': instance_name})
                    LOG.error(error)
                    raise nova_exception.InstanceRebootFailure(error)
            # (Redundant) Though openstack code performing state validations,
            # validating again to make sure vm state is active if in future if
            # there is any driver level internal reboot call.
            elif powerstate_str is not power_state.RUNNING:
                error = (_('Cannot restart virtual machine %(lpar_name). '
                           'The virtual machine is not in '
                           'active state.') % {'lpar_name': instance_name})
                LOG.error(error)
                raise nova_exception.InstanceRebootFailure(error)
            else:
                self._powervm.power_off_on(ctxt, instance, network_info,
                                           reboot_type,
                                           block_device_info,
                                           bad_volumes_callback)
        except exception.IBMPowerVMLPAROperationTimeout:
            LOG.debug("Operating system shutdown has timed out "
                      "for %s. Forcing an immediate restart." %
                      instance_name)
            reboot_type = 'HARD'
            info = {'msg': _('The soft restart command for the virtual '
                    'machine {instance_name} has timed out. '
                    'Proceeding with hard restart.'),
                    'instance_name': instance_name}
            notifier.error(ctxt, 'compute.instance.log', info)
            self._powervm.power_off_on(ctxt, instance, network_info,
                                       reboot_type,
                                       block_device_info,
                                       bad_volumes_callback)
        except Exception as exc:
            with excutils.save_and_reraise_exception():
                # set instance to error state
                conductor.API().instance_update(
                    ctxt, instance['uuid'],
                    vm_state=vm_states.ERROR,
                    task_state=None)
                LOG.error(exc)

    def get_host_ip_addr(self):
        """Retrieves the IP address of the hypervisor host."""
        LOG.debug("In get_host_ip_addr")
        # TODO(mrodden): use operator get_hostname instead
        hostname = CONF.powervm_mgr
        LOG.debug("Attempting to resolve %s" % hostname)
        ip_addr = socket.gethostbyname(hostname)
        LOG.debug("%(hostname)s was successfully resolved to %(ip_addr)s" %
                  {'hostname': hostname, 'ip_addr': ip_addr})
        return ip_addr

    def pause(self, instance):
        """Pause the specified instance."""
        pass

    def unpause(self, instance):
        """Unpause paused VM instance."""
        pass

    def suspend(self, instance):
        """suspend the specified instance."""
        pass

    def resume(self, instance, network_info, block_device_info=None):
        """resume the specified instance."""
        pass

    def get_available_resource(self, nodename):
        """Retrieve resource info."""
        return self._powervm.get_available_resource()

    def host_power_action(self, host, action):
        """Reboots, shuts down or powers up the host."""
        pass

    def legacy_nwinfo(self):
        """
        Indicate if the driver requires the legacy network_info format.
        """
        return False

    def manage_image_cache(self, context, all_instances):
        """
        Manage the driver's local image cache.

        Some drivers chose to cache images for instances on disk. This method
        is an opportunity to do management of that cache which isn't directly
        related to other calls into the driver. The prime example is to clean
        the cache and remove images which are no longer of interest.
        """
        self._powervm.manage_image_cache(context, all_instances)

    def get_volume_connector(self, instance):
        """Get connector information for the instance for attaching to volumes.
        """
        return self._powervm.get_volume_connector(instance)

    def attach_volume(self, context, connection_info, instance, mountpoint,
                      disk_bus=None, device_type=None, encryption=None):
        """Attach a volume to the instance at the mountpoint."""
        self._powervm.attach_volume(connection_info, instance, mountpoint)

    def detach_volume(self, connection_info,
                      instance, mountpoint, encryption=None):
        """Detach the disk attached to the instance."""
        self._powervm.detach_volume(connection_info, instance, mountpoint)

    @logcall
    def destroy(self, context, instance, network_info, block_device_info=None,
                destroy_disks=True):
        """Destroy (shutdown and delete) the specified instance."""
        try:
            self._powervm._operator.remove_vopt_media_by_instance(instance)

            self._powervm.destroy(instance['name'], destroy_disks)

            # clean up the VIFs
            self.unplug_vifs(instance, network_info)
        except Exception as e:
            with excutils.save_and_reraise_exception():
                # we need to raise InstanceTerminationFailure
                # otherwise, the instance would stuck in deleting state
                # We don't need to worry about InstanceNotFound exception
                # since compute manager has special handling for it
                if not isinstance(e, nova_exception.InstanceNotFound):
                    raise common_ex.IBMPowerVMInstanceTerminationFailure(
                        reason=e)

    @logcall
    def migrate_disk_and_power_off(self, context, instance, dest,
                                   instance_type, network_info,
                                   block_device_info=None):
        """Currently only implements resize

        :returns: disk_info dictionary that is passed as the
                  disk_info parameter to finish_migration
                  on the destination nova-compute host
        """
        operation = None
        if (self.get_host_ip_addr() != dest):
            operation = 'migrate'
        if operation:
            if CONF.host_storage_type == 'local':
                raise exception.IBMPowerVMOperationNotSupportedException(
                    operation="%s:"
                    " is not supported operation"
                    " in local disk configuration" % operation,
                    operator='Local Disk Operator')
        disk_info = {}
        try:
            disk_info = self._powervm.resize(context, instance, dest,
                                             instance_type, network_info,
                                             block_device_info)
        except Exception as e:
            LOG.warn(_('Instance %s resize failed')
                     % instance['name'])
            LOG.exception(_('Error resizing virtual machine: %s') % e)
            #
            # Issue 6721
            # even though we notify, it better to put the VM into
            # error state as rest user will not have notification
            # to know the error if the state of vm is not in error
            #
            raise e
        return disk_info

    def _get_resize_name(self, instance_name):
        """Rename the instance to be migrated to avoid naming conflicts

        :param instance_name: name of instance to be migrated
        :returns: the new instance name
        """
        name_tag = 'rsz_'

        # if the current name would overflow with new tag
        if ((len(instance_name) + len(name_tag)) > 31):
            # remove enough chars for the tag to fit
            num_chars = len(name_tag)
            old_name = instance_name[num_chars:]
        else:
            old_name = instance_name

        return ''.join([name_tag, old_name])

    @logcall
    def finish_migration(self, context, migration, instance, disk_info,
                         network_info, image_meta, resize_instance,
                         block_device_info=None, power_on=True):
        """Completes a resize, turning on the migrated instance.

        :param context: the context for the migration/resize
        :param migration: the migrate/resize information
        :param instance: the instance being migrated/resized
        :param disk_info: the newly transferred disk information
        :param network_info:
           :py:meth:`~nova.network.manager.NetworkManager.get_instance_nw_info`
        :param image_meta: image object returned by nova.image.glance that
                           defines the image from which this instance
                           was created
        :param resize_instance: True if the instance is being resized,
                                False otherwise
        :param block_device_info: instance volume block device info
        :param power_on: True if the instance should be powered on, False
                         otherwise
        """

        # TODO(jwcroppe): Nothing is currently done with 'power_on', which is a
        #                new parameter in the Havana time-frame. Resizing an
        #                inactive LPAR already leaves the LPAR stopped and
        #                cold migration isn't supported as of Paxes R1.0.
        #                This should be considered when cold migration
        #                support is added.

        #(rakatipa) Using disk_info, during resize of local backed VM
        # reverting db changes of disk_size from requested disk_size
        if (disk_info and (disk_info['is_resize'] == 'true')):
            old_metalist = instance.get('system_metadata', [])
            old_metalist['instance_type_root_gb'] = disk_info['root_gb']
            old_metalist['instance_type_ephemeral_gb'] = \
                disk_info['ephemeral_gb']
            instance['root_gb'] = disk_info['root_gb']
            instance['ephemeral_gb'] = disk_info['ephemeral_gb']

    def plug_vifs(self, instance, network_info):
        """
        Plug VIFs into networks.

        :param instance: network instance
        :param network_info: network information for the VM depoyment.
        """
        self._powervm.plug_vifs(instance, network_info)

    def unplug_vifs(self, instance, network_info):
        """
        Unplug VIFs from networks.

        :param instance: network instance
        :param network_info: network information for the VM deployment
        """
        self._powervm.unplug_vifs(instance, network_info)

    @logcall
    def spawn(self, context, instance, image_meta, injected_files,
              admin_password, network_info=None, block_device_info=None):
        """Create a new instance/VM/domain on powerVM."""

        LOG.debug("Received a request for the creation of a new "
                  "instance on PowerVM")

        # TODO(ajiang): remove the original spawn()
        # path once Paxes 2Q support is not needed. Paxes
        # 4Q only supports 2Q image migration instead of booting
        # from it.
        # if meta_version and int(meta_version) >= 2:
        try:
            callback = block_device_info['powervc_blockdev_callbacks']
            validate_volumes = callback['validate_volumes']
            attach_volumes = callback['attach_volumes']
            validate_attachment = callback['validate_attachment']
        except Exception:
            validate_volumes = None
            attach_volumes = None
            validate_attachment = None

        self._powervm.spawn(context, instance, image_meta, injected_files,
                            admin_password, network_info, block_device_info,
                            validate_volumes, attach_volumes,
                            validate_attachment)
#         else:
# old style image.
#             self._powervm.spawn(context, instance, image_meta['id'],
#                                 network_info)

    @logcall
    @send_ui_notification(notify_msg.CAPTURE_ERROR,
                          notify_msg.CAPTURE_SUCCESS, log_exception=True)
    def snapshot(self, context, instance, image_id, update_task_state):
        """Snapshots the specified instance.

        :param context: security context
        :param instance: Instance object as returned by DB layer.
        :param image_id: Reference to a pre-created image that will
                         hold the snapshot.
        :param update_task_state: Function reference that allows for updates
                                  to the instance task state.
        """
        snapshot_start = time.time()
        image_ref = image_id
        notifier = rpc.get_notifier(
            service='compute', host=self._powervm._host)
        # disk capture and glance upload
        try:
            new_snapshot_meta = self._prepare_snapshot_metadata(context,
                                                                instance,
                                                                image_id)
            image_ref = new_snapshot_meta['name']
            self._powervm.capture_image(context, instance, image_id,
                                        new_snapshot_meta, update_task_state)
        except Exception as e:
            with excutils.save_and_reraise_exception():
                LOG.exception(_("Snapshot failed with reason %s ") % e)

        snapshot_time = time.time() - snapshot_start
        inst_name = instance['name']
        LOG.info(_("%(inst_name)s captured in %(snapshot_time)s seconds") %
                 locals())

    @logcall
    def _prepare_snapshot_metadata(self, context, instance, image_id):
        """ Prepares the snapshot metadata for the new snapshot image.

        :param context: security context
        :param instance: Instance object as returned by DB layer.
        :param image_id: Reference to a pre-created image that will
                         hold the snapshot.
        :returns: a dictionary containing the image metadata to be
                used for the image update when snapshot is complete.
        """
        # Get the image data for this snapshot created by
        # the compute manager
        glance_service, img_id = glance.get_remote_image_service(context,
                                                                 image_id)

        snapshot_meta = glance_service.show(context, image_id)
        new_image_props = snapshot_meta['properties']

        # os_distro property is not set by on-boarding throw back an
        # exception, just similar to HMC
        distro = new_image_props.get('os_distro')
        if not distro:
            raise common_ex.IBMPowerVMNoOSForVM(instance_name=instance['name'])

        os_options = ['aix', 'rhel', 'sles']
        i_name = instance['name']
        if not distro in os_options:
            raise common_ex.IBMPowerVMUnsupportedOSForVM(os_distro=distro,
                                                         instance_name=i_name,
                                                         options=os_options)

        # If the image does not have a configuration strategy yet,
        # add the default configuration strategy.
        # If the base image that this VM was deployed from
        # contains a configuration strategy, it will be copied
        # forward into this image in the compute manager.
        if 'configuration_strategy' not in new_image_props:
            LOG.debug("The new image metadata does not contain "
                      "a configuration strategy. Adding one now.")

            # load ibmpowervm OVF template
            t_path = os.path.dirname(__file__)
            new_ovf_file = os.path.join(t_path, '../templates/ibmpowervm.ovf')
            with open(new_ovf_file, 'r') as f:
                ovf_descriptor = f.read()

            # Get default config strategy mappings
            mappings = self._get_default_mapping()
            cs_data = config_strategy.calc_common_metadata('ovf',
                                                           ovf_descriptor,
                                                           mappings)
            cs_json = json.dumps(cs_data)

            # Send the request to Glance to add the configuration strategy
            update_strategy.send_request(image_id, context.auth_token, 'add',
                                         config_strategy_data=cs_json)

        # Initialize the metadata that will be sent as an update
        # to the image when the capture is complete.
        # hypervisor_type and architecture were missing, added now.
        new_snapshot_meta = {
            'is_public': False,
            'name': snapshot_meta['name'],
            'status': 'active',
            'properties': {
                'image_location': 'snapshot',
                'image_state': 'available',
                'owner_id': instance['project_id'],
                'hypervisor_type': 'powervm',
                'architecture': 'ppc64'
            },
            'disk_format': 'raw',
            'container_format': 'bare'
        }

        return new_snapshot_meta

    @logcall
    def _get_default_mapping(self):
        mappings = []
        # Adapter properties
        mappings.append({
            'source': 'server.network.*.v4.address',
            'target': 'com.ibm.ovf.vmcontrol.adapter.networking.' +
            'ipv4addresses.*'
        })
        mappings.append({
            'source': 'server.network.*.v4.netmask',
            'target': 'com.ibm.ovf.vmcontrol.adapter.networking.ipv4netmasks.*'
        })
        mappings.append({
            'source': 'server.network.*.v4.use_dhcp',
            'target': 'com.ibm.ovf.vmcontrol.adapter.networking.usedhcpv4.*'
        })
        mappings.append({
            'source': 'server.network.*.slotnumber',
            'target': 'com.ibm.ovf.vmcontrol.adapter.networking.slotnumber.*'
        })

        # System wide and system networking
        mappings.append({
            'source': 'server.network.1.v4.gateway',
            'target':
            'com.ibm.ovf.vmcontrol.system.networking.ipv4defaultgateway'
        })
        mappings.append({
            'source': 'server.hostname',
            'target': 'com.ibm.ovf.vmcontrol.system.networking.hostname'
        })
        mappings.append({
            'source': 'server.domainname',
            'target': 'com.ibm.ovf.vmcontrol.system.networking.domainname'
        })
        mappings.append({
            'source': 'server.dns-client.dns_list',
            'target': 'com.ibm.ovf.vmcontrol.system.networking.dnsIPaddresses'
        })

        return mappings

    @logcall
    def confirm_migration(self, migration, instance, network_info):
        """Confirms a resize, destroying the source VM."""
        pass

    @logcall
    def finish_revert_migration(self, instance, network_info,
                                block_device_info=None):
        """Finish reverting a resize, powering back on the instance."""
        pass

    @logcall
    @send_ui_notification(notify_msg.LPM_ERROR, None, migrate_op=True,
                          log_exception=True)
    def check_can_live_migrate_source(self, ctxt, instance_ref,
                                      dest_check_data):
        """Check if it is possible to execute live migration.

        This checks if the live migration can succeed, based on the
        results from check_can_live_migrate_destination.

        :param context: security context
        :param instance_ref: nova.db.sqlalchemy.models.Instance
        :param dest_check_data: result of check_can_live_migrate_destination
        """
        if CONF.host_storage_type == 'local':
            raise exception.IBMPowerVMOperationNotSupportedException(
                operation='confirm_migration: confirm migration is '
                'not supported operation in local disk configuration',
                operator='Local Disk Operator')
        inst_name = instance_ref['name']
        # [10227: Ratnaker]Changing host to host_display_name to be
        # consistent with gui messages and for usability.
        # This property change reflects only in notifications
        host_name = CONF.host_display_name
        # dest_hst = dest_check_data['dest_sys_name']
        # Using property 'src_sys_name' instead of 'src_hst_disp_name' to be
        # consistent with HMC and KVM.
        dest_check_data['src_sys_name'] = host_name
        # check if migration is already going on for the lpar
        if self._powervm.is_vm_migrating(inst_name):
            error = (_("Migration of %s is already in progress.")
                     % inst_name)
            LOG.exception(error)
            raise common_ex.IBMPowerVMMigrationInProgress(inst_name)

        # check if the host is not a destination host.
        if self._powervm.is_dest_host_for_migration():
            error = (_("Cannot migrate %(inst)s because %(host)s is "
                       "currently participating in migration as a "
                       "target.") % {'inst': inst_name, 'host': host_name})
            LOG.exception(error)
            raise common_ex.IBMPowerVMMigrationFailed(error)
        # Check if the source host reached maximum concurrent migrations
        self._powervm.check_concur_migr_stats_source(inst_name, host_name)

        try:
            dest_check_data = \
                self._powervm._check_can_live_migrate_source(ctxt,
                                                             instance_ref,
                                                             dest_check_data)
        except Exception:
            with excutils.save_and_reraise_exception():
                self.migration_cleanup(instance_ref, source=True)

        return dest_check_data

    @logcall
    @send_ui_notification(notify_msg.LPM_ERROR_DEST, None,
                          log_exception=True)
    def check_can_live_migrate_destination(self, ctxt, instance_ref,
                                           src_compute_info, dst_compute_info,
                                           block_migration=False,
                                           disk_over_commit=False):
        """Check if it is possible to execute live migration.

        This runs checks on the destination host, and then calls
        back to the source host to check the results

        :param ctxt: security context
        :param instance_ref: nova.db.sqlalchemy.models.Instance
        :param dest: destination host
        :param block_migration: if true, prepare for block migration
        :param disk_over_commit: if true, allow disk over commit
        """
        if CONF.host_storage_type == 'local':
            raise exception.IBMPowerVMOperationNotSupportedException(
                operation='confirm_migration: confirm migration is '
                'not supported operation in local disk configuration',
                operator='Local Disk Operator')

        inst_name = instance_ref['name']
        host_name = CONF.host_display_name

        if self._powervm.is_vm_migrating_dest(inst_name):
            error = (_("Migration of %s is already in progress.")
                     % inst_name)
            LOG.exception(error)
            raise common_ex.IBMPowerVMMigrationInProgress(inst_name)

        if self._powervm.is_source_host_for_migration():
            error = (_("Cannot migrate %(inst)s because %(host)s is "
                       "currently participating in migration as a "
                       "source.") % {'inst': inst_name,
                                     'host': host_name})
            LOG.exception(error)
            raise common_ex.IBMPowerVMMigrationFailed(error)
        # Check if the destination host reached maximum concurrent migrations
        self._powervm.check_concur_migr_stats_dest(inst_name, host_name)

        try:
            migrate_data = \
                self._powervm._check_can_live_migrate_destination(
                    ctxt, instance_ref,
                    block_migration=False, disk_over_commit=False)
        except Exception:
            with excutils.save_and_reraise_exception():
                self.migration_cleanup(instance_ref)

        return migrate_data

    @logcall
    def check_can_live_migrate_destination_cleanup(self, ctxt,
                                                   dest_check_data):
        """Do required cleanup on dest host after
           check_can_live_migrate_destination

        :param ctxt: security context
        :param dest_check_data: result of check_can_live_migrate_destination
        """
        if CONF.host_storage_type == 'local':
            raise exception.IBMPowerVMOperationNotSupportedException(
                operation='confirm_migration: confirm migration is '
                'not supported operation in local disk configuration',
                operator='Local Disk Operator')
        self._powervm._check_can_live_migrate_destination_cleanup(
            ctxt, dest_check_data)

    @logcall
    @send_ui_notification(notify_msg.LPM_ERROR, None, migrate_op=True,
                          log_exception=True)
    def pre_live_migration(self, context, instance, block_device_info,
                           network_info, disk_info, migrate_data=None):
        """Preparations for live migration to dest host

        :param context: security context
        :param instance: dict of instance data
        :param block_device_info: instance block device information
        :param network_info: instance network information
        :param migrate_data: implementation specific data dict.
        """
        if CONF.host_storage_type == 'local':
            raise exception.IBMPowerVMOperationNotSupportedException(
                operation='confirm_migration: confirm migration is '
                'not supported operation in local disk configuration',
                operator='Local Disk Operator')

        self._powervm._pre_live_migration(context, instance, block_device_info,
                                          network_info, migrate_data)

    @logcall
    def ensure_filtering_rules_for_instance(self, instance, network_info,
                                            time_module=None):
        """Setting up filtering rules and waiting for its completion.

        :params instance_ref: nova.db.sqlalchemy.models.Instance object
        """
        pass

    @logcall
    @send_ui_notification(notify_msg.LPM_ERROR, None, migrate_op=True,
                          log_exception=True)
    def live_migration(self, ctxt, instance_ref, dest,
                       post_method, recover_method, block_migration=False,
                       migrate_data=None):
        """Live migration of an instance to another host

        :param ctxt: security context
        :params instance_ref:
            nova.db.sqlalchemy.models.Instance object
            instance object that is migrated.
        :params dest: destination host
        :params post_method:
            post operation method.
            expected nova.compute.manager.post_live_migration.
        :params recover_method:
            recovery method when any exception occurs.
            expected nova.compute.manager.recover_live_migration.
        :params block_migration: if true, migrate VM disk.
        :params migrate_data: implementation specific params.
        """
        if CONF.host_storage_type == 'local':
            raise exception.IBMPowerVMOperationNotSupportedException(
                operation='confirm_migration: confirm migration is '
                'not supported operation in local disk configuration',
                operator='Local Disk Operator')

        self._powervm._live_migration(ctxt, instance_ref,
                                      dest, post_method, recover_method,
                                      migrate_data, block_migration=False)

    @logcall
    def post_live_migration(self, ctxt, instance_ref, block_device_info,
                            migrate_data=None):
        '''
            Implemented the method to clean the flag in case of
            successful migration on the source host.
        '''
        self.migration_cleanup(instance_ref, source=True)

    @logcall
    @send_ui_notification(notify_msg.LPM_ERROR, notify_msg.LPM_SUCCESS,
                          migrate_op=True, log_exception=True)
    def post_live_migration_at_destination(self, ctxt, instance_ref,
                                           network_info,
                                           block_migration=False,
                                           block_device_info=None,
                                           migrate_data=None):
        """Post live migration on the destination host

        :param ctxt: security context
        :param instance_ref: dictionary of info for instance
        :param network_info: dictionary of network info for instance
        :param block_migration: boolean for block migration
        """
        if CONF.host_storage_type == 'local':
            raise exception.IBMPowerVMOperationNotSupportedException(
                operation='confirm_migration: confirm migration is '
                'not supported operation in local disk configuration',
                operator='Local Disk Operator')

        #dest_host = migrate_data['dest_sys_name']
        #src_hst_disp = migrate_data['src_hst_disp_name']

        try:
            self._powervm._post_live_migration_at_destination(
                ctxt, instance_ref, network_info,
                block_migration=False, block_device_info=None)
        except Exception:
            with excutils.save_and_reraise_exception():
                pass
        finally:
            self.migration_cleanup(instance_ref)

    @logcall
    @send_ui_notification(notify_msg.LPM_ERROR_DEST, None,
                          log_exception=True)
    def rollback_live_migration_at_destination(self, context, instance,
                                               network_info,
                                               block_device_info):
        """Rollback a failed live migration

        :param instance: instance dictionary of failed migration
        :param context: security context
        :param network_info: dictionary of network info for instance
        :param block_device_info: instance volume block device info
        """
        if CONF.host_storage_type == 'local':
            raise exception.IBMPowerVMOperationNotSupportedException(
                operation='confirm_migration: confirm migration is '
                'not supported operation in local disk configuration',
                operator='Local Disk Operator')
        try:
            self._powervm._rollback_live_migration_at_destination(context,
                                                                  instance)
        except Exception:
            with excutils.save_and_reraise_exception():
                pass
        finally:
            # Remove the migrate class object for the destination host
            # on a failing migration
            self.migration_cleanup(instance)

    def unfilter_instance(self, instance, network_info):
        """Stop filtering instance

        :param instance: dictionary of instnace info
        :param network_info: dictionary of network info for instance
        """
        pass

    def migration_cleanup(self, instance, source=False):
        """
        Used for HMC migration at manager level
        """
        inst_name = instance['name']

        if source:
            self._powervm.unset_source_host_for_migration(inst_name)
        else:
            self._powervm.unset_dest_host_for_migration(inst_name)

    @logcall
    def power_off(self, instance):
        """Power off the specified instance."""
        try:
            self._powervm.power_off(instance['name'])
        except Exception as e:
            with excutils.save_and_reraise_exception():
                # set instance to error state
                conductor.API().instance_update(
                    context.get_admin_context(), instance['uuid'],
                    vm_state=vm_states.ERROR,
                    task_state=None)
                self._reraise_exception_as_ibmpowervmlparinstancenotfound(
                    e, instance['name'])

    @logcall
    def power_on(self, context, instance, network_info,
                 block_device_info=None):
        """Power on the specified instance."""
        try:
            self._powervm.power_on(instance['name'])
        except Exception as e:
            with excutils.save_and_reraise_exception():
                # set instance to error state
                conductor.API().instance_update(
                    context.get_admin_context(), instance['uuid'],
                    vm_state=vm_states.ERROR,
                    task_state=None)
                self._reraise_exception_as_ibmpowervmlparinstancenotfound(
                    e, instance['name'])

    def _reraise_exception_as_ibmpowervmlparinstancenotfound(self, pvc_cmd_ex,
                                                             instance_name):
        """
        Check whether the exception is instance of IBMPowerVMCommandFailed.
        If so, check the error contain VIOSE010401A1-0042. If it does,
        re-raise with IBMPowerVMLPARInstanceNotFound
        """
        if isinstance(pvc_cmd_ex, exception.IBMPowerVMCommandFailed):
            # check whether the error contains  VIOSE010401A1-0042
            # indicating the lpar is not found
            if pvc_cmd_ex.error is not None:
                if any('VIOSE010401A1-0042' in err
                       for err in pvc_cmd_ex.error):
                    raise exception.IBMPowerVMLPARInstanceNotFound(
                        instance_name=instance_name)

    @logcall
    def remove_volume_device_at_destination(self, context, instance, cinfo):
        """ clean up volume device at destination during Rollback

        :param context: security context
        :param instance: instance dictionary of failed migration
        :param cinfo: block_disk_map['connection_info']
        """

        self._powervm.remove_volume_device_at_destination(context,
                                                          instance,
                                                          cinfo)

    def get_info(self, instance):
        """Get the current status of an instance."""
        info = {}
        notifier = rpc.get_notifier(
            service='compute', host=self._powervm._host)
        try:
            info = self._powervm.get_info(instance['name'])
        except exception.IBMPowerVMLPARInstanceNotFound as e:
            LOG.error(_('LPAR %s not found') % instance['name'])
            info = {'state': power_state.NOSTATE}
            # set instance to error state
            # extract the current vm_state
            inst_vm_state = instance['vm_state']
            if(inst_vm_state != vm_states.ERROR):
                set_instance_error_state_and_notify(instance)
        return info

    def get_available_nodes(self, refresh=False):
        """Returns nodenames of all nodes managed by the compute service.

        This method is for multi compute-nodes support. If a driver supports
        multi compute-nodes, this method returns a list of nodenames managed
        by the service. Otherwise, this method should return
        [hypervisor_hostname].
        PowerVC only manages 1 node per compute service. Also, we will try
        to extract the cached hypervisor_hostname before trying to extract
        the hypervisor_hostname from host_stats
        """
        # extract hypervisor_hostname from _powervm instead of CONF
        # The very first time we start the compute node, the CONF will
        # not have the hypervisor_hostname set since it was set during the init
        hyp_hostname = self._powervm._hypervisor_hostname
        LOG.debug("hyp_hostname = %s" % hyp_hostname)
        if not hyp_hostname:
            stats = self.get_host_stats(refresh=True)
            hyp_hostname = stats['hypervisor_hostname']

        return [hyp_hostname]

    def prep_instance_for_attach(self, context, instance, storage_type):
        pass

    def cleanup_network_associations(self, context, host):
        """
        cleans up network associations of deleted networks
        """
        net_assn_cleanup.cleanup_network_associations(
            context, host_name=host)

    def log_free_disk(self, is_audit):
        """
        Called from resource tracker to log free disk on host
        :param is_audit: is this an audit log (Bool)
        :param resources: Host resources (Dict)
        """
        stats = self.get_host_stats()
        # If storage is local, log free disk on host.
        if CONF.host_storage_type == 'local' and stats.get('disk_available'):
            free_disk = stats['disk_available'] / 1024
            if is_audit:
                LOG.audit(_("Free disk (GB): %s") % free_disk)
            else:
                LOG.debug("Hypervisor: free disk (GB): %s" % free_disk)

    def is_instance_on_host(self, instance_uuid, instance_name):
        """
        Check if an instance exists on this host
        """
        return self._powervm.instance_exists(instance_name)

    def ssh_trust(self, public_key):
        return self._powervm.ssh_trust(public_key)

    def get_ssh_console(self, context, instance):
        #get host ip address
        hostip = self.get_host_ip_addr()
        #get instance lpar id 
        #vmid = instance["power_specs"]["vm_id"]
        vminfo = self._powervm.get_power_specs(instance['name'])
        vmid = vminfo["vm_id"]
        
        return {'host': hostip, 'port': vmid, 'internal_access_path': None}

    def image_create(self, context, image_meta, image_data):
        image_data['context'] = context
        location = image_data.get('location', None)
        if not location:
            raise
        self._powervm.image_create(image_meta, image_data)
        
    def image_attach_volume(self, context, image_meta, image_data, 
                            connection_info, encryption=None):
        image_data['context'] = context
        self._powervm.image_attach_volume(image_meta, image_data, 
                                          connection_info,encryption)
