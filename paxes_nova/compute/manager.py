#
#
# All Rights Reserved.
# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# Copyright 2011 Justin Santa Barbara
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

"""Extends the Compute Manager to provide additional Capabilities for PowerVC
   and to override any necessary behavioral changes from the Community code.
   This code is organized into the following categories:
    1. Overriding Compute Manager Initialization
    2. Compute-related Manager Extensions
    3. Discovery-related Manager Extensions
    4. Image-related Manager Extensions
    5. Migration-related Manager Extensions
    6. Monitor-related Manager Extensions
    7. Network-related Manager Extensions
    8. Storage-related Manager Extensions
    9. Overriding the ResourceTracker and VirtAPI
"""
import copy
import errno
import uuid
import fcntl
import functools
import os
from eventlet import greenthread
from oslo.config import cfg
import time

import nova.context
import nova.db.api
from nova import exception
from nova import network
from nova import rpc
from nova import utils
from nova.compute import flavors
from nova.compute import manager
from nova.compute import resource_tracker
from nova.compute import power_state
from nova.compute import task_states
from nova.compute import utils as compute_utils
from nova.compute import vm_states
from nova.image import glance
from nova.objects import instance as instance_obj
from nova.network import model as network_model
from nova.network import neutronv2
from nova.objects import base as obj_base
from nova.objects import block_device as blkdev_obj
from nova.openstack.common import excutils
from nova.openstack.common import importutils
from nova.openstack.common import jsonutils
from nova.openstack.common import lockutils
from nova.openstack.common import log as logging
from nova.openstack.common import periodic_task
from nova.openstack.common import timeutils
from nova.openstack.common.db import api as db_api
from nova.db.sqlalchemy import api as session
from nova.virt import block_device as blk_dev
from nova.virt import driver

from paxes_nova.compute import EVENT_TYPE_WARN
from paxes_nova.compute import send_ui_notification
from paxes_nova.compute import notify_messages as notify_msg

from paxes_nova import logcall
from paxes_nova.compute import api as pvc_compute_rpcapi
from paxes_nova.compute import exception as pwrvc_excep
from paxes_nova.conductor import api as conductor
from paxes_nova.db import api as db
from paxes_nova.db.network import models as dom
from paxes_nova.objects.compute import dom as compute_dom
from paxes_nova.virt import discovery_driver
from paxes_nova.virt import netutils
from paxes_nova.virt.ibmpowervm.common import exception as pvm_exc
from paxes_nova.virt.ibmpowervm.common import migrate_utils
from paxes_nova.virt.ibmpowervm.common.constants \
    import CONNECTION_TYPE_NPIV, CONNECTION_TYPE_SSP
from paxes_nova.volume import cinder as volume
from paxes_nova.objects.network import dom_kvm
from paxes_nova.network.powerkvm import agent

from paxes_nova import _


# Constant for Activating state used post-deploy
ACTIVATING = 'activating'
IMAGE_BOOT_DEVICE_PROPERTY = 'boot_device_name'
LOG = logging.getLogger(__name__)


ibmpowervm_interval_opts = [
    cfg.IntOpt('ibmpowervm_activating_timeout', default=1200,
               help='Timeout (seconds) for VM in activating after deploy.'),
    cfg.IntOpt('ibmpowervm_iso_detach_timeout',
               default=3600,
               help='Timeout in seconds for removal of optical post-deploy.'),
    cfg.IntOpt('ibmpowervm_optical_cleanup_interval',
               default=1800,
               help='Interval to check for virtual optical needing removal '
               'post-deploy.'),
    cfg.IntOpt('inventory_host_interval', default=300,
               help='Interval to Refresh the Inventory for the Host'),
    cfg.IntOpt('quick_inventory_host_interval', default=60,
               help='Interval to do a quick check of host\'s VIOSes\' status'),
    cfg.IntOpt('inventory_instances_interval', default=120,
               help='Interval to Refresh the Inventory for Instances'),
    cfg.IntOpt('monitor_instances_interval', default=300,
               help='Interval to Refresh the Metrics for Instances'),
    cfg.IntOpt('metrics_message_size', default=20,
               help='The size limit of a compute.instance.metrics message. '
                    'For each message, the number of instances that is '
                    'reported.  For example if there are 40 instances on a '
                    'host, there will be 2 messages sent each time the '
                    'utilization data is collected (40 / 20 = 2)'),
    cfg.StrOpt('host_display_name', default=None,
               help='The display name set by the user for the Host'),
    cfg.IntOpt('volume_status_check_max_retries', default=60,
               help='volume create may take long time. Nova will keep'
                    'retry until either the volume is available or it'
                    'times out'),
    cfg.IntOpt('cleanup_net_assn_interval', default=1200,
               help='Timeout (seconds) for cleanup network associations.'),
    cfg.StrOpt('boot_volume_name_prefix', default='boot-',
               help='Customize boot volume display name prefix'),
    cfg.BoolOpt('ibmpowervm_handle_out_of_band_migration', default=True,
                help='Handle out of band migration of instances'),
    cfg.BoolOpt('has_first_boot_occurred', default=False,
                help='Indicates if the agent has run through the first'
                     ' boot setup')
]

CONF = cfg.CONF
CONF.register_opts(ibmpowervm_interval_opts)
CONF.import_opt('powervm_mgr_type', 'paxes_nova.virt.ibmpowervm.ivm')
CONF.import_opt('powervm_mgr', 'paxes_nova.virt.ibmpowervm.ivm')
CONF.import_opt('host_storage_type', 'paxes_nova.virt.ibmpowervm.ivm')

get_notifier = functools.partial(rpc.get_notifier, service='compute')
wrap_exception = functools.partial(exception.wrap_exception,
                                   get_notifier=get_notifier)


class PowerVCComputeManager(manager.ComputeManager):
    """Extends the base Compute Manager class with PowerVC capabilities"""

    def __init__(self, compute_driver=None, *args, **kwargs):
        """Constructor for the PowerVC extension to the Compute Manager"""
        try:
            # BEGIN: TEMPORARY WORKAROUND FOR 4172
            _BACKEND_MAPPING = {'sqlalchemy': 'nova.db.sqlalchemy.api'}
            nova.db.api.IMPL = db_api.DBAPI(
                'sqlalchemy', backend_mapping=_BACKEND_MAPPING, lazy=True)
            # END: 4172

            super(PowerVCComputeManager, self).__init__(
                compute_driver='nova.virt.fake.FakeDriver', *args, **kwargs)
            # Since we need to add methods to the VirtAPI so the driver can
            # call other DB methods, we first construct a temporary driver so
            # that we can override the VirtAPI before we construct the
            # real driver
            self.compute_rpcapi = pvc_compute_rpcapi.PowerVCComputeRPCAPI()
            self.virtapi = PowerVCComputeVirtAPI(self)
            self.conductor_api = conductor.PowerVCConductorAPI()
            self.volume_api = volume.PowerVCVolumeAPI()
            self.driver = driver.\
                load_compute_driver(self.virtapi, compute_driver)
            self.instance_chunk_map = dict()
            self.instance_chunk_counter = 0

            # Monkey patches:
            netutils.monkey_patch_netutils_get_injected_network_template()

        except Exception as exc:
            LOG.exception(_('error in ComputeMgr init, ending nova-compute'))
            raise exc

    #######################################################
    ##########  Override Initialization Methods  ##########
    #######################################################
    def pre_start_hook(self, **kwargs):
        """Override the Pre-Start Hook to fix Base Compute Manager issues"""
        # Call the Parent pre-start hook to create the ComputeNode in the DB
        super(PowerVCComputeManager, self).pre_start_hook(**kwargs)
        # After the Compute Node is inserted in the DB, then Inventory the Host
        if isinstance(self.driver, discovery_driver.ComputeDiscoveryDriver):
            self.inventory_host(nova.context.get_admin_context())
            self.inventory_instances(nova.context.get_admin_context())

    def init_host(self):
        """Override the Init Host to fix Base Compute Manager issues"""
        try:
            super(PowerVCComputeManager, self).init_host()
            self.check_and_run_first_boot(nova.context.get_admin_context())
        except Exception:
            LOG.exception(_('ComputeMgr init_host error, ending nova-compute'))
            raise

    def _init_instance(self, context, instance):
        """Override Init-Instance method to fix Base Compute Manager issues"""
        try:
            super(PowerVCComputeManager,
                  self)._init_instance(context, instance)
        except Exception:
            LOG.exception(_('exception initializing the Instance, moving on'))

    def _destroy_evacuated_instances(self, context):
        """Override Base Compute Manager to update evacuated instances."""
        driver = self.driver
        try:
            LOG.debug('Update instead of delete evacuated instances.')
            # Get all LPARs on this host that are in Paxes DB
            drv_instances = self.driver.list_instances()
            db_instances = self._query_db_instances(context)
            # If the Driver implemented, ask it to handle Evacuated Instances
            if isinstance(driver, discovery_driver.ComputeDiscoveryDriver):
                driver.handle_evacuated_instances(
                    context, db_instances, drv_instances)
        # We don't want to take down the process just because cleanup failed
        except Exception:
            LOG.exception(_('exception in Evacuation Cleanup, moving on'))

    def _get_resource_tracker(self, nodename):
        """Override to return our own Resource Tracker implementation"""
        rt = self._resource_tracker_dict.get(nodename)
        if not rt:
            if nodename not in self.driver.get_available_nodes():
                msg = nodename + ' is not a valid node managed by the host.'
                raise exception.NovaException(msg)
            rt = PowerVCResourceTracker(self.host, self.driver, nodename)
            self._resource_tracker_dict[nodename] = rt
        return rt

    #######################################################
    ########  Compute-related Overrides/Extensions  #######
    #######################################################
    def check_and_run_first_boot(self, context):
        """
        Method to be run on first boot post registration
        """
        # check if first boot has already occurred, if yes return
        if CONF.has_first_boot_occurred:
            return
        #if not, check that driver is instance of ComputeDiscoveryDriver
        if isinstance(self.driver, discovery_driver.ComputeDiscoveryDriver):
            # run the post registration against the driver
            LOG.debug(_('Running post_registration against %s ' % self.driver))
            self.driver.post_registration(context)
            # run openstack-config to set has_first_boot_occurred as True
            LOG.debug('Running openstack-config to set '
                      'has_first_boot_occurred')
            utils.execute('/usr/bin/openstack-config', '--set',
                          '/etc/nova/nova.conf', 'DEFAULT',
                          'has_first_boot_occurred', True,
                          run_as_root=True, check_exit_code=[0])
            # update in-memory has_first_boot_occurred
            LOG.debug('Updating in-memory conf option has_first_boot_occurred')
            CONF.has_first_boot_occurred = True

    def end_compute_with_hmc(self, context, data):
        LOG.debug('Received end_compute_with_hmc cast %s' % data)
        hmc_ip = data['hmc_ip']
        if hmc_ip == CONF.powervm_mgr:
            # this is needed to trigger the health monitoring
            LOG.error(_('Received 401 unauthorized error from HMC %s')
                      % hmc_ip)
            os._exit(1)

    @send_ui_notification(notify_msg.STOP_ERROR,
                          notify_msg.STOP_SUCCESS,
                          log_exception=True)
    def stop_instance(self, context, instance):
        """Stopping an instance on this host."""
        # Instance must be passed as a keyword argument in order for
        # revert_task_state decorator to function properly
        super(PowerVCComputeManager, self).stop_instance(context,
                                                         instance=instance)

    @send_ui_notification(notify_msg.START_ERROR,
                          notify_msg.START_SUCCESS,
                          log_exception=True)
    def start_instance(self, context, instance):
        """Starting an instance on this host."""
        # Instance must be passed as a keyword argument in order for
        # revert_task_state decorator to function properly
        super(PowerVCComputeManager, self).start_instance(context,
                                                          instance=instance)

    @send_ui_notification(notify_msg.DEPLOY_ERROR, notify_msg.DEPLOY_SUCCESS,
                          log_exception=True)
    def _spawn(self, context, instance, image_meta, network_info,
               block_device_info, injected_files, admin_password,
               set_access_ip=False):
        """Spawn an instance with error logging and update its power state.
           Override to send notification.
        """
        ret_instance = super(PowerVCComputeManager, self)._spawn(
            context, instance, image_meta, network_info, block_device_info,
            injected_files, admin_password, set_access_ip=set_access_ip)

        if not self._is_managing_PowerKVM():
            # For PowerVM, set task states differently
            # ISO deploys will go to stopped with no task state
            if image_meta['disk_format'] == 'iso':
                ret_instance.vm_state = vm_states.STOPPED
            else:
                # Non-ISO PowerVM deploys go to the ACTIVATING task state
                ret_instance.vm_state = vm_states.ACTIVE
                ret_instance.task_state = ACTIVATING
            ret_instance.save()

        return ret_instance

    def handle_lifecycle_event(self, event):
        '''
        Lifecycle events are only applicable to PowerKVM. We override the
        state of the VM in the case of SUSPEND so we need to call get_info()
        to get the real state rather than relying on the event data.
        '''
        context = nova.context.get_admin_context()
        instance = instance_obj.Instance.get_by_uuid(
            context, event.get_instance_uuid())

        inst_info = self.driver.get_info(instance)
        vm_power_state = inst_info['state']

        LOG.info(_("Life cycle event %(state)d on VM %(uuid)s. State is"
                   " %(vm_state)s.") %
                 {'state': event.get_transition(),
                  'uuid': event.get_instance_uuid(),
                  'vm_state': vm_power_state})

        self._sync_instance_power_state(context,
                                        instance,
                                        vm_power_state)

    def _sync_instance_power_state(self, context, db_instance, vm_power_state,
                                   use_slave=False):
        """Align instance power state between the database and hypervisor.

        If the instance is not found on the hypervisor, but is in the database,
        then a stop() API will be called on the instance.
        """

        # We re-query the DB to get the latest instance info to minimize
        # (not eliminate) race condition.
        db_instance.refresh()
        db_power_state = db_instance.power_state
        vm_state = db_instance.vm_state

        if self.host != db_instance.host:
            # on the sending end of nova-compute _sync_power_state
            # may have yielded to the greenthread performing a live
            # migration; this in turn has changed the resident-host
            # for the VM; However, the instance is still active, it
            # is just in the process of migrating to another host.
            # This implies that the compute source must relinquish
            # control to the compute destination.
            LOG.info(_("During the sync_power process the "
                       "instance has moved from "
                       "host %(src)s to host %(dst)s") %
                     {'src': self.host,
                      'dst': db_instance.host},
                     instance=db_instance)
            return
        elif db_instance.task_state is not None:
            # on the receiving end of nova-compute, it could happen
            # that the DB instance already report the new resident
            # but the actual VM has not showed up on the hypervisor
            # yet. In this case, let's allow the loop to continue
            # and run the state sync in a later round
            LOG.info(_("During sync_power_state the instance has a "
                       "pending task. Skip."), instance=db_instance)
            return

        if vm_power_state != db_power_state:
            # power_state is always updated from hypervisor to db
            db_instance.power_state = vm_power_state
            db_instance.save()
            db_power_state = vm_power_state

        # Note(maoy): Now resolve the discrepancy between vm_state and
        # vm_power_state. We go through all possible vm_states.
        if vm_state in (vm_states.BUILDING,
                        vm_states.RESCUED,
                        vm_states.RESIZED,
                        vm_states.ERROR):
            # TODO(maoy): we ignore these vm_state for now.
            pass
        elif vm_state == vm_states.ACTIVE:
            # The only rational power state should be RUNNING
            if vm_power_state in (power_state.SHUTDOWN,
                                  power_state.CRASHED):
                LOG.warn(_("Instance shutdown by itself. Calling "
                           "the stop API."), instance=db_instance)
                try:
                    # Note(maoy): here we call the API instead of
                    # brutally updating the vm_state in the database
                    # to allow all the hooks and checks to be performed.
                    self.compute_api.stop(context, db_instance)
                except Exception:
                    # Note(maoy): there is no need to propagate the error
                    # because the same power_state will be retrieved next
                    # time and retried.
                    # For example, there might be another task scheduled.
                    LOG.exception(_("error during stop for shutdown/crashed"),
                                  instance=db_instance)
            elif vm_power_state == power_state.SUSPENDED:
                # Instance is suspended. Setting state of
                # instance to suspended in DB accordingly.
                self.set_vm_state(db_instance, vm_power_state,
                                  vm_states.SUSPENDED)
            elif vm_power_state == power_state.PAUSED:
                # Instance is paused. Setting state of
                # instance to paused in DB accordingly.
                self.set_vm_state(db_instance, vm_power_state,
                                  vm_states.PAUSED)
            elif vm_power_state == power_state.NOSTATE:
                # Occasionally, depending on the status of the hypervisor,
                # which could be restarting for example, an instance may
                # not be found.  Therefore just log the condidtion.
                LOG.warn(_("Instance is unexpectedly not found. Ignore."),
                         instance=db_instance)
        elif vm_state in (vm_states.STOPPED,
                          vm_states.PAUSED,
                          vm_states.SUSPENDED):
            if vm_power_state == power_state.RUNNING:
                # Instance is running. Setting state of
                # instance to active in DB accordingly.
                self.set_vm_state(db_instance, vm_power_state,
                                  vm_states.ACTIVE)
            elif vm_power_state == power_state.SHUTDOWN:
                if vm_state != vm_states.STOPPED:
                    # Instance is stopped. Setting
                    # state of instance to stopped in DB.
                    self.set_vm_state(db_instance, vm_power_state,
                                      vm_states.STOPPED)
            elif vm_power_state == power_state.SUSPENDED:
                if vm_state != vm_states.SUSPENDED:
                    # If an instance is suspended out of band, its vm_state
                    # could become 'stopped' or 'paused' first. Sync up
                    # vm_state with power_state here.
                    self.set_vm_state(db_instance, vm_power_state,
                                      vm_states.SUSPENDED)
            else:
                # Do nothing if instance is in following state
                # as reported by hypervisor
                # NOSTATE
                # CRASHED
                # BUILDING
                LOG.warn(_("Instance state is %(s1)s as reported by "
                           "hypervisor, but its state in PowerVC is "
                           "%(s2)s. Ignore.")
                         % {'s1': vm_power_state, 's2': vm_state})
        elif vm_state in (vm_states.SOFT_DELETED,
                          vm_states.DELETED):
            if vm_power_state not in (power_state.NOSTATE,
                                      power_state.SHUTDOWN):
                # Note(maoy): this should be taken care of periodically in
                # _cleanup_running_deleted_instances().
                LOG.warn(_("Instance is not (soft-)deleted."),
                         instance=db_instance)

    def set_vm_state(self, db_instance, vm_power_state, new_state):
        """
        Set the vm_state of an instance to new_state
        """
        LOG.warn(_('Current instance state of %(name)s is %(old_state)s. '
                   'Instance power state is %(power_state)s. '
                   'Setting instance state to %(new_state)s '
                   'in database.') %
                 {'name': db_instance['display_name'],
                  'old_state': db_instance['vm_state'],
                  'power_state': vm_power_state, 'new_state': new_state})
        db_instance.vm_state = new_state
        db_instance.save()

    def _error_notify(self, instance, context, severity, details):
        info = {'msg': _('{message} Virtual Server: instance UUID:'
                '{instance_id}, instance name: {instance_name} '
                'on host: {host_name}'),
                'message': details,
                'instance_id': instance['uuid'],
                'instance_name': instance['display_name'],
                'host_name': self.host}
        notifier = rpc.get_notifier(service='compute', host=self.host)
        if severity == 'error':
            notifier.error(context, 'compute.instance.log', info)
        elif severity == 'info':
            notifier.info(context, 'compute.instance.log', info)
        elif severity == 'critical':
            notifier.critical(context, 'compute.instance.log', info)
        elif severity == 'warn':
            notifier.warn(context, 'compute.instance.log', info)
        elif severity == 'debug':
            notifier.debug(context, 'compute.instance.log', info)

    def _query_db_instances(self, context, filter_instances=None):
        """Helper method to filter out the subset of the DB Instances"""
        filtered_instances = list()
        context = context.elevated()
        instfact = compute_dom.ManagedInstanceFactory.get_factory()
        # Query all of the Instance for the Host from the Database
        db_instances = instfact.find_all_instances_by_host(context, self.host)
        # If they didn't provide any Filter criteria, just return all
        if not filter_instances:
            return db_instances
        # Build a Map of UUID's for the Instances, for quicker lookup
        filter_map = dict([(instance['uuid'],
                            instance) for instance in filter_instances])
        # Loop through each of the DB Instances, seeing if there is a match
        for db_instance in db_instances:
            if db_instance['uuid'] in filter_map:
                filtered_instances.append(db_instance)
        return filtered_instances

    def _populate_admin_context(self, context):
        """Helper method to populate the Token/ServiceCatalog on the Context"""
        #We don't need to do anything if the Token/ServiceCatalog are populated
        if context.auth_token and context.service_catalog:
            return
        try:
            #We are using the Neutron Client since they having Caching logic
            nclient = neutronv2.get_client(context).httpclient
            #Since the Neutron Client is cached, the token may already be
            #populated, so only need to authenticate if it isn't set yet
            if nclient.auth_token is None:
                nclient.authenticate()
            context.auth_token = nclient.auth_token
            #change for paxes
            context.service_catalog = \
                nclient.service_catalog.catalog['serviceCatalog']
#             context.service_catalog = \
#                 nclient.service_catalog.catalog['access']['serviceCatalog']
        except Exception as exc:
            LOG.warn(_('Error trying to populate Token/Catalog on the '
                       'Context'))
            LOG.exception(exc)

    #######################################################
    #######  Discovery-related Overrides/Extensions  ######
    #######################################################
    def discover_instances(self, context):
        """Returns a list of all of the VM's that exist on the Host"""
        driver = self.driver
        # Currently we won't throw an exception if it isn't a Discover Driver
        if not isinstance(driver, discovery_driver.ComputeDiscoveryDriver):
            drvclass = driver.__class__.__name__
            LOG.warn(_('Driver %s does not implement Discover Driver')
                     % drvclass)
            return {'identifier': None, 'servers': [], 'chunking': False}
        # Call the Compute Discovery Driver to get a list of existing VM's
        all_instances = self._discover_instances(context)
        # We need to modify the Instances returned from the Driver slightly
        self._manipulate_driver_instances(all_instances, False)
        # Break up the list of VM's to a set of Chunks to be returned
        identifier = self._persist_instances_chunks(all_instances)
        # Return the first chunk of persisted volumes to the caller
        return self.get_next_instances_chunk(context, identifier)

    def query_instances(self, context, instance_ids, allow_unsupported=False):
        """Returns details about the VM's that were requested"""
        return_all = '*all' in instance_ids
        match_instances, query_instances = (list(), list())
        # First we need to figure out what VM's really exist on the Host
        all_instances = self._discover_instances(context)
        uuid_map = dict([(inst_id, '') for inst_id in instance_ids])
        # Loop through each VM's on the Host, seeing if we should query
        for instance in all_instances:
            # See if they requested all instances or this specific one
            if return_all or instance['uuid'] in uuid_map:
                support = instance.get('support', {})
                supported = support.get('status', 'supported')
                # If this is managed, then no need to do a query, just return
                if instance.get('managed') is True:
                    match_instances.append(instance)
                # If this isn't supported and no override, just return it
                elif not allow_unsupported and supported != 'supported':
                    match_instances.append(instance)
                # Otherwise it is supported/un-managed and a match so query
                else:
                    instance.pop('support', instance.pop('managed', None))
                    query_instances.append(instance)
        # Only worth calling the Driver if some VM's exist on the System
        if len(query_instances) > 0:
            instances = self.driver.query_instances(context, query_instances)
            match_instances.extend(instances)
        # We need to modify the Instances returned from the Driver slightly
        self._manipulate_driver_instances(match_instances, True, all_instances)
        # Break up the list of VM's to a set of Chunks to be returned
        identifier = self._persist_instances_chunks(match_instances)
        # Return the first chunk of persisted VM's to the caller
        return self.get_next_instances_chunk(context, identifier)

    @periodic_task.periodic_task(spacing=CONF.inventory_instances_interval)
    def inventory_instances(self, context, instances=None):
        """Periodic Task to Refresh the Inventory for the Instances"""
        driver, context = (self.driver, context.elevated())
        LOG.debug('Invoking Method to Refresh Instance Inventory - Begin')
        try:
            drv_insts = self.driver.list_instances()
            name_map = dict([(inst, '') for inst in drv_insts])
            # Retrieve All of the Instances from the Nova DB for the Given Host
            db_insts = self._query_db_instances(context, instances)
            db_insts = [inst for inst in db_insts if inst['name'] in name_map]
            # If this is a Discovery Driver, then let it Gather Inventory
            if isinstance(driver, discovery_driver.ComputeDiscoveryDriver):
                # We will also allow the Driver to handle Evacuated Instances
                driver.handle_evacuated_instances(context, db_insts, drv_insts)
                # Now we can actually collect Inventory on the Instances
                driver.inventory_instances(context, db_insts)
        except Exception as exc:
            LOG.warn(_('Error refreshing Instance Inventory'))
            LOG.exception(exc)
        LOG.debug('Invoking Method to Refresh Instance Inventory - End')

    def verify_host_running(self, context):
        """Verifies the nova-compute service for the Host is running"""
        return True

    def ssh_trust(self, context, public_key):
        return self.driver.ssh_trust(public_key)
    
    @logcall
    @periodic_task.periodic_task(spacing=CONF.inventory_host_interval)
    @lockutils.synchronized('powervc-inventory', 'host-inventory-')
    def inventory_host(self, context):
        """Periodic Task to Refresh the Inventory for the Host"""
        driver, context = (self.driver, context.elevated())
        #The Periodic Task Context may not have the Token/ServiceCatalog
        self._populate_admin_context(context)
        try:
            # If this is a Discovery Driver, then let it Gather Inventory
            if isinstance(driver, discovery_driver.ComputeDiscoveryDriver):
                driver.inventory_host(context)
        except Exception as exc:
            LOG.warn(_('Error refreshing Host Inventory'))
            LOG.exception(exc)

    @logcall
    @periodic_task.periodic_task(spacing=CONF.quick_inventory_host_interval)
    @lockutils.synchronized('powervc-inventory', 'host-inventory-')
    def quick_inventory_host(self, context):
        '''
        Periodic Task to do a quick check of whether a full inventory is needed
        or not.  If so, it will initiate the full inventory.
        '''
        driver, context = (self.driver, context.elevated())

        #The Periodic Task Context may not have the Token/ServiceCatalog
        self._populate_admin_context(context)
        try:
            # If this is a Discovery Driver, then let it Gather Inventory
            if isinstance(driver, discovery_driver.ComputeDiscoveryDriver):
                if driver.is_inventory_needed(context):
                    driver.inventory_host(context)
        except Exception as exc:
            LOG.exception(exc)

    def get_host_ovs(self, context):
        LOG.debug('Invoking get host ovs data - Begin')
        retVal = None
        try:
            if hasattr(self.driver, 'get_host_ovs'):
                LOG.debug('Retrieving host ovs data using Driver...')
                retVal = self.driver.get_host_ovs(context)
        except Exception as exc:
            """
            NOTE: Since this is the interface boundary between the agent
            and server we will not raise the exception and will just
            log it on the host side
            """
            LOG.exception(exc)

            error_obj = dom_kvm.Error("%s" % exc)
            ovs_dom = dom_kvm.HostOVSNetworkConfig(CONF.get("host"))

            if retVal is None:
                ovs_dom.error_list = [error_obj]

            else:
                ovs_dom.from_dict(retVal)
                ovs_dom.error_list.append(error_obj)

            retVal = ovs_dom.to_dict()

        LOG.debug('Invoking get host ovs data - End')
        return retVal

    def update_host_ovs(self, context, dom, force_flag, rollback):
        LOG.debug('Invoking update host ovs data - Begin')
        retVal = {}
        try:
            if hasattr(self.driver, 'update_host_ovs'):
                LOG.debug('Updating host ovs data using Driver...')
                retVal = self.driver.update_host_ovs(context,
                                                     dom,
                                                     force_flag,
                                                     rollback)
        except Exception as exc:
            """
            NOTE: Since this is the interface boundary between the agent
            and server we will not raise the exception and will just
            log it on the host side
            """
            LOG.error(exc)

            if agent.ERRORS_KEY not in retVal:
                retVal[agent.ERRORS_KEY] = [{'message': '%s' % exc}]
            else:
                retVal[agent.ERRORS_KEY].append({'message': '%s' % exc})

        LOG.debug('Updating get host ovs data - End')
        return retVal

    def unmanage_host(self, context):
        """Allows the Driver to do any necessary cleanup on Host Removal"""
        # We only want to call unmanage on the Driver if it implements it
        if isinstance(self.driver, discovery_driver.ComputeDiscoveryDriver):
            LOG.info(_('Unmanaging Host, allowing Driver to do cleanup...'))
            self.driver.unmanage_host(context)

    @lockutils.synchronized('instance_chunking', 'nova-')
    def get_next_instances_chunk(self, context, identifier):
        """Provides Chunking of VM's Lists to avoid QPID Limits"""
        instance_chunks = self.instance_chunk_map.get(identifier)
        # If the Identifier doesn't exist, we will just return an empty list
        if instance_chunks is None:
            return dict(identifier=identifier, servers=[], chunking=False)
        # If this is the last chunk (or no chunking), just return that list
        if len(instance_chunks) == 1:
            self.instance_chunk_map.pop(identifier, None)
            return dict(identifier=identifier,
                        servers=instance_chunks[0], chunking=False)
        # Otherwise return the first chunk and say that there are more left
        self.instance_chunk_map[identifier] = instance_chunks[1:]
        return dict(identifier=identifier,
                    servers=instance_chunks[0], chunking=True)

    def _discover_instances(self, context):
        """Internal Method to list of all of the VM's that exist on the Host"""
        # Call the Compute Discovery Driver to get a list of existing VM's
        drv_instances = self.driver.discover_instances(context)
        # Generate the UUID's for the VM's and determine which are Managed
        self._generate_instance_uuids(context, drv_instances)
        return drv_instances

    @lockutils.synchronized('instance_chunking', 'nova-')
    def _persist_instances_chunks(self, instances):
        """Internal Helper method to generate the UUID's for the VM's"""
        current_len = 0
        current_list, instance_chunks = (list(), list())
        # First get an identifier to be used to reference this chunking
        identifier = str(self.instance_chunk_counter)
        self.instance_chunk_counter = self.instance_chunk_counter + 1
        # Loop through each instance, breaking it up into chunks based on size
        while len(instances) > 0:
            instance = instances.pop(0)
            instance_len = len(str(instance))
            # If we could possibly go over the 64K Message size, break up
            if len(current_list) > 0 and (current_len + instance_len) > 40000:
                instance_chunks.append(current_list)
                current_list = []
                current_len = 0
            # Add this instance to the current chunk that is being returned
            current_len = current_len + instance_len
            current_list.append(instance)
        # Add the final chunk to the overall set of chunks for the instance
        # list
        instance_chunks.append(current_list)
        self.instance_chunk_map[identifier] = instance_chunks
        return identifier

    def _manipulate_driver_instances(self, drv_instances, query, discinsts=[]):
        """Internal Helper method to modify attributes on the Instances"""
        state_map = {'1': 'active', '3': 'paused', '4': 'stopped',
                     '6': 'error', '7': 'suspended', '9': 'building'}
        name_map = dict([(inst['name'], inst) for inst in discinsts])
        # Loop through each of the Driver Instances, modifying the data
        for inst in drv_instances:
            # Get some things off the discover call if query didn't return them
            disc_inst = name_map.get(inst['name'], {})
            state = inst.pop('state', disc_inst.get('state', 0))
            inst['id'] = inst.pop('uuid', disc_inst.get('uuid'))
            # If this is the result of a Driver Query Instances call
            if query:
                inst['power_state'] = state
                inst['vm_state'] = state_map.get(str(state), 'error')
                inst['memory_mb'] = int(inst.get('memory_mb', 0))
                inst['root_gb'] = int(inst.get('root_gb', 0))
                inst['ephemeral_gb'] = int(inst.get('ephemeral_gb', 0))
                vcpus = str(inst.get('vcpus', 0))
                if vcpus.find('.') > 0:
                    vcpus = vcpus[:vcpus.find('.')]
                inst['vcpus'] = int(vcpus)
            # If this is the result of a Driver Discover Instances call
            else:
                inst['status'] = state_map.get(str(state), 'error')
                inst.pop('power_specs', None)
            # This is a temporary work-around until the OSEE 167498 defect is
            # fixed, where the _('yyy') translated messages are lazy-loaded
            # now, so they get returned as Message objects until they are
            # explicitly converted to strings, and there is an RPC issue in
            # trying to serialize these objects.  So we will explicitly convert
            # the non-supported reason messages to strings to work around now.
            if inst.get('support') is not None:
                if inst['support'].get('reasons') is not None:
                    inst['support']['reasons'] = \
                        [_('%s') % reason for reason in
                         inst['support']['reasons']]

    def _generate_instance_uuids(self, context, drv_instances):
        """Internal Helper method to generate the UUID's for the VM's"""
        name_map, uuid_map = (dict(), dict())
        vm_name_map, vm_uuid_map = (dict(), dict())
        # Query the Database to get the Instance info for this Host
        db_instances = self._query_db_instances(context)
        # Loop through each of the Instances, creating the Maps for Lookup
        for db_instance in db_instances:
            power_specs = db_instance.get('power_specs', {})
            vm_uuid = power_specs.get('vm_uuid')
            vm_name = power_specs.get('vm_name')
            vm_uuid = '!' if vm_uuid is None else vm_uuid
            vm_name = '!' if vm_name is None else vm_name
            uuid_map[db_instance['uuid']] = db_instance
            name_map[db_instance.get('name', '!')] = db_instance
            vm_uuid_map[vm_uuid.lower()] = db_instance
            vm_name_map[vm_name] = db_instance
        # Cleanup any default invalid entries we added to the Maps
        name_map.pop('!', None)
        vm_name_map.pop('!', None)
        vm_uuid_map.pop('!', None)
        # Loop through the instances generating UUID's for any that need one
        for drv_vm in drv_instances:
            managed = False
            db_instance = None
            power_specs = drv_vm.get('power_specs', {})
            vm_uuid = power_specs.get('vm_uuid')
            vm_name = power_specs.get('vm_name')
            # See if there was a match on the Name with what is in the DB
            if vm_name is not None:
                db_instance = name_map.get(vm_name)
                if db_instance is None:
                    db_instance = vm_name_map.get(vm_name)
            # See if there was a match on the UUID with what is in the DB
            if db_instance is None and vm_uuid is not None:
                db_instance = uuid_map.get(vm_uuid.lower())
                if db_instance is None:
                    db_instance = vm_uuid_map.get(vm_uuid.lower())
            # If the UUID isn't set, do a secondary lookup to see if it exists
            if db_instance is not None:
                drv_vm['uuid'] = db_instance['uuid']
                drv_vm['name'] = db_instance['display_name']
                managed = True
            # Otherwise it isn't managed, so we need to generate one
            else:
                namesp = uuid.UUID('a0dd4880-6115-39d6-b26b-77df18fe749f')
                namestr = self._get_unique_instance_str(drv_vm)
                drv_vm['uuid'] = str(uuid.uuid3(namesp, namestr))
            # Set the Managed attribute based on what we determined earlier
            drv_vm['managed'] = managed

    def _get_unique_instance_str(self, instance):
        """Internal Helper method to create a unique string for the instance"""
        instance_name = instance.get('name', '')
        instance_id = instance.get('hostuuid', '')
        return "ibm-powervc://server/host='%s',id='%s',name='%s'" % \
            (self.host, str(instance_name), str(instance_id))

    #######################################################
    #########  Image-related Overrides/Extensions  ########
    #######################################################
    @periodic_task.periodic_task(spacing=600)
    def _check_instance_activating_time(self, context):
        """Ensure that instances are not stuck in activating."""
        timeout = CONF.ibmpowervm_activating_timeout
        if timeout == 0:
            return
        filters = {'task_state': ACTIVATING, 'host': self.host}
        activating_insts = self.conductor_api.instance_get_all_by_filters(
            context, filters, columns_to_join=[])
        # Loop through all the Activating Instance to do the time outs
        for instance in activating_insts:
            if timeutils.is_older_than(instance['created_at'], timeout):
                self._instance_update(context, instance['uuid'],
                                      task_state=None)
                LOG.warn(_("Instance activating timed out. "
                           "Resetting task state."), instance=instance)

    @periodic_task.periodic_task(
        spacing=CONF.ibmpowervm_optical_cleanup_interval)
    def _check_instance_vopt_removal(self, context):
        """Clean up virtual optical devices after 1 hour."""
        if CONF.powervm_mgr_type.lower() == 'hmc':
            # only done for HMC
            timeout = CONF.ibmpowervm_iso_detach_timeout
            if timeout < 0:
                return

            insts = self.conductor_api.instance_get_all_by_host(
                context, self.host, columns_to_join=[])
            # Loop through all the Instances to find ones older than 1 hour
            old_instances = []
            for instance in insts:
                if timeutils.is_older_than(instance['created_at'],
                                           timeout):
                    old_instances.append(instance['uuid'])
            self.driver.vopt_cleanup(old_instances)

    def _prepare_volume_by_image(self, context, instance, source_volid=None,
                                 volume_size=None, volume_type=None,
                                 image_id=None, display_name=None,
                                 bootable=True):
        """
        Create volume based on image volume or create an empty volume
        based on volume size.  Handles PowerVC image meta and build BDM
        accordingly before spawn.
        :param context: operation context
        :param instance: instance that is building.
        :param source_volid: image volume id
        :param volume_size: volume size to create an empty volume
        :param volume_type: optional volume type for volume creation. Will get
                            overridden if volume type defined in flavor
        :param image_id: image uuid
        :param display_name: display name of boot volume
        :return: volume that has been created.
        """
        # source_vol and volume_size are mutually exclusive
        if source_volid and volume_size > 0:
            msg = (_('_prepare_volume_by_image(): Invalid input. '
                     'source_vol=%(source_vol)s volume_size=%(volume_size)s'
                     'volume_type=%(volume_type)s'))
            LOG.error(msg)
            raise exception.InvalidInput(reason=msg)

        name = display_name if (display_name and
                                isinstance(display_name, basestring)) else ''
        if bootable:
            # For bootable volume, save some details in the volume meta data
            vol_meta = {'is_boot_volume': "True",
                        'instance_uuid': instance['uuid']}
        else:
            vol_meta = None
        # get the storage template from the flavor.
        voltype_flavor = self._get_volume_type_from_flavor(context, instance)

        if source_volid:
            src_vol = self.volume_api.get(context, source_volid)
            if (not src_vol.get('id') or src_vol['id'] != source_volid):
                # The image volume doesn't exist.
                exc_reason = (_("image volume doesn't exist: "
                                "%(source_volid)s") % locals())
                exc_args = {'image_id': image_id,
                            'reason': exc_reason}
                LOG.error(exc_reason)
                raise exception.ImageUnacceptable(**exc_args)

            # input volume_type can overwrite image volume volume_type
            voltype = src_vol['volume_type']
            # volume_api.get() returns string "None" instead of type None
            if voltype and voltype.lower() != "none":
                src_voltype = voltype
            else:
                src_voltype = None

            # The volume type specified in the volume_mapping can
            # overwrite the volume type associated with the image volume
            if voltype_flavor:
                vol_type = voltype_flavor
            elif volume_type:
                vol_type = volume_type
            else:
                vol_type = src_voltype

            if vol_type is None:
                vol_type = CONF.host_storage_type
            # create boot volume from image volume.
            # The volume_api is PowerVCVolumeAPI which subclassed
            # from nova volume api class since its create() doesn't
            # take source_vol parameter for volume clone.
            try:
                vol = self.volume_api.create(context, src_vol['size'],
                                             name, '', source_vol=src_vol,
                                             volume_type=vol_type,
                                             metadata=vol_meta)
                details = (_("Started clone boot volume %(volid)s "
                             "from image volume %(srcvolid)s with "
                             "volume type \"%(voltype)s\" for instance "
                             "%(inst)s") %
                           {'volid': vol['id'],
                            'srcvolid': src_vol['id'],
                            'voltype': vol_type,
                            'inst': instance['uuid']})
                LOG.info(details)

            except Exception:
                with excutils.save_and_reraise_exception() as ex:
                    error = ex.value
                    message = (_("Could not clone boot volume "
                                 " from image volume %(volid)s "
                                 "during deploy instance: %(inst)s "
                                 "Error: %(error)s") %
                               {'volid': src_vol['id'],
                                'inst': instance['uuid'],
                                'error': error})
                    LOG.error(message)
        elif volume_size > 0:
            # volume type is default from the image's metadata. It could
            # be overwritten by the volume type in flavor.
            vol_type = None
            if voltype_flavor:
                vol_type = voltype_flavor
            elif volume_type:
                vol_type = volume_type
            if not vol_type:
                vol_type = CONF.host_storage_type
            # create an empty volume based on the given size
            try:
                # The volume_type here is passed in by caller.
                # for IVM iso deploy, _prepare_iso_deploy_boot_volume
                # gets it from flavor.
                vol = self.volume_api.create(context, volume_size,
                                             name, '', volume_type=vol_type,
                                             metadata=vol_meta)
                details = (_("Started create volume %(volid)s "
                             "with size: %(size)s GB and "
                             "volume type \"%(voltype)s\" for instance "
                             "%(inst)s") %
                           {'volid': vol['id'],
                            'size': volume_size,
                            'voltype': vol_type,
                            'inst': instance['uuid']})
                LOG.info(details)

            except Exception:
                with excutils.save_and_reraise_exception() as ex:
                    error = ex.value
                    details = (_("Could not create %(size)sGB boot volume "
                                 "during deploy instance %(inst)s"
                                 "Error: %(error)s" %
                                 {'size': volume_size,
                                  'inst': instance['uuid'],
                                  'error': error}))
                    LOG.error(details)
        # check the volume status. If in "error" state stop the
        # the build process.
        volume = self.volume_api.get(context, vol['id'])
        # check newly cloned boot volume status.Bail if in error state.
        if (not volume.get('id') or volume['id'] != vol['id'] or
                'error' in volume['status']):
            error = (_("Health status: %(hstatus)s.") %
                     {'hstatus': volume.get('health_status')})

            reason = (_("Instance %(inst)s boot volume %(volid)s went into "
                        "error state during deploy. Error: %(error)s.") %
                      {'inst': instance['uuid'],
                       'volid': volume['id'],
                       'error': error})
            LOG.error(reason)
            exc_args = {'instname': instance['display_name'],
                        'instuuid': instance['uuid'],
                        'srcvolid': src_vol['id'],
                        'srcvolname': src_vol['display_name'],
                        'volmeta':
                        volume['volume_metadata'].get(
                            'Create Failure description')}

            raise pwrvc_excep.IBMPowerVCBootVolumeCloneFailure(**exc_args)

        message = (_("Instance %(inst)s boot volume %(volid)s create request "
                     "has been submitted to storage successfully. Current "
                     "volume status: %(status)s") %
                   {'inst': instance['uuid'],
                    'volid': volume['id'],
                    'status': volume['status']})
        LOG.info(message)
        return volume

    def _process_powervc_image_meta(self, context, instance, image_meta):
        """
        process new PowerVC image meta data , meta_version >=1.2

        :param context: operation context
        :param instance: compute instance
        :param image_meta: glance image meta data
        :return: None
        """

        LOG.debug("Enter _process_powervc_image_meta(): "
                  "image_meta=%(image_meta)s" % locals())
        # clean up any left over bdms for the instance
        bdms = blkdev_obj.BlockDeviceMappingList.get_by_instance_uuid(
            context, instance['uuid'])
        # clean up any existing volume BDMs for this instrance during deploy.
        for bdm in bdms:
            if bdm.is_volume:
                bdm.destroy(context)

        # digest image meta data
        image_prop = image_meta['properties']
        volume_mappings_str = image_prop.get('volume_mapping')
        if volume_mappings_str and isinstance(volume_mappings_str, basestring):
            volume_mappings = jsonutils.loads(volume_mappings_str)

        root_dev_name = image_prop.get(IMAGE_BOOT_DEVICE_PROPERTY)
        # set the default root_device_name if it is missing from
        # image metadata.
        if not root_dev_name:
            root_dev_name = '/dev/sda'

        # build the boot volume name.

        # build block_device_mapping for instance based on meta data
        for vol_map in volume_mappings:
            # Paxes supports only one boot volume. The image volume_mapping
            # for the boot volume needs to specify device_name='/dev/sda'.
            device_name = vol_map.get('device_name')
            voldispname = vol_map.get('display_name')
            bootvolname = self._generate_boot_volume_name(
                instance,
                display_name=voldispname)

            values = {'instance_uuid': instance['uuid'],
                      'device_name': device_name,
                      'snapshot_id': None,
                      'volume_id': None,
                      'volume_size': vol_map.get('volume_size'),
                      'no_device': None,
                      'virtual_name': None,
                      'connection_info': None,
                      'image_id': image_meta['id'],
                      'delete_on_termination': False}

            if root_dev_name == device_name:
                values['delete_on_termination'] = True

            # Check the delete_on_termination flag in the
            # volume_mapping to overwrite the calculated delete_on_termination
            # flag.
            destroy_vol = vol_map.get('delete_on_termination')
            if destroy_vol and isinstance(destroy_vol, bool):
                values['delete_on_termination'] = destroy_vol

            # create volume from image volume
            if vol_map.get('source_volid'):
                volume = self._prepare_volume_by_image(
                    context, instance, source_volid=vol_map['source_volid'],
                    display_name=bootvolname,
                    volume_type=vol_map.get('volume_type'),
                    image_id=image_meta['id'],
                    bootable=True
                )
            elif vol_map.get('volume_size'):
                # create an empty volume
                volume = self._prepare_volume_by_image(
                    context, instance, volume_size=vol_map['volume_size'],
                    display_name=bootvolname,
                    volume_type=vol_map.get('volume_type'),
                    image_id=image_meta['id'],
                    bootable=False
                )
            elif vol_map.get('volume_id'):
                # Paxes doesn't support boot directly from volume.
                # Boot volume will always clone from source_vol.
                # But image volume_mapping can specify volume to
                # attach during boot.  The additional volume will not
                # be deleted by default when the VM is deleted, unless
                # overridden by 'delete_on_termination' in volume_mapping.
                voltmp = self.volume_api.get(context, vol_map['volume_id'])
                if voltmp and voltmp['id'] == vol_map['volume_id']:
                    if voltmp['status'] != 'available':
                        LOG.warn(_("Failed to attach volume: %(voltmp)s"
                                   " during boot since it is not in the"
                                   "available state.") % dict(voltmp=voltmp))
                        # no necessary to fail the boot. Just ignore the
                        # volume and move on.
                        continue
                    volume = copy.deepcopy(voltmp)

            # The boot volume create request has been submitted to
            # cinder. It may not be done yet. The volume state
            # will be checked before attach during driver.spawn().
            LOG.debug('boot image volume: %(volume)s' % locals())
            values['volume_id'] = volume['id']
            values['volume_size'] = volume['size']

            # if the volume is boot disk. update the default_ephemeral_device
            # and capacity_gb field in the instance.
            if (root_dev_name and root_dev_name == values.get('device_name')):
                args = {'default_ephemeral_device': volume['id'],
                        'root_gb': volume['size']}
                self.conductor_api.instance_update(context, instance['uuid'],
                                                   **args)
                # update the memory objects too.
                instance.update(args)
            # update block device mapping in BDM table
            self.conductor_api.block_device_mapping_update_or_create(context,
                                                                     values)

    def _get_volume_type_from_flavor(self, context, instance):
        """
        Return powervm:boot_volume_type from flavor for boot volume
        storage template. The boot_volume_type is the volume type
        name that returns from volume types get REST API.
        The boot_volume_type passed in will be a volume type name.
        """
        instance_type = flavors.get_flavor(instance['instance_type_id'],
                                           context)
        boot_volume_type = None
        if instance_type:
            extra_specs = instance_type.get('extra_specs')
            if extra_specs:
                boot_volume_type = extra_specs.get("powervm:boot_volume_type")

        available_volume_types = self.volume_api.list_volume_types(context)

        # TODO: Need to consider default volume type when that is available.
        if not available_volume_types or not boot_volume_type:
            # no volume available on the system. Nothing we can do.
            return None
        else:
            for item in available_volume_types:
                if (item['id'] == boot_volume_type or
                        item['name'] == boot_volume_type):
                    # It is not clear whether boot_volume_type passed in is
                    # a volume type name or id. volume clone can only use
                    # volume type name. Do the translation here as needed.
                    LOG.debug("Instance %(inst)s boot volume type: %(voltype)s"
                              ", available volume type: %(voltypelist)s." %
                              {'inst': instance['uuid'],
                               'voltype': boot_volume_type,
                               'voltypelist': boot_volume_type})
                    return item['name']
        # The specified volume type is not defined on the system.
        return None

    def _generate_boot_volume_name(self, instance, display_name=None):
        """Generate boot volume name for instance. If image metadata
            supplies a customized display_name, it will override
            the generated boot volume name
        """
        prefix = CONF.boot_volume_name_prefix

        if not instance or display_name:
            display_name = ('' if not display_name or not
                            isinstance(display_name, basestring)
                            else display_name)
            return prefix + display_name

        if instance.get('host') and instance.get('name'):
            bootvolname = instance['host'] + "-" + instance['name']
        elif instance.get('uuid'):
            bootvolname = instance['uuid']
        else:
            bootvolname = ''
        return prefix + bootvolname

    def _prepare_iso_deploy_boot_volume(self, context, instance, image_meta):
        """
        This is helper function to create the boot volume
        for ISO deploy backed by cinder volume. For local disk support,
        the disk is not cinder backed and compute virt driver needs
        to create it. Need to get the volume type flavor as well.

        :param context: security context
        :param instance: Instance object
        :param image_meta: Glance image meta data for deploy
        :return: None
        """
        boot_device_size = instance['ephemeral_gb']
        if not boot_device_size or boot_device_size <= 0:
            msg = (_("ephemeral_gb %(ephemeral_gb)s for instance"
                     " %(instance_uuid)s is not valid") %
                   {'ephemeral_gb': boot_device_size,
                    'instance_uuid': instance['uuid']})
            raise exception.InvalidInput(reason=msg)

        bootvolname = self._generate_boot_volume_name(instance)

        # volume type will be handled by _prepare_volume_by_image to
        # retrieve it from flavor.
        boot_volume = self._prepare_volume_by_image(
            context, instance, volume_size=boot_device_size,
            display_name=bootvolname, image_id=image_meta['id'],
            bootable=True
        )

        LOG.debug('boot image volume: %(boot_volume)s' % locals())

        values = {'instance_uuid': instance['uuid'],
                  'device_name': '/dev/sda',
                  'snapshot_id': None,
                  'volume_id': None,
                  'volume_size': boot_volume['size'],
                  'no_device': None,
                  'virtual_name': None,
                  'connection_info': None,
                  'image_id': image_meta['id'],
                  'delete_on_termination': True,
                  'volume_id': boot_volume['id'],
                  }

        # if the volume is boot disk. update the default_ephemeral_device
        # and capacity_gb field in the instance.
        args = {'default_ephemeral_device': boot_volume['id'],
                'root_gb': boot_volume['size']}
        self.conductor_api.instance_update(context, instance['uuid'],
                                           **args)

        self.conductor_api.block_device_mapping_update_or_create(context,
                                                                 values)

    def _is_ivm_iso_cinder_deploy(self, image_meta):
        """
        Need to make sure the empty volume creation only happen
        on the IVM ISO deploy with cinder volume scenario.
        """

        if CONF.powervm_mgr_type.lower() == 'ivm':
            disk_format = image_meta.get("disk_format")
            if (disk_format and disk_format.lower() == "iso" and
                    CONF.host_storage_type.lower() == "san"):
                return True
            elif (disk_format and disk_format.lower() == "iso" and
                    CONF.host_storage_type.lower() == "iscsi"):
                return True

        return False

    def _prep_block_device(self, context, instance, bdms):
        """
        override nova compute manager's _prep_block_device
        in order to consume the new image meta data for powervc to
        clone a image volume as boot volume but doesn't attach the
        volume here. The image meta property volume_mapping
        will be used to build the BDM, clone the boot volume
        without attach the volume before spawn.
        :param context: operation context
        :param instance: compute instance
        :param bdms: The block device mapping based on image mapping
                     and block_device_mapping properties or the
                     block_device_mapping passed as parameter during
                     deploy.
        :return block_device_info:
        """
        if hasattr(self.driver, "get_hypervisor_type"):
            hypervisor_type = self.driver.get_hypervisor_type()
            # For PowerKVM, use openstack block device path directly
            if hypervisor_type and hypervisor_type == 'QEMU':
                s = super(PowerVCComputeManager, self)
                return s._prep_block_device(context, instance, bdms)

        # this is the path for Paxes Image meta data
        image_ref = instance['image_ref']
        if not image_ref:
            LOG.warn(_("ComputeMgr _setup_block_device_mapping() exception"
                       " since missing image_ref from instance"))
            raise exception.InvalidImageRef(image_href=image_ref)

        image_meta = self._get_image_meta(context, image_ref)

        if not self._valid_image_meta(image_meta):
            LOG.warn(_("ComputeMgr _setup_block_device_mapping() "
                       "exception due to invalid image meta data: "
                       "%(image_meta)s") % locals())
            raise exception.InvalidImageRef(image_href=image_ref)

        if not self._validate_powervc_bdms(bdms, image_meta['id']):
            debug_data = [str(bdm) for bdm in bdms]
            LOG.warn(_("Failed to process boot volume during deploy. "
                       "Dump BDMs: %(debug_data)s") % locals())
            reason = "Failed to process boot volume during deploy."
            self._error_notify(instance, context, 'error', reason)
            raise exception.InvalidBDM()

        # handle the ISO image deploy for IVM cinder based environment
        if self._is_ivm_iso_cinder_deploy(image_meta):
            self._prepare_iso_deploy_boot_volume(context, instance,
                                                 image_meta)
        else:
            LOG.debug("Image has neither mapping nor block_device_mapping but "
                      "has some meta data: %(image_meta)s" % locals())
            image_prop = image_meta["properties"]
            meta_version = self._meta_version(image_prop)

            if meta_version >= 2:
                # For Paxes image(meta_version >=2),
                # the volume_mapping will be processed and boot volume
                # will be built. But the volume will not be attached
                # to instance. Instead, the _boot_from_volume() will be
                # passed to virtual driver spawn() as a call back method.
                # The boot volume attachment will be done after instance
                # lpar creation right before start the instance.
                self._process_powervc_image_meta(context, instance,
                                                 image_meta)

        # refresh bdms
        bdms = blkdev_obj.BlockDeviceMappingList.get_by_instance_uuid(
            context, instance['uuid'])
        # The BDMs has been setup for the volumes created based on
        # image meta data.
        blkdev_info = self._get_instance_volume_block_device_info(context,
                                                                  instance)
        # extend the block_device_info with call method for HMC driver
        # to call back boot volume attach during spawn.
        blkdev_info['powervc_blockdev_callbacks'] = {
            'validate_volumes': self._validate_volumes,
            'attach_volumes': self._attach_volumes_and_boot,
            'validate_attachment': self._validate_attachment}
        return blkdev_info

    @staticmethod
    def _validate_powervc_bdms(bdms, image_id):
        # when _prep_block_device() is invoked, there should at most be
        # one entry which will be the image volume. And the volume
        # will not be attached. For ISO deploy, there should be no BDM's.
        valid = False
        if not bdms:
            valid = True
        elif (len(bdms) == 1 and
              bdms[0]['source_type'] == 'image' and
              not bdms[0]['connection_info'] and
              bdms[0]['image_id'] == image_id):
            valid = True
        return valid

    @staticmethod
    def _meta_version(image_prop):
        """
        identify image meta data version
        Image meta data revision history:
        # 0 : None Paxes image.
        # 1 : Paxes R1 image, which uses volume_id in the property to store
              vdisk uid
        # 2 : Paxes R2 image, which is a cinder volume backed image. It has
              following attributes in the image_meta['properties']
              image_meta['properties']['meta_version']
              image_meta['properties']['volume_mapping'] dict

        :param image_prop: image properties field
        :return: meta_version in float
        """
        if not image_prop:
            return int(0)  # non-Paxes image

        if image_prop.get("volume_id"):
            return int(1)  # Paxes R1 image
        else:
            if not image_prop.get("meta_version"):
                return int(0)  # non-Paxes image
            else:
                return int(image_prop.get("meta_version"))

    @staticmethod
    def _get_image_meta(context, image_ref):
        image_service, image_id = glance.get_remote_image_service(context,
                                                                  image_ref)
        return image_service.show(context, image_id)

    @staticmethod
    def _valid_image_meta(image_meta):
        return True

    #######################################################
    #######  Migration-related Overrides/Extensions  ######
    #######################################################
    @wrap_exception()
    @manager.reverts_task_state
    @manager.wrap_instance_event
    @manager.errors_out_migration
    @manager.wrap_instance_fault
    @send_ui_notification(notify_msg.RESIZE_ERROR, None,
                          log_exception=True)
    def resize_instance(self, context, instance, image,
                        reservations=None, migration=None, migration_id=None,
                        instance_type=None):
        """Starts the migration of a running instance to another host."""
        if not migration:
            migration = self.conductor_api.migration_get(context, migration_id)
        with self._error_out_instance_on_exception(context, instance['uuid'],
                                                   reservations):
            if not instance_type:
                instance_type = self.conductor_api.instance_type_get(
                    context,
                    migration['new_instance_type_id'])

            network_info = self._get_instance_nw_info(context, instance)

            migration.status = 'migrating'
            migration.save(context.elevated())

            instance.task_state = task_states.RESIZE_MIGRATING
            instance.save(expected_task_state=task_states.RESIZE_PREP)

            self._notify_about_instance_usage(
                context, instance, "resize.start", network_info=network_info)

            block_device_info = self._get_instance_volume_block_device_info(
                context, instance)

            disk_info = self.driver.migrate_disk_and_power_off(
                context, instance, migration['dest_host'],
                instance_type, network_info, block_device_info)

            migration_p = obj_base.obj_to_primitive(migration)
            instance_p = obj_base.obj_to_primitive(instance)
            self.conductor_api.network_migrate_instance_start(context,
                                                              instance_p,
                                                              migration_p)

            migration.status = 'post-migrating'
            migration.save(context.elevated())

            instance.host = migration.dest_compute
            instance.node = migration.dest_node
            instance.task_state = task_states.RESIZE_MIGRATED
            instance.save(expected_task_state=task_states.RESIZE_MIGRATING)

            self.compute_rpcapi.finish_resize(
                context, instance,
                migration, image, disk_info,
                migration['dest_compute'], reservations)

            self._notify_about_instance_usage(context, instance, "resize.end",
                                              network_info=network_info)

    @wrap_exception()
    @send_ui_notification(notify_msg.RESIZE_ERROR, notify_msg.RESIZE_SUCCESS,
                          log_exception=True)
    def confirm_resize(self, context, instance, reservations, migration):

        super(PowerVCComputeManager, self). \
            confirm_resize(context, instance, reservations, migration)
        try:
            LOG.info(_("Refreshing statistics for the  %s instance.")
                     % instance.name)
            db_inst = self._query_db_instances(context, [instance])
            self.driver.inventory_instances(context, db_inst)
        except Exception as exc:
            LOG.warn(_('Failed to refresh instance invetory '))
            LOG.exception(exc)

    @wrap_exception()
    @manager.reverts_task_state
    def check_can_live_migrate_destination(self, ctxt, instance,
                                           block_migration=False,
                                           disk_over_commit=False):
        """
        When source host validation fails we need to clean up the driver's
        migration object on the target system.  We can't do this via the
        dictionaries passed between the methods.
        """

        # Dictionary to pass between all the migration methods
        # on both the source and target host threads
        migrate_data = {}

        try:
            migrate_data = super(PowerVCComputeManager, self). \
                check_can_live_migrate_destination(
                    ctxt, instance=instance, block_migration=block_migration,
                    disk_over_commit=disk_over_commit)
        except pvm_exc.IBMPowerVMMigrationInProgress:
            with excutils.save_and_reraise_exception():
                pass
        except Exception:
            with excutils.save_and_reraise_exception():
                self.driver.migration_cleanup(instance)

        return migrate_data

    @wrap_exception()
    def pre_live_migration(self, context, instance, block_migration=False,
                           disk=None, migrate_data=None):

        # Issue 5649: Onboarded lpars do not have a fixed IP assigned causing
        # pre_live_migration to fail.  Rather than wait for a community fix
        # we'll reimplement the manager level pre_live_migration without the
        # fixed IP check.
        try:
            block_device_info = self._get_instance_volume_block_device_info(
                context, instance,
                refresh_conn_info=migrate_data['refresh_conn_info'])
            network_info = self._get_instance_nw_info(context, instance)
        except Exception:
            with excutils.save_and_reraise_exception():
                message = _("An internal error occurred. For more information"
                            ", see log file for host %s." % CONF.host)
                tgt_name = migrate_data.get('dest_sys_name')
                migrate_utils.send_migration_failure_notification(context,
                                                                  instance,
                                                                  tgt_name,
                                                                  message)
        self._notify_about_instance_usage(context, instance,
                                          "live_migration.pre.start",
                                          network_info=network_info)

        pre_live_migration_data = self.driver.pre_live_migration(
            context, instance, block_device_info,
            network_info, disk, migrate_data)

        # NOTE(tr3buchet): setup networks on destination host
        self.network_api.setup_networks_on_host(context, instance,
                                                self.host)

        self._notify_about_instance_usage(context, instance,
                                          "live_migration.pre.end",
                                          network_info=network_info)
        return pre_live_migration_data

    @wrap_exception()
    def live_migration(self, context, dest, instance,
                       block_migration=False, migrate_data=None):
        # BDMS will be updated by pre_live_migration() if there are
        # attached cinder volumes. The info in the BDMS may be
        # changed. Save it for roll-back during exception.
        migrate_data = dict(migrate_data or {})
        old_bdms_src = blkdev_obj.BlockDeviceMappingList.get_by_instance_uuid(
            context, instance['uuid'])
        migrate_data['pre_migrate_bdms'] = old_bdms_src

        super(PowerVCComputeManager, self).live_migration(
            context, dest, instance, block_migration, migrate_data)

    @wrap_exception()
    @manager.reverts_task_state
    def post_live_migration_at_destination(self, context, instance,
                                           block_migration=False,
                                           migrate_data=None):
        """Post operations for live migration .

        :param context: security context
        :param instance: Instance dict
        :param block_migration: if true, prepare for block migration
        :param migrate_data: implementation specific data dictionary
        """
        # Notify post live at destination has started
        LOG.info(_('Post operation of migration started on destination host'),
                 instance=instance)
        network_info = self._get_instance_nw_info(context, instance)
        self._notify_about_instance_usage(context, instance,
                                          "live_migration.post.dest.start",
                                          network_info=network_info)

        # NOTE(tr3buchet): setup networks on destination host
        #                  this is called a second time because
        #                  multi_host does not create the bridge in
        #                  plug_vifs
        self.network_api.setup_networks_on_host(context, instance, self.host)

        migration = {'source_compute': instance['host'],
                     'dest_compute': self.host, }
        self.conductor_api.network_migrate_instance_finish(context, instance,
                                                           migration)

        try:
            # Call driver
            block_device_info = self._get_instance_volume_block_device_info(
                context, instance)
        except Exception:
            with excutils.save_and_reraise_exception():
                message = _("An internal error occurred. For more information"
                            ", see log file for host %s." % CONF.host)
                tgt_name = migrate_data.get('dest_sys_name')
                migrate_utils.send_migration_failure_notification(context,
                                                                  instance,
                                                                  tgt_name,
                                                                  message)

        self.driver.post_live_migration_at_destination(context, instance,
                                                       network_info,
                                                       block_migration,
                                                       block_device_info,
                                                       migrate_data)

        # Restore instance state
        current_power_state = self._get_power_state(context, instance)
        node_name = None
        try:
            compute_node = self._get_compute_info(context, self.host)
            node_name = compute_node['hypervisor_hostname']
        except exception.NotFound:
            LOG.exception(_('Failed to get compute_info for %s') % self.host)
        finally:
            # Do not check for expected task state following a successful
            # migration.  This leads to the health state following to go
            # critical when something other than migration causes the task
            # state to clear. Also, don't clear the task state here, as
            # it will be cleared by a thread kicked off by
            # driver.post_live_migration_at_destination when RMC becomes active
            instance = self._instance_update(context, instance['uuid'],
                                             host=self.host,
                                             power_state=current_power_state,
                                             vm_state=vm_states.ACTIVE,
                                             task_state=None, node=node_name)

        # NOTE(vish): this is necessary to update dhcp
        self.network_api.setup_networks_on_host(context, instance, self.host)
        self._notify_about_instance_usage(context, instance,
                                          "live_migration.post.dest.end",
                                          network_info=network_info)

    @wrap_exception()
    @manager.reverts_task_state
    def rollback_live_migration_at_destination(self, context, instance):

        # We're not calling 'super' so we still need to send notifications
        network_info = self._get_instance_nw_info(context, instance)
        self._notify_about_instance_usage(
            context, instance, "live_migration.rollback.dest.start",
            network_info=network_info)

        # NOTE(tr3buchet): tear down networks on destination host
        self.network_api.setup_networks_on_host(context, instance,
                                                self.host, teardown=True)

        # NOTE(vish): The mapping is passed in so the driver can disconnect
        #             from remote volumes if necessary
        block_device_info = self._get_instance_volume_block_device_info(
            context, instance)

        # Don't call the parent roll-back, we define our own driver level
        self.driver.rollback_live_migration_at_destination(context, instance,
                                                           network_info,
                                                           block_device_info)

        # unplug VLAN at Destination during rollback
        self.driver.unplug_vifs(instance, network_info)

        self._notify_about_instance_usage(
            context, instance, "live_migration.rollback.dest.end",
            network_info=network_info)

    @wrap_exception()
    @manager.reverts_task_state
    @manager.wrap_instance_fault
    def _post_live_migration(self, ctxt, instance,
                             dest, block_migration=False, migrate_data=None):
        """Post operations for live migration on source host.
        We override the manager level code as the behavior for
        IVM and HMC needs to be different

        This method is called from live_migration
        and mainly updating database record.

        :param ctxt: security context
        :param instance: Instance object
        :param dest: destination host
        :param block_migration: if true, prepare for block migration
        :param migrate_data: if not None, it is a dict which has data
        required for live migration without shared storage

        """
        try:
            # Notify the post migration has started
            network_info = self._get_instance_nw_info(ctxt, instance)
            self._notify_about_instance_usage(ctxt, instance,
                                              "live_migration._post.start",
                                              network_info=network_info)
            LOG.info(_('_post_live_migration() is started..'),
                     instance=instance)
            LOG.debug('Migration data = %s' % migrate_data)

            # Get the block device info
            bdms = blkdev_obj.BlockDeviceMappingList.get_by_instance_uuid(
                ctxt, instance['uuid'])
            block_device_info = self._get_instance_volume_block_device_info(
                ctxt, instance, bdms)
        except Exception:
            with excutils.save_and_reraise_exception():
                message = _("An internal error occurred. For more information"
                            ", see log file for host %s." % CONF.host)
                tgt_name = migrate_data.get('dest_sys_name')
                migrate_utils.send_migration_failure_notification(ctxt,
                                                                  instance,
                                                                  tgt_name,
                                                                  message)

        self.driver.post_live_migration(ctxt, instance,
                                        block_device_info, migrate_data)
        # Detach volumes for IVM only.
        # For NPIV/SSP storage, supported only by HMC managed systems,
        # the disk attachment is maintained by powervm during LPM.
        # From openstack's point of view, the attachment is the same no matter
        # which host it is running on. For example, in NPIV scenario, the disk
        # is attached to a virtualized fibre channel adapter assigned to the
        # instance. The attached disk is only visible to the instance.
        # This level of virtualization hides all the implementation details for
        # LPM, and it stays unchanged during the life cycle of the instance
        # regardless which host it is running on.
        refresh_connections = migrate_data['refresh_conn_info']
        if refresh_connections:
            LOG.debug("Terminating the storage connections for migration.")
            connector = self.driver.get_volume_connector(instance)
            for bdm in bdms:
                # NOTE(vish): We don't want to actually mark the volume
                #             detached, or delete the bdm, just remove the
                #             connection from this host.

                # remove the volume connection without detaching
                # from hypervisor because the instance is not
                # running anymore on the current host
                if bdm.is_volume:
                    self.volume_api.terminate_connection(ctxt, bdm.volume_id,
                                                         connector)
        else:
            LOG.debug("Skipping storage operations for migration.")

        # Releasing vlan.
        # (not necessary in current implementation?)
        migration = {'source_compute': self.host,
                     'dest_compute': dest, }
        self.conductor_api.network_migrate_instance_start(ctxt,
                                                          instance,
                                                          migration)

        # Define domain at destination host, without doing it,
        # pause/suspend/terminate do not work.
        self.compute_rpcapi.post_live_migration_at_destination(
            ctxt, instance, block_migration, dest, migrate_data)

        # NOTE(tr3buchet): tear down networks on source host
        self.driver.unplug_vifs(instance, network_info)
        self.network_api.setup_networks_on_host(ctxt, instance,
                                                self.host, teardown=True)
        self.instance_events.clear_events_for_instance(instance)
        self._notify_about_instance_usage(ctxt, instance,
                                          "live_migration._post.end",
                                          network_info=network_info)
        LOG.info(_('Migrating instance to %s finished successfully.'),
                 dest, instance=instance)
        LOG.info(_("You may see the error \"libvirt: QEMU error: "
                   "Domain not found: no domain with matching name.\" "
                   "This error can be safely ignored."),
                 instance=instance)

    @wrap_exception()
    @manager.reverts_task_state
    def _rollback_live_migration(self, context, instance, dest,
                                 block_migration, migrate_data=None):
        """Rollback operations for live migration on source host.
        We override the manager level code as the behavior for
        IVM and HMC needs to be different
        """

        # If pre_live_migration or live_migration fails
        # we need to clean up the migration dict on the source.
        self.driver.migration_cleanup(instance, source=True)

        # Notify rollback has started
        LOG.debug('Rollback starting on source host for instance %s. '
                  'Migration data = %s' % (instance['display_name'],
                                           migrate_data))
        self._notify_about_instance_usage(context, instance,
                                          "live_migration._rollback.start")

        # Set the state back to Active
        instance = self._instance_update(
            context, instance['uuid'], host=instance['host'],
            vm_state=vm_states.ACTIVE, task_state=None,
            expected_task_state=task_states.MIGRATING)

        # NOTE(tr3buchet): setup networks on source host (really it's re-setup)
        self.network_api.setup_networks_on_host(context, instance, self.host)

        # Detach volumes for IVM only.
        refresh_connections = migrate_data['refresh_conn_info']
        if refresh_connections:
            LOG.debug("Removing volume connections for migration.")
            bdms = blkdev_obj.BlockDeviceMappingList.get_by_instance_uuid(
                context, instance['uuid'])
            for bdm in bdms:
                if bdm.is_volume:
                    volume_id = bdm.volume_id
                    try:
                        self.compute_rpcapi.remove_volume_connection(context,
                                                                     instance,
                                                                     volume_id,
                                                                     dest)
                    except Exception as ex:
                        LOG.error(_("Failed to remove volume %(volume_id)s "
                                    "connection  due to error: %(error)s") %
                                  {'volume_id': volume_id, 'error': ex})
        else:
            LOG.debug("Skipping removing volume connection for migration.")

        # Rollback on destination
        self.compute_rpcapi.rollback_live_migration_at_destination(
            context, instance, dest)

        # Block device mapping for attached volumes may have been changed
        # by pre_live_migration(). Need to restore it after roll-back.
        self._restore_pre_migrate_bdms(context, migrate_data)

        # Notify we're done
        LOG.debug('Rollback ending on source host for instance %s.' %
                  instance['display_name'])
        self._notify_about_instance_usage(context, instance,
                                          "live_migration._rollback.end")

    #######################################################
    ########  Monitor-related Overrides/Extensions  #######
    #######################################################
    @periodic_task.periodic_task(spacing=CONF.monitor_instances_interval)
    def monitor_instances(self, context, instances=None):
        """Periodic Task to Refresh Metrics for the Instances"""
        driver, context = (self.driver, context.elevated())
        LOG.debug('Invoking Method to Refresh Instance Metrics - Begin')
        try:
            drv_insts = self.driver.list_instances()
            name_map = dict([(inst, '') for inst in drv_insts])
            # Retrieve All of the Instances from the Nova DB for the Given Host
            db_insts = self._query_db_instances(context, instances)
            db_insts = [inst for inst in db_insts if inst['name'] in name_map]
            # If this is a Discovery Driver, then let it Gather Metrics
            if isinstance(driver, discovery_driver.ComputeDiscoveryDriver):
                # It is only worth Gathering Metrics if there are Instances
                if len(db_insts) > 0:
                    metric_data = driver.monitor_instances(context, db_insts)
                    self._send_metric_notifications(context, metric_data)
        except Exception as exc:
            LOG.warn(_('Error refreshing Instance Metrics'))
            LOG.exception(exc)
        LOG.debug('Invoking Method to Refresh Instance Metrics - End')

    def _send_metric_notifications(self, context, metric_data):
        """Collects with LPAR monitor data (i.e. CPU utilization)"""
        payload, payloads = (list(), list())
        # If there wasn't any Metric data, then log a debug statement
        if len(metric_data) <= 0:
            LOG.debug('No CPU data found: ' + str(metric_data))
        # Loop through the Metric Data on a per-Instance level
        for i in range(len(metric_data)):
            inst_data = metric_data[i]
            payload.append({'instance_id': inst_data['uuid'],
                            'cpu_utilization': inst_data['cpu_utilization']})
            # If we have hit the Message Size limit, add a new chunk
            if (i > 1) and (i % CONF.metrics_message_size == 0):
                payloads.append(payload)
                payload = []
            # If this is the last one, then we want to add the last chunk
            elif i == (len(metric_data) - 1):
                payloads.append(payload)
        # Loop through each of the Payload Chunks, sending a Notification
        for p in payloads:
            notifier = rpc.get_notifier(service='compute', host=self.host)
            notifier.info(context, 'compute.instance.metrics', {'data': p})

    #######################################################
    ########  Network-related Overrides/Extensions  #######
    #######################################################
    def change_vif_sea(self, context, data):
        """
        This API calls the plug method on the compute driver to plug a VLAN
        on a VEA.
        :param context: Context Object
        :param data: The dictionary object containing information to call
        the plug  operation, sent by the API service.
        :return: Calls the compute driver and returns the plug call back.
        """
        greenthread.spawn(self._change_vif_sea, context, data)
        return True

    @logcall
    @lockutils.synchronized('change_powervm_vifs', 'nova-change-vif-powervm-')
    def _change_vif_sea(self, context, data):
        """
        This API calls the plug method on the compute driver to plug a VLAN
        on a VEA.  This is the internal call that should be done on the
        greenthread.

        :param context: Context Object
        :param data: The dictionary object containing information to call
        the plug  operation, sent by the API service.
        :return: Calls the compute driver and returns the plug call back.
        """
        # Obtain data from the input.  Start with common data.
        vlan = data['vlan']

        # Move on to the 'new' data
        # Note that the force=True is only used in the rollback, but
        # we set it here so that we don't have to rebuild.
        new_sea = data['new_sea']
        new_lpar = data['new_lpar_id']
        net_id = {'id': data['net_id']}
        new_vif = network_model.VIF('fake_id', 'fake_address', net_id,
                                    'fake_dev_name', sea_lpar_id=new_lpar,
                                    sea_name=new_sea, force=True,
                                    vlan=vlan)

        # Build a list of network ids that will be updated when complete
        net_ids_to_update = [data['net_id']]
        net_ids_to_update.extend(data.get('peers', []))

        # Build the 'old' data...what is 'currently' plugged in and find all
        # other vifs that will be affected by this change.
        old_sea = ''
        old_lpar = ''
        old_vif_list = []
        na_and_peers_tuple_list = []
        for net_id_to_update in net_ids_to_update:
            net_assn = db.network_association_find(
                context,
                host_name=self.host,
                neutron_network_id=net_id_to_update)
            if(not net_assn or net_assn is None or not net_assn.sea or
               net_assn.sea is None):
                continue
            if net_id['id'] == net_id_to_update:
                old_sea = net_assn.sea.name
                old_lpar = net_assn.sea.vio_server.lpar_id
            na_and_peers_tuple_list.append((net_assn.sea.name,
                                            [net_id_to_update]))

            # It's possible to find duplicates, so make sure we aren't already
            # replugging this vif.
            net_assn_lpar = net_assn.sea.vio_server.lpar_id
            already_listed = False
            for old_vif_entry in old_vif_list:
                if(old_vif_entry['meta']['sea_name'] == net_assn.sea.name or
                   old_vif_entry['network']['id'] == net_id_to_update):
                    # We've already got this vif on the list, skip it
                    already_listed = True
            if not already_listed:
                cur_vif = network_model.VIF('fake_id', 'fake_address',
                                            {'id': net_id_to_update},
                                            'fake_dev_name',
                                            sea_lpar_id=net_assn_lpar,
                                            sea_name=net_assn.sea.name,
                                            force=True,
                                            vlan=vlan)
                old_vif_list.append(cur_vif)

        # Build the driver (IVM or HMC) that will perform the
        # plug/unplug for us
        vif_conf = CONF.powervm_vif_driver
        vif_drv = importutils.import_object(vif_conf, host_name=self.host)

        # For logging purposes, get the network name.
        net_api = network.API()
        neutron_obj = net_api.get(context, data['net_id'])
        if neutron_obj is not None:
            net_name = neutron_obj.get('name', data['net_id'])
        else:
            net_name = data['net_id']

        try:
            # Now start to plug/unplug
            msg = (_("Network %(net)s is being modified to use Shared "
                     "Ethernet Adapter (SEA) %(new_sea)s instead of SEA "
                     "%(old_sea)s on host %(host)s.") %
                   {'host': self.host, 'net': net_name,
                    'old_sea': old_sea, 'new_sea': new_sea})
            LOG.info(msg)

            # Happy path should just be 'unplug and plug'
            vif_drv.unplug(None, old_vif_list)

            # We update the data base now with the new SEA and network_ids.
            # This is because the corresponding plug call will check the
            # database for the network association to find the SEA to work
            # against.
            self.__update_na_and_peers_in_db(context, new_lpar,
                                             [(new_sea, net_ids_to_update)])
            vif_drv.plug(None, [new_vif])

            # Lastly notify the user that the change operation has completed
            msg = (_("Network %(net)s was successfully modified to use Shared "
                     "Ethernet Adapter (SEA) %(new_sea)s instead of SEA "
                     "%(old_sea)s on host %(host)s. Connectivity was restored "
                     "on all virtual machines that use the network") %
                   {'host': self.host, 'net': net_name,
                    'old_sea': old_sea, 'new_sea': new_sea})
            info = {'msg': msg, 'sea_name': new_sea, 'host_name': self.host}
            LOG.info(msg)
            notifier = rpc.get_notifier(service='compute', host=self.host)
            notifier.info(context, 'compute.instance.log', info)

        except Exception as ex:
            # Something went wrong...we need to log and then attempt to replug
            # to the original state.
            LOG.exception(ex)

            # Update the VIFs, note that we now need to set the force to true
            try:
                vif_drv.unplug(None, [new_vif])

                # Update the database to set the Network Mapping back to
                # the original value. This would mean reverting the peers to
                # the older SEA.
                self.__update_na_and_peers_in_db(context, old_lpar,
                                                 na_and_peers_tuple_list)

                vif_drv.plug(None, old_vif_list)
            except Exception as ex2:
                LOG.exception(ex2)

            # Lastly notify the user that something has gone wrong.
            msg = (_("The adapter change on Host %(host)s for Network "
                     "%(net)s failed to finish successfully.  The system has "
                     "returned the following error message: %(msg)s.  "
                     "Please review the issue and retry the command.") %
                   {'host': self.host, 'msg': ex.message,
                    'net': net_name})
            info = {'msg': msg, 'sea_name': new_sea, 'host_name': self.host}
            LOG.warn(msg)
            notifier = rpc.get_notifier(service='compute', host=self.host)
            notifier.warn(context, 'compute.instance.log', info)

    @periodic_task.periodic_task(spacing=CONF.cleanup_net_assn_interval)
    def _cleanup_network_associations(self, context):
        """
        This task periodically cleans up network associations
        of deleted networks
        """
        LOG.info(_('pvc_nova.compute.manager.PowerVCComputeManager '
                   '_cleanup_network_associations: updating network'
                   ' associations...'))
        if isinstance(self.driver, discovery_driver.ComputeDiscoveryDriver):
            self.driver.cleanup_network_associations(
                context, self.host)

    def __update_na_and_peers_in_db(self, context, lpar_id, sea_na_tuple_list):
        """
        Updates the network associations for the net_ids with a new SEA.

        :param context: The user context making the request
        :param lpar_id: The lpar housing the sea
        :param sea_na_tuple_list: A list of SEA/NA tuples to update.
        """
        db_session = session.get_session()
        with db_session.begin():
            vio_servers = db.vio_server_find_all(context, self.host,
                                                 db_session)
            host_dom = dom.Host(self.host, vio_servers)

            for ttuple in sea_na_tuple_list:
                sea = ttuple[0]
                net_ids = ttuple[1]
                sea_dom = host_dom.find_adapter(lpar_id, sea)
                for net_id in net_ids:
                    db.network_association_put_sea(context, self.host, net_id,
                                                   sea_dom, db_session)

    #######################################################
    ########  Storage-related Overrides/Extensions  #######
    #######################################################
    @wrap_exception()
    @manager.reverts_task_state
    @manager.wrap_instance_fault
    @send_ui_notification(notify_msg.ATTACH_ERROR, notify_msg.ATTACH_SUCCESS,
                          log_exception=True)
    def attach_volume(self, context, volume_id, mountpoint, instance,
                      bdm=None):
        """
        attach a volume to an instance, prevent any image volume
        from being attached.
        :param context: operation context
        :param volume_id: volume id for attaching
        :param mountpoint: mount point
        :param instance: compute instance for attaching.
        :param bdm: block device mapping object.
        :return: None
        """
        volume = self.volume_api.get(context, volume_id)
        metadata = volume.get("volume_metadata")

        # For image-volume, is_image_volume will be set
        # in the volume metadata.
        if metadata and ('is_image_volume' in metadata and
                         bool(metadata['is_image_volume'])):
            # roll back attaching state and log a warning message
            # if it is an image volume
            LOG.warn(_("Cannot attach image volume: %(volume_id)s") % locals())
            self.volume_api.unreserve_volume(context, volume_id)
            bdm.destroy(context)
            raise exception.InvalidVolume(reason='Cannot attach image volume')
        else:
            s = super(PowerVCComputeManager, self)
            s.attach_volume(context, volume_id, mountpoint, instance, bdm=bdm)

    @wrap_exception()
    @manager.reverts_task_state
    @manager.wrap_instance_fault
    @send_ui_notification(notify_msg.DETACH_ERROR, notify_msg.DETACH_SUCCESS,
                          log_exception=True)
    def detach_volume(self, context, volume_id, instance):
        """
        detach a volume from an instance, prevent any boot volume
        from being detached.
        :param context: operation context
        :param volume_id: volume id for detaching
        :param instance: compute instance for detaching.
        :return: None
        """
        volume = self.volume_api.get(context, volume_id)
        metadata = volume.get("volume_metadata")
        is_ivm = CONF.powervm_mgr_type == 'ivm'
        # For boot-volume, is_boot_volume will be set
        # in the volume metadata.
        if metadata and ('is_boot_volume' in metadata and
                         bool(metadata['is_boot_volume'])):
            # roll back detaching state and log a warning message
            # if it is a boot volume
            self.volume_api.roll_detaching(context, volume_id)
            msg = (_("Cannot detach boot volume: %(volume_id)s") % locals())
            LOG.warn(msg)
            return
        # OSEE detach_volume part. Paxes need to retrieve
        # the saved connector per BDM during detach for NPIV.
        bdm = blkdev_obj.BlockDeviceMapping.get_by_volume_id(
            context, volume_id)
        if CONF.volume_usage_poll_interval > 0:
            vol_stats = []
            mp = bdm['device_name']
            # Handle bootable volumes which will not contain /dev/
            if '/dev/' in mp:
                mp = mp[5:]
            try:
                vol_stats = self.driver.block_stats(instance['name'], mp)
            except NotImplementedError:
                pass

            if vol_stats:
                LOG.debug(_("Updating volume usage cache with totals"))
                rd_req, rd_bytes, wr_req, wr_bytes, flush_ops = vol_stats
                self.conductor_api.vol_usage_update(context, volume_id,
                                                    rd_req, rd_bytes,
                                                    wr_req, wr_bytes,
                                                    instance,
                                                    update_totals=True)

        self._detach_volume(context, instance, bdm)

        if is_ivm:
            # call the driver to get the connector.
            # this will be the IVM FC attachment since the connector
            # could be different after migration.
            connector = self.driver.get_volume_connector(instance)
        else:
            connector = self._get_bdm_volume_connector(context, instance, bdm,
                                                       volume_id)
            # HMC managed system and has no connector, try to generate
            # it from the saved connection_info.
            if not connector:
                connector = self._construct_connector_from_conninfo(instance,
                                                                    bdm)
            # Last try by called the virt driver. Could be expensive and
            # have exception.
            if not connector:
                try:
                    connector = self.driver.get_volume_connector(instance)
                except Exception as ex:
                    LOG.error(_("Failed to detach volume %(volid)s from "
                                "instance %(inst)s due to null "
                                "volume connector. Error: %(error)s") %
                              {'volid': volume_id,
                               'inst': instance['uuid'],
                               'error': ex})
                    err_msg = {'volid': volume_id,
                               'inst': instance['uuid']}
                    raise pwrvc_excep.IBMPowerVCVInvalidVolumeConnector(
                        **err_msg)
        LOG.info(_("Instance %(inst)s detach volume %(volid)s with connector: "
                   "%(conn)s ") %
                 {'inst': instance['uuid'],
                  'volid': volume_id,
                  'conn': connector})
        self.volume_api.terminate_connection(context, volume_id, connector)
        self.volume_api.detach(context.elevated(), volume_id)
        bdm = blkdev_obj.BlockDeviceMapping.\
            get_by_volume_id(context, volume_id, instance['uuid'])
        if bdm is not None:
            bdm.destroy(context)
        self._instance_update(context, instance['uuid'],
                              updated_at=timeutils.utcnow())
        info = dict(volume_id=volume_id)
        self._notify_about_instance_usage(
            context, instance, "volume.detach", extra_usage_info=info)

    def remove_volume_connection(self, context, volume_id, instance):
        """Override Remove Volume Connection to Clean-up Stale Disks"""
        bdm = blkdev_obj.BlockDeviceMapping.get_by_volume_id(context,
                                                             volume_id)
        cinfo = jsonutils.loads(bdm['connection_info'])
        super(PowerVCComputeManager,
              self).remove_volume_connection(context, volume_id, instance)
        self.driver.remove_volume_device_at_destination(
            context, instance, cinfo)

    def reserve_block_device_name(self, context, instance, *args, **kwargs):
        """ Generate the next available device_name for volume attachment.
        PowerVC hide instance['root_device_name'] during runtime to
        prevent capture to do gown wrong path in PowerVC. But
        instance['root_device_name'] is needed for attach_volume() function
        get generate unique mount point. Otherwise detach_volume() will
        detach the wrong volume. There is no serialization is needed in
        this function since root_device_name is read only and will not
        be changed during runtime. Parenet class has proper lock to
        prevent device_name collision."""
        def set_root_dev_name(name):
            if 'root_device_name' in instance:
                instance['root_device_name'] = name

        s = super(PowerVCComputeManager, self)

        # For PowerKVM, take the openstack path.
        if hasattr(self.driver, "get_hypervisor_type"):
            hypervisor_type = self.driver.get_hypervisor_type()
            if hypervisor_type and hypervisor_type == 'QEMU':
                return s.reserve_block_device_name(context, instance,
                                                   *args, **kwargs)

        # fix the instance['root_device_name'] before attach_volume
        set_root_dev_name(
            self._get_instance_root_device_name(context, instance))

        try:
            device_name = s.reserve_block_device_name(context, instance,
                                                      *args, **kwargs)
            LOG.info(_("powervc_nova compute manager: "
                       "reserve_block_device_name: %(device_name)s") %
                     locals())
            set_root_dev_name(None)
            return device_name

        except Exception:
            with excutils.save_and_reraise_exception():
                LOG.exception(_("Failed to reservice block device name for "
                                "instance: %(inst_uuid)s") %
                              {'inst_uuid': instance['uuid']})
                set_root_dev_name(None)

    def _delete_instance(self, context, instance, bdms,
                         reservations=None):
        """Overridden to handle the HypervisorUnavailable exception
        in the case of PowerKVM. When the libvirt service is down Delete
        task hang indefinitely."""
        s = super(PowerVCComputeManager, self)
        try:
            s._delete_instance(context, instance, bdms,
                               reservations)
        except exception.HypervisorUnavailable:
            with excutils.save_and_reraise_exception():
                if (instance.task_state == task_states.DELETING):
                    instance.task_state = None
                    instance.expected_task_state = None
                    instance.save()

    @send_ui_notification(notify_msg.DELETE_ERROR,
                          notify_msg.DELETE_SUCCESS,
                          error_event_type=EVENT_TYPE_WARN,
                          log_exception=True)
    def terminate_instance(self, context, instance, bdms, reservations):
        """Terminate an instance on this host.
           Override to send ui notification. Notify in the compute manager
           instead of the driver's destroy method, so notifications are only
           sent when a user issues a delete on the instance.
        """
        super(PowerVCComputeManager, self).terminate_instance(
            context, instance, bdms, reservations)

    def _shutdown_instance(self, context, instance,
                           bdms, requested_networks=None, notify=True):
        """ override openstack _shutdown_instance in order to
        save the volume connector before destroy the partition.
        For NPIV/SSP attached volumes, they are not assigned to host.
        The connector doesn't exist after destroy the instance.
        For NPIV, this connector is important to remove the
        hostmap and delete the fabric zone. For SSP, termiante_connection
        is a no-op. For IVM, it is the same as openstack."""

        """Shutdown an instance on this host."""
        context = context.elevated()
        LOG.audit(_('%(action_str)s instance: %(instuuid)s') %
                  {'action_str': 'Terminating', 'instuuid': instance['uuid']},
                  context=context, instance=instance)

        if notify:
            self._notify_about_instance_usage(context, instance,
                                              "shutdown.start")

        # get network info before tearing down
        try:
            network_info = self._get_instance_nw_info(context, instance)
        except (exception.NetworkNotFound, exception.NoMoreFixedIps):
            network_info = network_model.NetworkInfo()

        # refresh bdms from database to sync up the in memory copy
        bdms = blkdev_obj.BlockDeviceMappingList.get_by_instance_uuid(
            context, instance['uuid'])

        # get bdms and volume connector before destroying the instance
        vol_bdms = [bdm for bdm in bdms if bdm.is_volume]
        has_bdm_attachment = False
        for bdm in vol_bdms:
            if self._bdm_has_attachment(bdm):
                has_bdm_attachment = True
                break
        # For IVM, regardless the host_storage_type, paxes will
        # support SAN based volume attach. The connector is the
        # host's FC ports wwpns.
        is_ivm = CONF.powervm_mgr_type == 'ivm'
        if is_ivm:
            vol_connector = self.driver.get_volume_connector(instance)
        else:
            # for NPIV, the connector has been saved in for each BDM. And
            # they might be different for each volume.
            vol_connector = None
        # LPAR get_info
        # Handling the HypervisorUnavailable exception forPowerKVM
        # If VM is Error: Send notification and delete it from DB
        # else send notification and mark it to Error.

        try:
            lpar_info = self.driver.get_info(instance)
        except exception.InstanceNotFound:
            lpar_info = {'state': power_state.NOSTATE,
                         'id': -1}
        except exception.HypervisorUnavailable as e:
            LOG.exception(_("Failed to retrieve LPAR, Delete VM Failed"
                            "%s") % e)
            vm_state = instance['vm_state']
            if (vm_state != vm_states.ERROR):
                reason = (_("Unable to Delete the virtual machine,"
                            "Virtual machine will be changed to Error "
                            "State. Reason: %s. For more information "
                            "see the host log files.") % e)
                LOG.exception(_("Unable to Delete the virtual machine, "
                                "Virtual machine will be changed to Error "
                                "State. Reason: %s. For more information,"
                                "see the host log files.") % e)
                self._error_notify(instance, context, 'error', reason)
                raise e
            reason = (_("Unable to delete the virtual machine. The virtual "
                        "machine will be removed from PowerVC management. "
                        "Reason: %s. For more information, see the host log"
                        "files.") % e)
            LOG.exception(_("Unable to delete the virtual machine. The "
                            "virtual machine will be removed from PowerVC "
                            "management. Reason: %s. For more information,"
                            "see the host log files.") % e)
            self._error_notify(instance, context, 'error', reason)
            return

        lpar_state = (power_state.NOSTATE if not lpar_info.get('state')
                      else lpar_info['state'])
        # If LPAR has been deleted out-of-band, the attachment information
        # exists while connector is empty. Need to be able to destroy
        # the instance for cleanup purpose.
        LOG.info(_("Destroy Virtual Server %(lpar_name)s, current LPAR state:"
                   " %(lpar_state)s") %
                 {'lpar_name': instance['name'],
                  'lpar_state': lpar_state})
        if (is_ivm
            and has_bdm_attachment
            and not self._valid_volume_connector(vol_connector)
                and lpar_state != power_state.NOSTATE):
            # For IVM/SAN based VM, the connector is not saved.
            # If connector cannot be retrieved from driver and
            # the partition has attachment and still exists,
            # paxes doesn't know how to clean it up.
            reason = ("Virtual Server cleanup failure. Please check the host "
                      "log files for futher details.")
            self._error_notify(instance, context,
                               'error', reason)
            raise exception.NovaException(message=reason)

        block_device_info = self._get_instance_volume_block_device_info(
            context, instance)
        LOG.debug("block_device_info: %s" % str(block_device_info))
        # Attempt driver destroy before releasing ip, may
        # want to keep ip allocated for certain failures
        # The volume mapping for the instance will be removed
        # by driver as well. _shutdown_instance should handle
        # the storage side hostmap and SAN fabric zoning removal.
        try:
            self.driver.destroy(context, instance, network_info,
                                block_device_info)
        except Exception as ex:
            if self._get_power_state(context, instance) != power_state.NOSTATE:
                # The lpar still exists. Don't clean up.
                reason = (_("LPAR may not be deleted properly. Please check "
                            "the host log files for further details. "
                            "Error: %(ex)s") %
                          dict(ex=ex))
                self._error_notify(instance, context,
                                   'warn', reason)
                LOG.warn(_("Failed to delete LPAR %(lpar_name)s from HMC when "
                           "deleting instance %(inst)s. Error: %(error)s") %
                         {'lpar_name': instance['name'],
                          'inst': instance['uuid'],
                          'error': ex})
                # this is destroy or clean up path. Try as much as we can.
        try:
            self._try_deallocate_network(context, instance, requested_networks)
        except Exception as ex:
            # LPAR may have been destroyed and there is no way
            # back. Try to clean up as much as we can.
            # The error has been logged in _try_deallocate_network.
            reason = (_("Deleted LPAR successfully. But failed to remove "
                        "the VLAN associated with the LPAR virtual ethernet. "
                        "Please check host log for further "
                        "details. Error: %(ex)s") % dict(ex=ex))
            self._error_notify(instance, context, 'warn', reason)
            LOG.warn(_("Deleted LPAR %(lpar_name)s successfully. But failed "
                       "to remove the VLAN associated with LPAR virtual "
                       "ethernet. Instance: %(inst)s. Error:%(error)s") %
                     {'lpar_name': instance['name'],
                      'inst': instance['uuid'],
                      'error': ex})

        vol_handled = 0
        vol_total = len(vol_bdms)
        last_connector = None
        for bdm in vol_bdms:
            clean_zones = False
            if vol_handled == vol_total - 1:
                clean_zones = True
            last_connector = self._bdm_cleanup_helper(
                context, instance, bdm, vol_connector, clean_zones, is_ivm)
            vol_handled += 1
        vol_connector = last_connector if last_connector else vol_connector
        # Final boot volume cleanup.
        self._boot_volume_cleanup(context, instance, vol_connector)

        # nova compute manager's _cleanup_volumes will be called
        # after this. But OSEE doesn't cleanup BDM after volume
        # cleanup as it said. So the BDMs have been leaked during reschedule.
        for bdm in vol_bdms:
            bdm.destroy(context)
        # sync up bdms memory object. This will prevent _cleanup_volume()
        # from deleting volumes.
        bdms = None

        # cleanup instance after block device cleanup.
        args = {'default_ephemeral_device': None,
                'root_gb': 0}
        instance.update(args)
        self.conductor_api.instance_update(context, instance['uuid'],
                                           **args)

        if notify:
            self._notify_about_instance_usage(context, instance,
                                              "shutdown.end")

    @logcall
    def _attach_volume(self, context, instance, bdm):
        """
        Extend this method to make sure instance has the required virtual
        adapter and maps to physical fibre channel adapter prior to
        cinder's initialize_connection.
        """
        volume_id = bdm.volume_id
        mountpoint = bdm['mount_device']
        try:
            #stg_type = self.volume_api.get_storage_type_for_volume(
            #    context, volume_id)
            if CONF.host_storage_type != 'san':
                stg_type = 'iscsi'
            else:
                stg_type = 'fc'

            self.driver.prep_instance_for_attach(context, instance, stg_type)
        except Exception:
            with excutils.save_and_reraise_exception() as ex:
                self.volume_api.unreserve_volume(context, volume_id)

        try:
            info = self._attach_volume_osee(context, volume_id, mountpoint,
                                            instance)
        except Exception:
            with excutils.save_and_reraise_exception() as ex:
                # We send an asynchronous notification that attachment failed.
                notify_info = {'instance_id': instance['uuid'],
                               'instance_name': instance['name'],
                               'volume_id': volume_id,
                               'host_name': self.host}
                error = ex.value

                try:
                    # Attempt to gather some more useful information for the
                    # error report.

                    # Volume details
                    vol = self.volume_api.get(context, volume_id)
                    notify_info['volume_display_name'] = vol['display_name']

                    # Storage provider details
                    provider_info = self.volume_api.get_stg_provider_info(
                        context, volume_id=volume_id)
                    update = {'storage_provider_display_name':
                              provider_info['host_display_name'],
                              'storage_provider_id':
                              provider_info['storage_hostname'],
                              'error': error}
                    notify_info.update(update)

                    # UI replaces volume_id with volume_display_name hyperlink.
                    msg = _("Could not attach volume {volume_id} to "
                            "virtual server {instance_name}.  Check that "
                            "host {host_name} has connectivity to storage "
                            "provider {storage_provider_id}, and check the "
                            "host's log files for further details of this "
                            "error: %(error)s.") % {'error': error}

                except Exception as e2:
                    LOG.exception(e2)
                    # If anything goes wrong, fall back to just reporting the
                    # information that we have to hand.  We'd end up here if we
                    # failed due to Cinder being down, for example.
                    msg = _("Could not attach volume {volume_id} to virtual "
                            "server {instance_name}.  Check that host "
                            "{host_name} has connectivity to all storage "
                            "providers.  Check the host's log files for "
                            "further details of this error.")

                notify_info['msg'] = msg
                notifier = rpc.get_notifier(service='volume.%s' % volume_id)
                notifier.error(context, 'volume.update.errors', notify_info)
                notifier = rpc.get_notifier(service='compute', host=self.host)
                notifier.error(context, 'compute.instance.log', notify_info)

        return info

    def _detach_volume(self, context, instance, bdm):
        """Do the actual driver detach using block device mapping."""
        volume_id = bdm['volume_id']

        s = super(PowerVCComputeManager, self)
        try:
            s._detach_volume(context, instance, bdm)
        except Exception:
            with excutils.save_and_reraise_exception() as ex:
                error = ex.value
                # Attempt to gather some more useful information for the
                # error report.
                notify_info = {'instance_id': instance['uuid'],
                               'instance_name': instance['name'],
                               'volume_id': volume_id,
                               'host_name': self.host}
                try:
                    # Volume details
                    vol = self.volume_api.get(context, volume_id)
                    notify_info['volume_display_name'] = vol['display_name']

                    # Storage provider details
                    provider_info = self.volume_api.get_stg_provider_info(
                        context, volume_id=volume_id)
                    update = {'storage_provider_display_name':
                              provider_info['host_display_name'],
                              'storage_provider_id':
                              provider_info['storage_hostname'],
                              'error': error}
                    notify_info.update(update)

                    # UI replaces volume_id with volume_display_name hyperlink.
                    msg = _("Could not detach volume {volume_id} from "
                            "virtual server {instance_name}.  Check that "
                            "host {host_name} has connectivity to storage "
                            "provider {storage_provider_id}, and check the "
                            "host's log files for further details of this "
                            "error: {error}.")

                except Exception as e2:
                    LOG.exception(e2)
                    # If anything goes wrong, fall back to just reporting the
                    # information that we have to hand.  We'd end up here if we
                    # failed due to Cinder being down, for example.
                    msg = _("Could not detach volume {volume_id} from virtual "
                            "server {instance_name}.  Check that host "
                            "{host_name} has connectivity to all storage "
                            "providers.  Check the host's log files for "
                            "further details of this error.")

                notify_info['msg'] = msg
                notifier = rpc.get_notifier(service='compute', host=self.host)
                notifier.error(context, 'compute.instance.log', notify_info)

    def _cleanup_volumes(self, context, instance_uuid, bdms):
        """ Override nova compute manager's method. All the volumes
        that have been marked with 'delete_on_termination' are
        handled by _shutdown_instance and _boot_volume_cleanup
        already. NO-OP the parent's _cleanup_volumes otherwise
        it will generate an exception in the compute log.
        """
        pass

    def _restore_pre_migrate_bdms(self, context, migrate_data):
        """Override restore pre-migrate BDMS data for attached volumes"""
        org_bdms = migrate_data.get("pre_migrate_bdms")
        if not org_bdms:
            return
        for bdm in org_bdms:
            connection_info = {'connection_info': bdm['connection_info']}
            self.conductor_api.block_device_mapping_update(
                context, bdm['id'], connection_info)

    def _get_instance_volume_block_device_info(self, context, instance,
                                               refresh_conn_info=False):
        """Override Nova compute manager's method in order to control
        refresh_conn_info. For NPIV VFC attached volume(HMC only), the
        connection stays unchanged during the instance's life cycle.
        There is no need to refresh.
        """
        s = super(PowerVCComputeManager, self)
        if CONF.powervm_mgr_type.lower() == 'hmc' and refresh_conn_info:
            # refresh connection is true under resize. There is no need
            # to refresh for NPIV/SSP attached volume during resize.
            return s._get_instance_volume_block_device_info(
                context, instance, refresh_conn_info=False)
        else:
            return s._get_instance_volume_block_device_info(
                context, instance, refresh_conn_info=refresh_conn_info)

    def _attach_volumes_and_boot(self, context, instance):
        """
        This is callback function to attach volumes before boot
        up lpar.
        :param context: operation context
        :param instance: compute instance
        :return: block_device_info
        """
        LOG.debug("Begin _attach_volumes_and_boot(), %s" % instance['uuid'])

        self._validate_state_before_attach_and_boot(
            context, instance['uuid'],
            method=self._attach_volumes_and_boot.func_name
        )

        bdms = blkdev_obj.BlockDeviceMappingList.get_by_instance_uuid(
            context, instance['uuid'])
        bdms = [bdm for bdm in bdms if bdm.is_volume]

        attached_bdms = []
        boot_cinder_volume_id = instance['default_ephemeral_device']

        # loop through all the volumes ready to attach to the instance
        # during boot and handle the volumes attach.
        # Sort the volumes by 'id' so they get attached in the same order in
        # which they were specified in the image's volume_mapping.
        for bdm in sorted(bdms, key=lambda k: k['id']):
            LOG.debug("Processing BDM %s" % bdm)
            attach_failed = False
            volume_id = bdm['volume_id']

            mountpoint = bdm['device_name']
            # save delete_on_termination since nova compute manage
            # _attach_volume will set delete_on_termination to False
            # regardless.

            delete_on_termination = bdm['delete_on_termination']

            if not mountpoint:
                # now generate a unique mountpoint. Since the instance
                # is not in the state the volume can be attached to,
                # no serialization is needed here. The volume has been
                #  reserved. It cannot be attached to other instance
                # at the same time.

                # bdms is updated every time a volume is attached. Get new copy
                bdms = blkdev_obj.BlockDeviceMappingList.get_by_instance_uuid(
                    context, instance['uuid'])

                # Take the bdms out of the list that don't have a device_name,
                # because nova/compute/utils/get_next_device_name() expects all
                # device_names to have values.
                bdms = [b for b in bdms if b.get('device_name')]

                mountpoint = compute_utils.get_device_name_for_instance(
                    context, instance, bdms, None)

                # Set the device_name in the bdm so attach doesn't try to
                # create a new bdm.
                self._update_bdm_device_name(context, bdm['id'], mountpoint)
                # set the device name in the bdm we already have.
                bdm['device_name'] = mountpoint

            data = {}
            try:
                driver_bdm = blk_dev.DriverVolumeBlockDevice(bdm)
                self._attach_volume(context, instance, driver_bdm)
                # _attach_volume is from parent class, which overwrite the
                # delete_on_termination to False regardless. Restore the proper
                # delete_on_termination after attach_volume.
                data = {'delete_on_termination': delete_on_termination}
                self.conductor_api.block_device_mapping_update(
                    context, bdm['id'], data)

            except Exception:
                if boot_cinder_volume_id != volume_id:
                    # If only data volume fails to be attached, it can
                    # move on. Since connection and reserve has been
                    # terminated by _attach_volume() if it fails to attach,
                    # no additional error handling is needed

                    # TODO(ajiang): Add a "required" field in the
                    # image meta data "volume_mapping" to decide whether
                    # the volume is important enough to stop attach_and_boot
                    # process.
                    attach_failed = True
                else:
                    with excutils.save_and_reraise_exception() as ex:
                        ex_info = ex.value

                        reason = (_("Could not deploy %(name)s due to boot "
                                    "volume: %(volume_id)s attach failure. "
                                    "Error: %(error)s") %
                                  {'name': instance['name'],
                                   'volume_id': volume_id,
                                   'error': ex_info})
                        # The notification has been sent by _attach_volume
                        # don't send it again. Just log the error.
                        LOG.error(reason)
                        # also need to terminate and detach all the attached
                        # volumes if we fail to attach boot volume
                        # For the 'delete_on_termination' volumes,
                        # _delete_instance will clean them up during "delete"
                        # Just clean up attachment here. _delete_instnace will
                        # also clean up the instance BDMS.
                        for tmpbdm in attached_bdms:
                            volid = tmpbdm['volume_id']
                            self._detach_volume(context, instance, tmpbdm)
                            connector = \
                                self.driver.get_volume_connector(instance)
                            self.volume_api.terminate_connection(context,
                                                                 volid,
                                                                 connector)
                            self.volume_api.detach(context, volid)

                # remember the attached volume's bdm for clean up
                if not attach_failed:
                    attached_bdms.append(bdm)

        # return block_device_info
        return self._get_instance_volume_block_device_info(context, instance)

    @logcall
    def _attach_volume_osee(self, context, volume_id, mountpoint,
                            instance):
        context = context.elevated()
        LOG.audit(_('Attaching volume %(volume_id)s to %(mountpoint)s'),
                  {'volume_id': volume_id, 'mountpoint': mountpoint},
                  context=context, instance=instance)
        try:
            connector = self.driver.get_volume_connector(instance)
            msg = (_("Attaching volume %(volume_id)s to instance %(inst)s with"
                     " connector: %(connector)s") %
                   {'volume_id': volume_id,
                    'inst': instance['uuid'],
                    'connector': connector})
            LOG.info(msg)
            connection_info = self.volume_api.initialize_connection(context,
                                                                    volume_id,
                                                                    connector)
            msg = (_("Successfully established volume %(volid)s connectivity, "
                     "connection_info: %(conn_info)s") %
                   dict(volid=volume_id, conn_info="*********"))
            LOG.info(msg)
        except Exception:  # pylint: disable=W0702
            with excutils.save_and_reraise_exception():
                LOG.exception(_("Failed to connect to volume %(volume_id)s "
                                "while attaching at %(mountpoint)s"),
                              {'volume_id': volume_id,
                               'mountpoint': mountpoint},
                              context=context, instance=instance)
                self.volume_api.unreserve_volume(context, volume_id)
        if 'serial' not in connection_info:
            connection_info['serial'] = volume_id

        try:
            self.driver.attach_volume(context,
                                      connection_info,
                                      instance,
                                      mountpoint,
                                      encryption=None)
            msg = (_("Successfully attached volume: %(volid)s to instance:"
                     "%(inst)s.") % dict(volid=volume_id,
                                         inst=instance['uuid']))
            LOG.info(msg)
        except Exception:  # pylint: disable=W0702
            with excutils.save_and_reraise_exception():
                LOG.exception(_("Failed to attach volume %(volume_id)s "
                                "at %(mountpoint)s") %
                              {'volume_id': volume_id,
                               'mountpoint': mountpoint},
                              context=context, instance=instance)
                LOG.info(_("Clean up failed volume &(volid)s attachment "
                           "to instance %(inst)s with connector: "
                           "%(connector)s") %
                         {'volid': volume_id,
                          'inst': instance['uuid'],
                          'connector': connector})
                self.volume_api.terminate_connection(context,
                                                     volume_id,
                                                     connector)

        self.volume_api.attach(context,
                               volume_id,
                               instance['uuid'],
                               mountpoint)
        # validate connector and connection_info and merge.
        # the updated connection info will have connector saved it
        # if a valid NPIV or SSP connector. Each volume attached
        # will its own connector saved.
        conn_info = self._update_conninfo_with_connector(
            context, instance, volume_id, connection_info, connector)
        values = {
            'instance_uuid': instance['uuid'],
            'connection_info': jsonutils.dumps(conn_info),
            'device_name': mountpoint,
            'delete_on_termination': False,
            'virtual_name': None,
            'snapshot_id': None,
            'volume_id': volume_id,
            'volume_size': None,
            'no_device': None}
        self.conductor_api.block_device_mapping_update_or_create(context,
                                                                 values)
        self._instance_update(context, instance['uuid'],
                              updated_at=timeutils.utcnow())
        info = dict(volume_id=volume_id)
        self._notify_about_instance_usage(
            context, instance, "volume.attach", extra_usage_info=info)

    def _validate_state_before_attach_and_boot(self, context,
                                               instance_uuid, method=None):
        """
        validate instance state before attach volumes and boot

        :param context: operation context
        :param instance_uuid: compute instance
        :param method: compute manager method that calls validate
        :return: instance if validate or None
        """

        instance = self.conductor_api.instance_get_by_uuid(context,
                                                           instance_uuid)
        vm_state = instance['vm_state']
        task_state = instance['task_state']

        if (vm_state != vm_states.BUILDING or
                task_state != task_states.SPAWNING):
            msg = (_("_validate_volumes() is called when instance is "
                     "in the wrong state: vm_state=%(vm_state)s "
                     "task_state=%(task_state)s") % locals())
            LOG.error(msg)
            exc_args = {'instance_uuid': instance_uuid,
                        'attr': vm_state,
                        'state': task_state,
                        'method': method}
            raise exception.InstanceInvalidState(**exc_args)

        return instance

    def _validate_attachment(self, context, instance_uuid):
        pass

    def _validate_volumes(self, context, instance_uuid):
        """
        callback function used by virtual driver during spawn to
        validate all the volumes state before attach those
        volumes and boot.
        :param context: operation context
        :param instance_uuid: compute instance uuid
        :return: None
        """
        LOG.debug("Begin _validate_volumes():")
        instance = self._validate_state_before_attach_and_boot(
            context, instance_uuid, method=self._validate_volumes.func_name
        )
        boot_volid = instance["default_ephemeral_device"]
        bdms = blkdev_obj.BlockDeviceMappingList.get_by_instance_uuid(
            context, instance['uuid'])
        bdms = [bdm for bdm in bdms if bdm.is_volume]
        for bdm in bdms:
            volume = self.volume_api.get(context, bdm['volume_id'])
            if 'error' in volume['status']:
                # something went wrong before attach the volume.
                # for boot volume. Deploy will fail. The error
                # will be notified and logged. For non boot volume,
                # log a warning.
                if boot_volid == volume['id']:
                    reason = (_("Instance %(inst)s boot volume creation "
                                "failure. Health status: %(hstatus)s.") %
                              {'inst': instance_uuid,
                               'hstatus': volume.get('health_status')})
                    LOG.debug("Bad volume health: %(volume)s" % locals())
                    self._error_notify(instance, context,
                                       'error', reason)
                    exc_args = {'volid': volume['id'],
                                'volume': volume}
                    raise pwrvc_excep.IBMPowerVCVolumeError(**exc_args)
                else:
                    # non-boot-volume. Log a warning
                    LOG.warn(_("Volume health issue. Skip volume attach. "
                               "%(volume)s") % locals())
                    continue

            # make sure the volumes have been created
            try:
                self._await_block_device_map_created(
                    context, bdm['volume_id'],
                    max_tries=CONF.volume_status_check_max_retries
                )
            except exception.VolumeNotCreated as ex:
                with excutils.save_and_reraise_exception():
                    if boot_volid == volume['id']:
                        # get the latest volume state and report error.
                        volume = self.volume_api.get(context, bdm['volume_id'])
                        imgvol = self.volume_api.get(context,
                                                     volume['source_volid'])

                        reason = (_("Fail to clone boot volume from image "
                                    "volume: %(imgvolname)s. Boot volume"
                                    "Health status: %(hstatus)s")
                                  % {'imgvolname': imgvol['display_name'],
                                     'hstatus': volume.get("health_status")})

                        self._error_notify(instance, context,
                                           'error', reason)
                        LOG.error(_("Fail to create boot volume: %(volume)s"
                                    " from image volume: %(imgvol)s")
                                  % locals())

            volume = self.volume_api.get(context, bdm['volume_id'])
            # make sure the volume is in the right state to attach
            self.volume_api.check_attach(context, volume, instance=instance)
            # change the volume to "attaching" state
            self.volume_api.reserve_volume(context, volume['id'])

    def _boot_volume_cleanup(self, context, instance, connector):
        """clean up any boot volume that wasn't populated
        into BDM after clone and instance failed to spawn.
        nova compute shutdown_instance won't be able to
        find that type boot volume to clean up.
        """
        boot_volid = instance['default_ephemeral_device']
        if not boot_volid:
            return

        try:
            boot_volume = self.volume_api.get(context, boot_volid)
            boot_bdm = blkdev_obj.BlockDeviceMapping.\
                get_by_volume_id(context, boot_volid)
            LOG.info(_("Final boot volume clean up during termination.  "
                       "boot_volume: %(boot_volume)s, boot_bdm: "
                       "%(boot_bdm)s") % locals())
            if boot_volume['status'] == 'deleting':
                # clean up in progress. Nothing left to delete.
                return

        except Exception:
            LOG.info(_("No cleanup needed, boot volume %(boot_volid)s "
                       "was already removed.") % locals())
            return

        # clean up boot_volume that has lost BDM entry.
        if (not boot_bdm and boot_volume and
                boot_volume['id'] == boot_volid):
            # it is truly a ghost boot device. Otherwise, there is
            # a BDM entry for the boot device.
            if boot_volume['status'] != 'available':
                # make sure attach_status is okay for us to delete
                if (boot_volume['attach_status'] == 'attached' and
                        connector):
                    # let's try to detach it first. But we don't have
                    # connection info since BDM entry has been missing.
                    # let's try clean up on cinder side.
                    message = (_("Detach ghost boot volume %(volid)s for "
                                 "instance %(inst)s with connector: "
                                 "%(connector)s") %
                               {'volid': boot_volid,
                                'inst': instance['uuid'],
                                'connector': connector})
                    try:
                        self.volume_api.terminate_connection(context,
                                                             boot_volid,
                                                             connector)
                        self.volume_api.detach(context.elevated(),
                                               boot_volid)
                    except Exception as ex:
                        reason = (_("Failed to clean up boot volume "
                                    "%(boot_volume)s during LPAR destroy"))
                        self._error_notify(instance, context,
                                           'warn', reason)

                        LOG.debug("Failed to clean up boot volume connection "
                                  "via cinder. volume: %(boot_volume)s, "
                                  "connector=%(connector)s" % locals())
                else:
                    LOG.warn(_("Volume connector for boot volume "
                               "%(boot_volid)s has been lost. Failed "
                               "to clean up boot volume: %(boot_volume)s") %
                             locals())

                # Make sure the attached state will be unset.
                self.volume_api.unreserve_volume(context, boot_volid)

            self.volume_api.delete(context, boot_volid)

        return

    def _bdm_cleanup_helper(self, context, instance, bdm, connector=None,
                            clean_zones=False, is_ivm=False):
        """
        The helper function to be used by _shutdown_instance(). The
        purpose is to consolidate all the volume detach and terminate
        connection function needed during VM destroy.
        TODO: change iv_ivm to is_npiv to make it clear since only
              NPIV attachment uses the saved connector. For IVM
              environment, the connector is the host FC ports and will
              not change.
        """
        if not bdm or (is_ivm and not connector):
            return

        volume_id = None
        try:
            volume_id = bdm['volume_id']
            volume = self.volume_api.get(context, volume_id)
            stg_type = self.volume_api.get_storage_type_for_volume(context,
                                                                   volume_id)
            # For the volume that marked "delete_on_termination"
            # and has no attachment, just delete it.
            if not self._bdm_has_attachment(bdm):
                if bdm['delete_on_termination']:
                    LOG.debug("Terminating bdm: %(bdm)s" % dict(bdm=bdm))
                    # volume attach transcation may have started and then
                    # gets into exception and cleanup before the
                    # attachment is really done. unreserve the volume to
                    # make sure it is deletable.
                    if volume['status'] == 'attaching':
                        self.volume_api.unreserve_volume(
                            context, bdm['volume_id'])
                    self.volume_api.delete(context, bdm['volume_id'])
                return

            if not is_ivm:
                # retrieve the connector from bdm connection_info
                connector = self._get_bdm_volume_connector(
                    context, instance, bdm, bdm['volume_id'])
                # for SSP connector will be None
                if not connector and stg_type == 'fc':
                    # the connector is lost, but there is an
                    # attachment.
                    if volume['attach_status'] == 'attached':
                        connector = self._construct_connector_from_conninfo(
                            instance, bdm)
                        if not connector:
                            # This is critical condition. Send UI notification.
                            message = (_("Could not terminate storage host "
                                         "mapping and SAN zoning for volume: "
                                         "%(volid)s when deleting "
                                         "the virtual server. Please check the"
                                         "host logs for further details.") %
                                       dict(volid=volume_id))
                            self._error_notify(instance, context,
                                               'critical',
                                               message)
                            # No need to try terminate_connection which will
                            # raise exception. The nova "detached" volume
                            # will be left in 'in-use' state. This needs
                            # some out of band clean up.
                            # TODO: Need to have cinder volume driver to
                            # rebuild the connector and send it back.
                            return
                        # Built a connnector based on the connection_info.
                        LOG.debug("connector created from connection_info: "
                                  "%(connector)s" % dict(connector="*******"))

                if stg_type == 'fc' and not is_ivm and clean_zones:
                    # It is the last attached volume for VM. Tell cinder that
                    # it is time to remove all the zones.
                    connector['clean_zones'] = 'all'
            LOG.info(_("Detach volume %(volume_id)s for instance %(inst)s "
                       "with connector: %(connector)s") %
                     {'volume_id': volume_id,
                      'inst': instance['uuid'],
                      'connector': connector})
            self.volume_api.terminate_connection(context,
                                                 bdm['volume_id'],
                                                 connector)
            self.volume_api.detach(context, volume_id)
            # make sure the volume in attaching state changes back to
            # available, so it could be handled by cleanup_volumes().
            self.volume_api.unreserve_volume(context, bdm['volume_id'])
            if volume_id and bdm['delete_on_termination']:
                LOG.debug("Terminating bdm: %(bdm)s" % dict(bdm=bdm))
                self.volume_api.delete(context, bdm['volume_id'])
        except exception.DiskNotFound as exc:
            LOG.warn(_('Ignoring DiskNotFound: %(exc)s, instance: %(inst)s,'
                       'volume id: %(volid)s') %
                     dict(exc=exc, inst=instance['uuid'], volid=volume_id))
        except exception.VolumeNotFound as exc:
            LOG.warn(_('Ignoring VolumeNotFound: %(exc)s, instance: %(inst)s,'
                       'volume id: %(volid)s') %
                     dict(exc=exc, inst=instance['uuid'], volid=volume_id))
        except Exception as exc:
            if not is_ivm:
                reason = (_("The virtual server was deleted. A failure "
                            "occurred for one of the following reasons: "
                            "The host mapping was unable to be removed for "
                            "one or more volumes on the storage controllers, "
                            "or the NPIV zone information was unable to be "
                            "removed from the switch fabric. Error: %(exc)s")
                          % dict(exc=exc))
            else:
                reason = (_("The virtual server was deleted. A failure "
                            "occurred due to the host mapping "
                            "was unable to be removed for one or more "
                            "volumes on the storage controllers. "
                            "Error: %(exc)s")
                          % dict(exc=exc))
            self._error_notify(instance, context, 'warn', reason)
            LOG.debug("Unable to terminate and detach volume for instance "
                      "%(inst)s %(volid)s with connector: %(conn)s, BDM: "
                      "%(bdm)s" %
                      {'volid': bdm['volume_id'],
                       'inst': instance['uuid'],
                       'conn': connector,
                       'bdm': str(bdm)})
        return connector

    @logcall
    def _get_bdm_volume_connector(self, context, instance, bdm, volume_id):
        """ For each volume that has been attached, it will be attached
        with a certain connector which will not change during the life
        cycle of the attachment. Save the connector to the connection_info.
        And it will be retrieved during terminate_connection. For NPIV,
        get_volume_connector could change since the SCG could be different.
        By saving the connector, the terminate_connection will be successful.
        Otherwise, the terminate_connector may not work if a different
        connector is given that is different from the one that used
        to initialize_connection().
        """

        if not bdm:
            bdm = blkdev_obj.BlockDeviceMapping.get_by_volume_id(context,
                                                                 volume_id)
        if not bdm or bdm['volume_id'] != volume_id:
            return None

        connector = None
        c_info = bdm.get("connection_info")
        #LOG.debug("BDM: %(bdm)s and connection_info: %(c_info)s"
        #          % locals())

        if c_info and isinstance(c_info, basestring):
            try:
                connection_info = jsonutils.loads(c_info)
                vol_conn = connection_info.get("volume_connector")
                connector = self._volume_connector_validator(vol_conn)
            except:
                # invalid connector
                connector = None
        return connector

    def _construct_connector_from_conninfo(self, instance, bdm):
        """ The connection_info for NPIV attached volume has an
        attribute called "initiator_target_map". It contains the
        vfc wwpn to target wwpn mapping. This could also be used
        to build a potential "good enough" connector for
        terminate_connection to use.
        """
        connector = None
        if not bdm or not bdm.get("connection_info"):
            return connector
        try:
            conninfo = bdm['connection_info']
            if not conninfo.get('driver_volume_type'):
                return connector
            conn_type = conninfo['driver_volume_type']
            if conn_type == 'fibre_channel' and conninfo.get('data'):
                conn_data = conninfo['data']
                if conn_data.get('initiator_target_map'):
                    # this is the NPIV connection. Let's construct the
                    # connector from the map.
                    # initiator_target_map looks like this:
                    # {"b": {"c0507606529b01a3":
                    #         ["500507680510041a", "500507680510041b"],
                    #        "c0507606529b01a2":
                    #         ["500507680510041a", "500507680510041b"]},
                    #  "a":....}
                    i_t_map = conn_data['initiator_target_map']
                    if i_t_map and isinstance(i_t_map, dict):
                        # remove all the duplicates.
                        wwpns = set()
                        for tag in i_t_map.values():
                            # wwpns will have unique VFC wwpns for the VM.
                            wwpns.union(set(tag.keys()))
                        connector = {'connection-type': CONNECTION_TYPE_NPIV,
                                     'connector':
                                     {'host': instance['name'],
                                      'wwpns': list(wwpns),
                                      'phy_to_virt_initiators': None}}
            elif conn_type == CONNECTION_TYPE_SSP:
                connector = {'connection-type': CONNECTION_TYPE_SSP,
                             'connector': None}
        except Exception:
            connector = None
        #LOG.debug("_construct_connector_from_conninfo: %(connector)s" %
        #          locals())
        return connector

    @logcall
    def _update_conninfo_with_connector(self, context, instance, volume_id,
                                        connection_info, connector):
        """
        This is helper function to update the connection_info with a new
        volume_connector field if the attached volume connector is VFC(NPIV)
        or SSP based. For HMC managed PowerVC, the volume connector will
        not change during the life cycle of the volume attachment even
        after migration. Save the connector can help speed up the
        detach_volume patch as well destroy/cleanup path with may not
        have a valid volume connector since LPAR may be in some invalid
        state.
        """
        if not volume_id or not connection_info or not connector:
            return connection_info
        volume_connector = None

        try:
            volume = self.volume_api.get(context, volume_id)

            if 'error' in volume['status']:
                # The volume is not healthy after initialize_connection.
                # something went wrong and we don't want to update
                # the BDM with the connection info.
                raise ValueError
            if connection_info['driver_volume_type'] == 'ibm_ssp':
                volume_connector = {'connection-type': CONNECTION_TYPE_SSP,
                                    'connector': None}
            elif (connection_info['driver_volume_type'] == 'fibre_channel'
                  and connector['phy_to_virt_initiators']
                  and connector['host']
                  and connector['wwpns']
                  and connection_info['data']['target_wwn']
                  and int(connection_info['data']['target_lun']) >= 0
                  and connection_info['data']['volume_id'] == volume_id):
                # if there is anything missing, it will cause exception.
                volume_connector = {'connection-type': CONNECTION_TYPE_NPIV,
                                    'connector': connector}
            else:
                volume_connector = None
        except:
            volume_connector = None
        # for IVM san FC connection, it will not be updated with the
        # connector
        if volume_connector:
            connection_info.update({'volume_connector': volume_connector})
        return connection_info

    @logcall
    def _volume_connector_validator(self, volume_connector):
        """
        Validate the volume connector format saved into the. Not the content.
        This is the volume_connector saved in the BDM connection_info.
        Will return the real volume connector if it is valid.
        For IVM FC connector, we don't save it since it could be
        attached to both source/target host during migration.
        volume_connector:
            { 'connection-type': 'npiv'| 'ibm_ssp',
              'connector': {...} actual connector returned from
              get_volume_connector
            }
            fc is the legacy FibreChannel connector
            npiv is the NPIV VFC connector
            ibm_ssp is the ssp vscsi connector
        """

        if not volume_connector:
            return None

        conn_info = None
        try:
            c_type = volume_connector['connection-type']
            conn_info = volume_connector['connector']
            if c_type == CONNECTION_TYPE_NPIV:
                if (not conn_info['host'] or
                    not conn_info['wwpns'] or
                        not conn_info['phy_to_virt_initiators']):
                    conn_info = None
            elif c_type == CONNECTION_TYPE_SSP:
                conn_info = None
            else:
                conn_info = None
        except:
            # validation failure
            conn_info = None
        return conn_info

    def _get_instance_root_device_name(self, context, instance):
        """ Helper function to generate root_device_name for
        instance"""
        bdms = self.conductor_api.block_device_mapping_get_all_by_instance(
            context, instance)
        root_bdm = [bdm for bdm in bdms if bdm['volume_id'] and
                    bdm['volume_id'] == instance['default_ephemeral_device']]

        if root_bdm:
            return root_bdm[0]['device_name']
        else:
            return '/dev/sda'

    def _update_bdm_device_name(self, context, bdm_id, device_name):
        value = {'device_name': device_name}
        self.conductor_api.block_device_mapping_update(context,
                                                       bdm_id,
                                                       value)

    def _bdm_has_attachment(self, bdm):
        if not bdm or not bdm.get("connection_info"):
            return False
        connection_info = bdm.get("connection_info")
        if (not connection_info or 'driver_volume_type' not in
                connection_info or 'data' not in connection_info):
            return False
        else:
            return True

    def _valid_volume_connector(self, connector):
        if not connector:
            return False
        if connector.get('host') and connector.get('wwpns'):
            return True
        elif connector.get('host') and connector.get('initiator'):
            return True
        else:
            return False

    def _is_managing_PowerKVM(self):
        if hasattr(self.driver, "get_hypervisor_type"):
            hypervisor_type = self.driver.get_hypervisor_type()
            # For PowerVM, use set task states differently
            if hypervisor_type and hypervisor_type == 'QEMU':
                return True
        return False

    def is_instance_on_host(self, context, instance_uuid, instance_name):
        """ Answer query from another host if instance
            is on this host.
        """
        ret = self.driver.is_instance_on_host(instance_uuid, instance_name)
        if ret:
            LOG.info(_('Virtual machine %(name)s (%(uuid)s) is being '
                       'managed by this host.') %
                     {'uuid': instance_uuid, 'name': instance_name})
        else:
            LOG.info(_('Virtual machine %(name)s (%(uuid)s) is not '
                       'being managed by this host.') %
                     {'uuid': instance_uuid, 'name': instance_name})
        return ret

#     @object_compat
#     @wrap_exception()
#     @wrap_instance_fault
    def get_ssh_console(self, context, console_type, instance):
        """Return connection information for a vnc console."""
        context = context.elevated()
        LOG.debug(_("Getting ssh console"), instance=instance)
        token = str(uuid.uuid4())

        if not CONF.wssh_enabled:
            raise exception.ConsoleTypeInvalid(console_type=console_type)

        if console_type == 'wssh':
            access_url = '%s?token=%s' % (CONF.wsshproxy_base_url, token)
        else:
            raise exception.ConsoleTypeInvalid(console_type=console_type)

        try:
            # Retrieve connect info from driver, and then decorate with our
            # access info token
            #connect_info = self.driver.get_vnc_console(context, instance)
            connect_info = self.driver.get_ssh_console(context, instance)
            connect_info['token'] = token
            connect_info['access_url'] = access_url
        except exception.InstanceNotFound:
            if instance['vm_state'] != vm_states.BUILDING:
                raise
            raise exception.InstanceNotReady(instance_id=instance['uuid'])

        return connect_info

#     @object_compat
#     @wrap_exception()
#     @wrap_instance_fault
    def validate_console_port(self, ctxt, instance, port, console_type):
        if console_type == "spice-html5":
            console_info = self.driver.get_spice_console(ctxt, instance)
        elif console_type == "rdp-html5":
            console_info = self.driver.get_rdp_console(ctxt, instance)
        elif console_type == "novnc":
            console_info = self.driver.get_vnc_console(ctxt, instance)
        else:
            console_info = self.driver.get_ssh_console(ctxt, instance)
        return console_info['port'] == port

    @logcall
    def image_create(self, ctxt, image_meta, image_data):
        image_id = image_data.get('image_id', None)
        if image_id is None:
            return
        LOG.info(_('Downding image %(image_id)s to %(host)s') %
                  {'image_id' : image_id,
                   'host' : CONF.host})
        if 'volume_id' not in image_data:
            imagevolume = self._generate_boot_volume_name(None,'images') + '_' + image_id[:8]
            volume_size = image_data.get('src_size', None)
            if not volume_size:
                volume_size = image_data.get('size') / (1024**3) + 1
            volume = self._image_create(ctxt, image_meta, image_data, 
                                           volume_size, 
                                           imagevolume)
            image_data['volume_id'] = volume['id']
        if volume['status'] == 'error':
            raise
        is_back = image_data.get('is_back', None)
        if is_back:
            self._image_attach_volume(ctxt, image_meta, image_data) 
        else:
            self.driver.image_create(ctxt, image_meta, image_data)

    def _image_create(self, context, image_meta, image_data, 
                      volume_size, name, vol_type=None, vol_meta=None):
        image_id = image_data.get('image_id', None)
        vol_type = vol_type or CONF.host_storage_type
        name = name or CONF.host + 'Image_Cinder'
        try:
            # The volume_type here is passed in by caller.
            # for IVM iso deploy, _prepare_iso_deploy_boot_volume
            # gets it from flavor.
            vol = self.volume_api.create(context, volume_size,
                                         name, '', volume_type=vol_type,
                                         metadata=vol_meta)
            details = (_("Started create volume %(volid)s "
                         "with size: %(size)s GB and "
                         "volume type \"%(voltype)s\" for instance "
                         "%(image_id)s") %
                       {'volid': vol['id'],
                        'size': volume_size,
                        'voltype': vol_type,
                        'image_id': image_id})
            LOG.info(details)

        except Exception:
                with excutils.save_and_reraise_exception() as ex:
                    error = ex.value
                    details = (_("Could not create %(size)sGB boot volume "
                                 "during deploy instance %(inst)s"
                                 "Error: %(error)s" %
                                 {'size': volume_size,
                                  'image_id': image_id,
                                  'error': error}))
                    LOG.error(details)
        # check the volume status. If in "error" state stop the
        # the build process.
        volume = self.volume_api.get(context, vol['id'])
        return volume

    def _image_attach_volume(self, ctxt, image_meta, image_data):
        context = ctxt.elevated()
        instance = None
        volume_id = image_data.get('volume_id')
        try:
            connector = self.driver.get_volume_connector(instance)
            msg = (_("Attaching volume %(volume_id)s  with"
                     " connector: %(connector)s") %
                   {'volume_id': volume_id,
                    'connector': connector})
            LOG.info(msg)
            connection_info = self.volume_api.initialize_connection(context,
                                                                    volume_id,
                                                                    connector)
            msg = (_("Successfully established volume %(volid)s connectivity, "
                     "connection_info: %(conn_info)s") %
                   dict(volid=volume_id, conn_info="*********"))
            LOG.info(msg)
        except Exception:  # pylint: disable=W0702
            with excutils.save_and_reraise_exception():
                LOG.exception(_("Failed to connect to volume %(volume_id)s ",
                              {'volume_id': volume_id}))
                self.volume_api.unreserve_volume(context, volume_id)
        if 'serial' not in connection_info:
            connection_info['serial'] = volume_id
        
        try:
            self.driver.image_attach_volume(context,
                                            image_meta,
                                            image_data,
                                            connection_info,
                                            encryption=None)
            msg = (_("Successfully attached volume: %(volid)s")
                                        % dict(volid=volume_id))

            LOG.info(msg)
        except Exception:  # pylint: disable=W0702
            with excutils.save_and_reraise_exception():
                LOG.exception(_("Failed to attach volume %(volume_id)s ") %
                              {'volume_id': volume_id},
                              context=context, instance=instance)
                LOG.info(_("Clean up failed volume &(volid)s attachment "
                           "with connector: "
                           "%(connector)s") %
                         {'volid': volume_id,
                          'connector': connector})
        finally:
            self.volume_api.terminate_connection(context,
                                                volume_id,
                                                connector)

#######################################################
##########  Override ResourceTracker Class  ###########
#######################################################
class PowerVCResourceTracker(resource_tracker.ResourceTracker):
    """Extends the base ResourceTracker class for PowerVC to fix issues"""

    def __init__(self, host, driver, nodename):
        """Constructor for the PowerVC extension to the Resource Tracker."""
        super(PowerVCResourceTracker, self).__init__(host, driver, nodename)
        self.first_update = True

    def update_available_resource(self, context):
        """Override Update Available Resources to Refresh the Statistics"""
        # We first need to refresh stats, since the Resource Tracker doesn't
        self.driver.get_host_stats(refresh=True)
        # Check first if we are in an error condition and shouldn't continue
        resources = self.driver.get_available_resource(self.nodename)
        stats = resources.get('stats', {})
        state = stats.get('hypervisor_state')
        # If the only thing in the stats is the state and is error, this means
        # we couldn't connect on startup, so bypass everything and set to error
        if len(stats) == 1 and (state != 'operating' and state != 'standby'):
            self._initialize_compute_node(context)
            #Since we don't want to wipe out existing stats, merge the state in
            exist_stats = self._stats_to_dict(self.compute_node['stats'])
            exist_stats['hypervisor_state'] = state
            self.stats['hypervisor_state'] = state
            self._update(context, {'stats': self._stats_to_str(exist_stats)})
            return
        # If it wasn't an initialization error, delegate to the parent class
        super(PowerVCResourceTracker, self).update_available_resource(context)

    def _report_hypervisor_resource_view(self, resources):
        """Log the hypervisor's view of free resources.

        This is just a snapshot of resource usage recorded by the
        virt driver.

        The following resources are logged:
            - free memory
            - free disk
            - free CPUs
            - assignable PCI devices

        Override, so that the driver can log the appropriate value
        for free disk. Free disk on a host is only applicable in
        local storage environments.
        """
        if hasattr(self.driver, "log_free_disk"):
            is_audit = False
            self.driver.log_free_disk(is_audit)

            free_ram_mb = resources['memory_mb'] - resources['memory_mb_used']
            LOG.debug(_("Hypervisor: free ram (MB): %s") % free_ram_mb)

            vcpus = resources['vcpus']
            if vcpus:
                free_vcpus = vcpus - resources['vcpus_used']
                LOG.debug(_("Hypervisor: free VCPUs: %s") % free_vcpus)
            else:
                LOG.debug(_("Hypervisor: VCPU information unavailable"))

            if 'pci_passthrough_devices' in resources and \
                    resources['pci_passthrough_devices']:
                LOG.debug(_("Hypervisor: assignable PCI devices: %s") %
                          resources['pci_passthrough_devices'])
            else:
                LOG.debug(_("Hypervisor: no assignable PCI devices"))
        else:
            super(PowerVCResourceTracker, self). \
                _report_hypervisor_resource_view(resources)

    def _report_final_resource_view(self, resources):
        """Report final calculate of free memory, disk, CPUs, and PCI devices,
        including instance calculations and in-progress resource claims. These
        values will be exposed via the compute node table to the scheduler.

        Override, so that the driver can log the appropriate value
        for free disk. Free disk on a host is only applicable in
        local storage environments.
        """
        if hasattr(self.driver, "log_free_disk"):
            is_audit = True
            self.driver.log_free_disk(is_audit)

            LOG.audit(_("Free ram (MB): %s") % resources['free_ram_mb'])

            vcpus = resources['vcpus']
            if vcpus:
                free_vcpus = vcpus - resources['vcpus_used']
                LOG.audit(_("Free VCPUS: %s") % free_vcpus)
            else:
                LOG.audit(_("Free VCPU information unavailable"))

            if 'pci_devices' in resources:
                LOG.audit(_("Free PCI devices: %s") % resources['pci_devices'])
        else:
            super(PowerVCResourceTracker, self). \
                _report_final_resource_view(resources)

    def _sync_compute_node(self, context, resources):
        """Override Sync Compute Node to re-add any missing statistics,
        and to only update if resources have changed"""
        update = True
        # See if the Compute Node already exists and hasn't been initialized
        self._initialize_compute_node(context)
        # We want to make sure Stats are in Dictionary format for comparison
        if resources.get('stats') is not None:
            resources['stats'] = self._stats_to_dict(resources['stats'])
        # Since for some inexplicable reason the Base Resource Tracker wipes
        # out any stats returned from the driver, we need to re-add any set
        orig_resources = self.driver.get_available_resource(self.nodename)
        if orig_resources and orig_resources.get('stats'):
            # Make sure that there is a Statistics Dictionary on the Resource
            stats = resources.get('stats')
            self.stats.update(orig_resources.get('stats'))
            resources['stats'] = dict() if stats is None else stats
            # Loop through each of the Original Statistics, adding missing ones
            for key, val in orig_resources['stats'].iteritems():
                if not key in resources['stats']:
                    resources['stats'][key] = val

        # If compute node is None, it may need to be created
        if self.compute_node:
            update = False
            # Found compute node. This is an update
            # Compare resources with compute_node from the database.
            # Update only if something is changing.
            for resource_key, resource_val in resources.iteritems():
                oldval = self.compute_node.get(resource_key)
                if resource_key == 'stats':
                    oldval = jsonutils.loads(oldval)
                    if self._have_stats_changed(oldval, resource_val):
                        update = True
                        break
                # value is not 'stats' dictionary
                elif str(oldval) != str(resource_val):
                    update = True
                    break
        if update or self.first_update:
            self.first_update = False
            # Since the Stats need to be a Dict for Notifications and a JSON
            # string for the DB, we will make a copy for sending to the DB
            dbresources = copy.deepcopy(resources)
            if dbresources.get('stats') is not None:
                dbresources['stats'] = self._stats_to_str(dbresources['stats'])
            # Send a Notification that are Creating/Updating the Compute Node
            creating = self.compute_node is None
            self._notify_compute_node_update(
                context, resources, 'start', creating)
            try:
                # Now we can call the parent to add the data to the database
                super(PowerVCResourceTracker,
                      self)._sync_compute_node(context, dbresources)
            finally:
                # Send a Notification that we Created/Updated the Compute Node
                self._notify_compute_node_update(
                    context, resources, 'end', creating)

    def _initialize_compute_node(self, context):
        """Helper Method to cache the existing Compute Node if there is one"""
        # Since the super class compares against the hypervisor_hostname to
        # determine if the compute_node already exists (which changes), we will
        # see if a compute_node exists for this host and use no matter the
        # hypervisor_hostname, since we only have 1 Node per Host for Paxes.
        if not self.compute_node:
            service = self._get_service(context)
            if(service and service['compute_node'] and
               len(service['compute_node']) > 0):
                self.compute_node = service['compute_node'][0]

    def _notify_compute_node_update(self, context,
                                    resources, postfix, creating):
        """Helper Method to Notify we are Creating/Updating Compute Node"""
        event_type = 'compute.node.create.'
        info = {'host': self.host, 'hypervisor_hostname': self.nodename}
        info['host_display_name'] = CONF.host_display_name
        # If this is an update, we want to notify the caller of what we updated
        if not creating:
            event_type = 'compute.node.update.'
        # If the Compute Node was created, we can get the ID and Service ID
        if self.compute_node:
            info['compute_node_id'] = self.compute_node['id']
            info['service_id'] = self.compute_node['service_id']
            info.update(resources)
        # Send the notification that we are Creating/Updating the Compute Node
        notifier = rpc.get_notifier(service='compute', host=self.host)
        notifier.info(context, event_type + postfix, info)

    def _stats_to_dict(self, statstr):
        """Helper Method to convert a Stats JSON String to a Dictionary"""
        if statstr is None:
            return dict()
        if isinstance(statstr, dict):
            return statstr.copy()
        return jsonutils.loads(statstr)

    def _stats_to_str(self, statdict):
        """Helper Method to convert a Stats Dictionary to a JSON String"""
        if statdict is None:
            statdict = dict()
        if isinstance(statdict, dict):
            return jsonutils.dumps(statdict)
        return statdict

    def _have_stats_changed(self, old_stats, new_stats):
        """Helper method to see if the Stats Dictionary changed"""
        # If there weren't any Stats before, then they obvious changed
        if not old_stats:
            return True
        # Loop through the Dictionary checking if any value has changed
        for key, newval in new_stats.iteritems():
            oldval = old_stats.get(key)
            # Even though the Driver should return Stats as Strings,
            # if they do return any as booleans, the database will
            # treat them as 0/1 Integers, so need to convert to compare
            if isinstance(newval, bool):
                newval = int(newval)
            # If the value has changed, set update to True
            if str(oldval) != str(newval):
                return True
        return False


#######################################################
###########  Override ComputeVirtAPI Class  ###########
#######################################################
class PowerVCComputeVirtAPI(manager.ComputeVirtAPI):
    """Extends the base ResourceTracker class for PowerVC to fix issues"""

    def __init__(self, compute):
        """Constructor for the PowerVC extension to the Virt API."""
        super(PowerVCComputeVirtAPI, self).__init__(compute)

    def host_reconcile_network(self, context, network_topo):
        """Reconciles the new Network Topology into the Database"""
        if network_topo is None:
            network_topo = dom.Host(CONF.host, [])
        self._compute.conductor_api.\
            host_reconcile_network(context, network_topo.to_dictionary())


#######################################################
###########  Monkey Patching                ###########
#######################################################
#
# Monkey patch file locking because it does spin locks
# and gives no indication of troubles.  The result is
# that periodic tasks can hang, if NFS lockd continues
# to return EAGAIN.  Usually restarting lockd helps.
class _Monkey_PosixLock(lockutils._PosixLock):
    def trylock(self):
        tries = 0
        start_time = time.time()
        err_msg = 'Unable to obtain lock for file: %s'
        while True:
            try:
                fcntl.lockf(self.lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except IOError as e:
                tries += 1
                if e.errno in (errno.EACCES, errno.EAGAIN):
                    # Check if we've been trying for quite a while.  Use
                    # both time and tries, in order to avoid time change
                    # issues and yet give it enough tries.
                    if time.time() - start_time > 3600 and tries > 4096:
                        LOG.error(_(err_msg) % self.fname)
                        raise exception.NovaException(message=(_(err_msg)
                                                               % self.fname))
                    # If we think we might have an issue, send a warning
                    if tries % 2048 == 0:
                        LOG.warn(_(err_msg) % self.fname)
                    time.sleep(0.05)
                else:
                    raise


# Inject our own overwritten lock function to avoid spinning forever
setattr(lockutils, 'InterProcessLock', _Monkey_PosixLock)

class PowerVCLocalComputeManager(PowerVCComputeManager):
    """Extends the base Compute Manager class with PowerVC capabilities"""
    def __init__(self, compute_driver=None, *args, **kwargs):
        super(PowerVCLocalComputeManager,self).__init__(compute_driver, *args, **kwargs)

    def _is_ivm_iso_cinder_deploy(self, image_meta):
        """  
        Need to make sure the empty volume creation only happen
        on the IVM ISO deploy with cinder volume scenario.
        """

        if CONF.powervm_mgr_type.lower() == 'ivm':           
            if not image_meta.has_key('meta_version'): 
                return True
            elif image_meta.has_key('meta_version') and image_meta['meta_version'] < 2:       
                return True
        return False

    def _prepare_iso_deploy_boot_volume(self, context, instance, image_meta):
        """  
        This is helper function to create the boot volume
        for ISO deploy backed by cinder volume. For local disk support,
        the disk is not cinder backed and compute virt driver needs
        to create it. Need to get the volume type flavor as well.

        :param context: security context
        :param instance: Instance object
        :param image_meta: Glance image meta data for deploy
        :return: None
        """
        boot_device_size = instance['root_gb']
        if not boot_device_size or boot_device_size <= 0:
            msg = (_("root_gb %(root_gb)s for instance"
                     " %(instance_uuid)s is not valid") %
                   {'root_gb': boot_device_size,
                    'instance_uuid': instance['uuid']})
            raise exception.InvalidInput(reason=msg)

        bootvolname = self._generate_boot_volume_name(instance)

        # volume type will be handled by _prepare_volume_by_image to
        # retrieve it from flavor.
        boot_volume = self._prepare_volume_by_image(
            context, instance, volume_size=boot_device_size,
            display_name=bootvolname, image_id=image_meta['id'],
            bootable=True
        )

        LOG.debug('boot image volume: %(boot_volume)s' % locals())

        values = {'instance_uuid': instance['uuid'],
                  'device_name': '/dev/sda',
                  'snapshot_id': None,
                  'volume_id': None,
                  'volume_size': boot_volume['size'],
                  'no_device': None,
                  'virtual_name': None,
                  'connection_info': None,
                  'image_id': image_meta['id'],
                  'delete_on_termination': True,
                  'volume_id': boot_volume['id'],
                  }

        # if the volume is boot disk. update the default_ephemeral_device
        # and capacity_gb field in the instance.
        args = {'default_ephemeral_device': boot_volume['id'],
                'root_gb': boot_volume['size']}
        self.conductor_api.instance_update(context, instance['uuid'],
                                           **args)

        self.conductor_api.block_device_mapping_update_or_create(context,
                                                                 values)
    def _prepare_volume_by_image(self, context, instance, source_volid=None,
                                 volume_size=None, volume_type=None,
                                 image_id=None, display_name=None,
                                 bootable=True):
        """
        Create volume based on image volume or create an empty volume
        based on volume size.  Handles PowerVC image meta and build BDM
        accordingly before spawn.
        :param context: operation context
        :param instance: instance that is building.
        :param source_volid: image volume id
        :param volume_size: volume size to create an empty volume
        :param volume_type: optional volume type for volume creation. Will get
                            overridden if volume type defined in flavor
        :param image_id: image uuid
        :param display_name: display name of boot volume
        :return: volume that has been created.
        """
        # source_vol and volume_size are mutually exclusive
        if source_volid and volume_size > 0:
            msg = (_('_prepare_volume_by_image(): Invalid input. '
                     'source_vol=%(source_vol)s volume_size=%(volume_size)s'
                     'volume_type=%(volume_type)s'))
            LOG.error(msg)
            raise exception.InvalidInput(reason=msg)

        name = display_name if (display_name and
                                isinstance(display_name, basestring)) else ''
        if bootable:
            # For bootable volume, save some details in the volume meta data
            vol_meta = {'is_boot_volume': "True",
                        'instance_uuid': instance['uuid']}
            if CONF.host_storage_type == 'local':
                vol_meta.setdefault('host',instance['host'])
        else:
            vol_meta = None
        # get the storage template from the flavor.
        voltype_flavor = self._get_volume_type_from_flavor(context, instance)

        vol_type = None
        if source_volid:
            src_vol = self.volume_api.get(context, source_volid)
            if (not src_vol.get('id') or src_vol['id'] != source_volid):
                # The image volume doesn't exist.
                exc_reason = (_("image volume doesn't exist: "
                                "%(source_volid)s") % locals())
                exc_args = {'image_id': image_id,
                            'reason': exc_reason}
                LOG.error(exc_reason)
                raise exception.ImageUnacceptable(**exc_args)

            # input volume_type can overwrite image volume volume_type
            voltype = src_vol['volume_type']
            # volume_api.get() returns string "None" instead of type None
            if voltype and voltype.lower() != "none":
                src_voltype = voltype
            else:
                src_voltype = None

            # The volume type specified in the volume_mapping can
            # overwrite the volume type associated with the image volume
            if voltype_flavor:
                vol_type = voltype_flavor
            elif volume_type:
                vol_type = volume_type
            else:
                vol_type = src_voltype

            if not vol_type:
                vol_type = CONF.host_storage_type
            # create boot volume from image volume.
            # The volume_api is PowerVCVolumeAPI which subclassed
            # from nova volume api class since its create() doesn't
            # take source_vol parameter for volume clone.
            try:
                vol = self.volume_api.create(context, src_vol['size'],
                                             name, '', source_vol=src_vol,
                                             volume_type=vol_type,
                                             metadata=vol_meta)
                details = (_("Started clone boot volume %(volid)s "
                             "from image volume %(srcvolid)s with "
                             "volume type \"%(voltype)s\" for instance "
                             "%(inst)s") %
                           {'volid': vol['id'],
                            'srcvolid': src_vol['id'],
                            'voltype': vol_type,
                            'inst': instance['uuid']})
                LOG.info(details)

            except Exception:
                with excutils.save_and_reraise_exception() as ex:
                    error = ex.value
                    message = (_("Could not clone boot volume "
                                 " from image volume %(volid)s "
                                 "during deploy instance: %(inst)s "
                                 "Error: %(error)s") %
                               {'volid': src_vol['id'],
                                'inst': instance['uuid'],
                                'error': error})
                    LOG.error(message)
        elif volume_size > 0:
            # volume type is default from the image's metadata. It could
            # be overwritten by the volume type in flavor.
            if voltype_flavor:
                vol_type = voltype_flavor
            elif volume_type:
                vol_type = volume_type
            if not vol_type:
                vol_type = CONF.host_storage_type
            # create an empty volume based on the given size
            try:
                # The volume_type here is passed in by caller.
                # for IVM iso deploy, _prepare_iso_deploy_boot_volume
                # gets it from flavor.
                vol = self.volume_api.create(context, volume_size,
                                             name, '', volume_type=vol_type,
                                             metadata=vol_meta)
                details = (_("Started create volume %(volid)s "
                             "with size: %(size)s GB and "
                             "volume type \"%(voltype)s\" for instance "
                             "%(inst)s") %
                           {'volid': vol['id'],
                            'size': volume_size,
                            'voltype': vol_type,
                            'inst': instance['uuid']})
                LOG.info(details)

            except UnboundLocalError:
                self._instance_update(
                        context, instance['uuid'],
                        vm_state=vm_states.ERROR,
                        task_state=None)

                details = (_("Could not create %(size)sGB boot volume"
                             " Error: Not Found %(instance_type_id)s volume type id" %
                             {'size' : volume_size,
                              'instance_type_id' : instance['instance_type_id']}))
                LOG.error(details)
                raise exception.InstanceNotFound(instance_id=instance['uuid'])
            except Exception:
                with excutils.save_and_reraise_exception() as ex:
                    error = ex.value
                    details = (_("Could not create %(size)sGB boot volume "
                                 "during deploy instance %(inst)s"
                                 "Error: %(error)s" %
                                 {'size': volume_size,
                                  'inst': instance['uuid'],
                                  'error': error}))
                    LOG.error(details)
        # check the volume status. If in "error" state stop the
        # the build process.
        volume = self.volume_api.get(context, vol['id'])
        # check newly cloned boot volume status.Bail if in error state.
        if (not volume.get('id') or volume['id'] != vol['id'] or
                'error' in volume['status']):
            error = (_("Health status: %(hstatus)s.") %
                     {'hstatus': volume.get('health_status')})

            reason = (_("Instance %(inst)s boot volume %(volid)s went into "
                        "error state during deploy. Error: %(error)s.") %
                      {'inst': instance['uuid'],
                       'volid': volume['id'],
                       'error': error})
            LOG.error(reason)
            if  not source_volid:
                src_vol['id'] = None
                src_vol['display_name'] = None
            exc_args = {'instname': instance['display_name'],
                        'instuuid': instance['uuid'],
                        'srcvolid': src_vol['id'],
                        'srcvolname': src_vol['display_name'],
                        'volmeta':
                        volume['volume_metadata'].get(
                            'Create Failure description')}

            raise pwrvc_excep.IBMPowerVCBootVolumeCloneFailure(**exc_args)

        message = (_("Instance %(inst)s boot volume %(volid)s create request "
                     "has been submitted to storage successfully. Current "
                     "volume status: %(status)s") %
                   {'inst': instance['uuid'],
                    'volid': volume['id'],
                    'status': volume['status']})
        LOG.info(message)
        return volume

    @logcall
    def _attach_volume(self, context, instance, bdm):
        """
        Extend this method to make sure instance has the required virtual
        adapter and maps to physical fibre channel adapter prior to
        cinder's initialize_connection.
        """
        volume_id = bdm.volume_id
        mountpoint = bdm['mount_device']

        try:
            info = self._attach_volume_osee(context, volume_id, mountpoint,
                                            instance)
        except Exception:
            with excutils.save_and_reraise_exception() as ex:
                # We send an asynchronous notification that attachment failed.
                notify_info = {'instance_id': instance['uuid'],
                               'instance_name': instance['name'],
                               'volume_id': volume_id,
                               'host_name': self.host}
                error = ex.value

                try:
                    # Attempt to gather some more useful information for the
                    # error report.

                    # Volume details
                    vol = self.volume_api.get(context, volume_id)
                    notify_info['volume_display_name'] = vol['display_name']

                    # Storage provider details
                    provider_info = self.volume_api.get_stg_provider_info(
                        context, volume_id=volume_id)
                    update = {'storage_provider_display_name':
                              provider_info['host_display_name'],
                              'storage_provider_id':
                              provider_info['storage_hostname'],
                              'error': error}
                    notify_info.update(update)

                    # UI replaces volume_id with volume_display_name hyperlink.
                    msg = _("Could not attach volume {volume_id} to "
                            "virtual server {instance_name}.  Check that "
                            "host {host_name} has connectivity to storage "
                            "provider {storage_provider_id}, and check the "
                            "host's log files for further details of this "
                            "error: %(error)s.") % {'error': error}

                except Exception as e2:
                    LOG.exception(e2)
                    # If anything goes wrong, fall back to just reporting the
                    # information that we have to hand.  We'd end up here if we
                    # failed due to Cinder being down, for example.
                    msg = _("Could not attach volume {volume_id} to virtual "
                            "server {instance_name}.  Check that host "
                            "{host_name} has connectivity to all storage "
                            "providers.  Check the host's log files for "
                            "further details of this error.")

                notify_info['msg'] = msg
                notifier = rpc.get_notifier(service='volume.%s' % volume_id)
                notifier.error(context, 'volume.update.errors', notify_info)
                notifier = rpc.get_notifier(service='compute', host=self.host)
                notifier.error(context, 'compute.instance.log', notify_info)

        return info

    def _valid_volume_connector(self, connector):
        if not connector:
            return False
        if connector.get('host'):
            return True
        else:
            return False

class PowerVCIscsiComputeManager(PowerVCLocalComputeManager):
    def _valid_volume_connector(self, connector):
        if not connector:
            return False
        if connector.get('host') and connector.get('wwpns'):
            return True
        elif connector.get('host') and connector.get('initiator'):
            return True
        else:
            return False

class PowerVCSanComputeManager(PowerVCLocalComputeManager):
    pass
