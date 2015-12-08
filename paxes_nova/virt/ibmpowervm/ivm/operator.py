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

import contextlib
import decimal
import eventlet
import json
import math
import os
import pipes
import random
import re
import shlex
import socket
import time
import pwd
import shutil

from eventlet import greenthread

from nova import conductor
from nova import context
from nova import utils
from nova import exception as base_exception

from nova.compute import power_state
from nova.compute import vm_states

from nova.image import glance

from nova.context import get_admin_context
from nova.objects import flavor as flavor_obj
from nova.openstack.common import excutils
from nova.openstack.common import importutils
from nova.openstack.common import jsonutils
from nova.openstack.common import log as logging
from nova.openstack.common.lockutils import synchronized

from oslo.config import cfg

from paxes_nova.objects.compute import dom as compute_dom
from paxes_nova.virt.ibmpowervm.common import constants as pvc_const
from paxes_nova.virt.ibmpowervm.common import cache
from paxes_nova.virt.ibmpowervm.common import exception as common_ex
from paxes_nova.virt.ibmpowervm.common import task_state_checker
from paxes_nova.virt.ibmpowervm.common import proc_utils
from paxes_nova import logcall

from paxes_nova.virt.ibmpowervm.ivm import blockdev
from paxes_nova.virt.ibmpowervm.ivm import command
from paxes_nova.virt.ibmpowervm.ivm import common
from paxes_nova.virt.ibmpowervm.ivm import constants
from paxes_nova.virt.ibmpowervm.ivm import exception
from paxes_nova.virt.ibmpowervm.ivm import svc
from paxes_nova.virt.ibmpowervm.ivm import lpar as LPAR
from paxes_nova.virt.ibmpowervm.common.pool import SSHConnectionPool
from paxes_nova.virt.ibmpowervm.vif.ivm.vm_import_ivm \
    import ImportVMNetworkIVM

from nova.virt import configdrive
from paxes_nova.virt.ibmpowervm.common import config_iso

#change for paxes
#from powervc_keystone.encrypthandler import EncryptHandler

from sets import Set
from paxes_nova import _

LOG = logging.getLogger(__name__)

ibmpowervm_opts = [
    cfg.IntOpt('ibmpowervm_live_migration_timeout',
               default=3600,
               help='Timeout in seconds for live migration task.'),
    cfg.IntOpt('ibmpowervm_power_off_timeout',
               default=120,
               help='Timeout in seconds for power off task.'),
    cfg.IntOpt('ibmpowervm_stats_cache_interval',
               default=30,
               help='Interval in seconds for caching host statistics.')
]

ibmpowervmlocalstorage_opts = [
        cfg.BoolOpt('local_or_cinder',
                    default=True,
                    help='when create volume choice local or cinder default cinder'),
        cfg.StrOpt('image_dest_dir',
                   default='/tmp')
]

CONF = cfg.CONF
CONF.register_opts(ibmpowervm_opts)
CONF.register_opts(ibmpowervmlocalstorage_opts)
CONF.import_opt('ibmpowervm_iso_detach_timeout',
                'paxes_nova.compute.manager')
CONF.import_opt('ibmpowervm_rmc_active_timeout',
                'paxes_nova.virt.ibmpowervm.common.task_state_checker')

# Constant for Activating state used post-deploy
ACTIVATING = 'activating'
# Average Image Size
AVG_IMAGE_SIZE = 4 * 1024 * 1024 * 1024  # 4 GBs
# Retry interval
MAX_ATTEMPTS_TO_CACHE_CLEANUP = 3

SUPPORTED = 'supported'
NOT_SUPPORTED = 'not_supported'

# Flavor extra_specs for memory and processor properties
EXPECTED_RESOURCE_SPECS = ['powervm:min_mem',
                           'powervm:max_mem',
                           'powervm:min_vcpu',
                           'powervm:max_vcpu',
                           'powervm:proc_units',
                           'powervm:min_proc_units',
                           'powervm:max_proc_units',
                           'powervm:dedicated_proc',
                           'powervm:uncapped',
                           'powervm:dedicated_sharing_mode',
                           'powervm:processor_compatibility',
                           'powervm:shared_weight',
                           'powervm:availability_priority']

# Other acceptable extra_specs attributes
EXPECTED_OTHER_SPECS = ['powervm:boot_volume_type',
                        'powervm:storage_connectivity_group']

MAXINT32 = (1 << 31) - 1

# Flag for maximum concurrent migrations supported
# Default considering it as 8, _update_host_stats updates it
MAX_SUPP_CONCUR_MIGRATONS = 8


#
# Base operator factory which will return the
# required instance of operator based on
# the op type provided to the factory.
#
class BaseOperatorFactory(object):

    @staticmethod
    def getOperatorInstance(optype, virtapi):
        if optype == 'san':
            return PowerVMSANOperator(virtapi)
        elif optype == 'local':
            return PowerVMLocalDiskOperator(virtapi)
        elif optype == 'iscsi':
            return PowerVMISCSIDiskOperator(virtapi)
        else:
            LOG.exception(_('Unknown Driver type : %s') % optype)
            raise


#
# Base operator class for all the operations.
# All the common operations defined for the
# intended extending class is abstracted in this class.
# The overriden methods inside the base
# operator gives the custom implementation for the IVM and HMC.
#
class PowerVMBaseOperator(object):

    """PowerVM main operator.

    The PowerVMBaseOperator is intended to wrap all operations
    from the driver and handle either IVM or HMC managed systems.
    """

    def __init__(self, virtapi, operator, disk_adapter):
        self._operator = operator
        self._disk_adapter = disk_adapter
        self._initiator = None
        self._hypervisor_hostname = CONF.hypervisor_hostname
        self._host_ip = None
        self._host = CONF.host
        self._cached_stats = cache.Cache(CONF.ibmpowervm_stats_cache_interval)
        self._virtapi = virtapi
        self._fc_wwpns = None

        vif_conf = CONF.powervm_vif_driver
        self._vif = importutils.import_object(vif_conf, CONF.host,
                                              self._operator)

        self._source_migrate = []
        self._dest_migrate = []

    @logcall
    def _list_managed_instances(self):
        """
        Returns a list of all instances from database belonging to host
        """
        ctxt = context.get_admin_context()
        instfact = compute_dom.ManagedInstanceFactory.get_factory()
        return instfact.find_all_instances_by_host(ctxt, self._host)

    def _list_unmanaged_instances(self, managed_insts_list=None):
        """
        Returns a list of LPAR names that reside on the host but
        are not being tracked
        """
        all_instances = set(self.list_instances())
        managed_instances = set()
        if managed_insts_list is None:
            managed_insts_list = self._list_managed_instances()
        for instance in managed_insts_list:
            managed_instances.add(instance['name'])
        return all_instances - managed_instances

    def _list_managed_instance_names(self, managed_instances=None):
        """
        Return a list of just instance names from database belonging to host
        """
        if managed_instances is None:
            managed_instances = self._list_managed_instances()
        return [inst['name'] for inst in managed_instances]

    def _parse_vios_lpar_ids(self, lpar_list):
        """Helper method to parse the list of LPAR ID's for the VIOS's"""
        vios_ids = list()
        for lpar in lpar_list:
            if lpar['lpar_env'] == 'vioserver':
                vios_ids.append(lpar['lpar_id'])
        return ','.join(vios_ids)

    def interogate_lpar_storage(self, vscsi_storage_info, npiv_storage_info,
                                hostuuid):
        """
        Return supported if vssci or logical volume is used
        vol_support = {'status': 'supported' , 'reason':''}
        """
        status = SUPPORTED
        reasons = []
        inst_hex = '%#010x' % int(hostuuid)
        is_npiv_storage = None
        is_vscsi_storage = None
        if vscsi_storage_info is not None:
            is_vscsi_storage = vscsi_storage_info.get(inst_hex)
        if npiv_storage_info is not None:
            is_npiv_storage = npiv_storage_info.get(inst_hex)

        if is_npiv_storage is not None:
            status = NOT_SUPPORTED
            reasons.append(_('Npiv not supported'))

        if is_vscsi_storage is None:
            status = NOT_SUPPORTED
            reasons.append(_('This virtual machine is not a candidate for'
                             ' management because it has no volumes attached'
                             ' to it.  You can only manage virtual machines'
                             ' that have at least a boot volume attached.'))

        if is_vscsi_storage is not None:
            # stor_type = is_vscsi_storage.pop()
            stor_type = self._operator.get_stor_type(is_vscsi_storage)
            # For lv it starts with 'sas'
            # for physical it starts with 'fsc'
            # for local storage exclude storage starts with fsc
            if stor_type is None:
                status = NOT_SUPPORTED
                reasons.append(_('This virtual machine is not a candidate for'
                                 ' management because no storage type found. '
                                 'You can only manage virtual machines that '
                                 'are backed by storage.'))
            elif CONF.host_storage_type.lower() == 'local':
                if stor_type[:3] == 'fsc':
                    status = NOT_SUPPORTED
                    reasons.append(_('This virtual machine is not a candidate '
                                     'for management because it is a SAN '
                                     'backed virtual machine. The associated'
                                     ' host uses local storage. Mixed storage'
                                     ' types are not supported.'))
            # for san storage exclude storage starts with sas
            elif CONF.host_storage_type.lower() == 'san':
                if stor_type[:3] == 'sas' or stor_type[:2] == 'lv':
                    status = NOT_SUPPORTED
                    reasons.append(_('This virtual machine is not a candidate '
                                     'for management because it is backed by '
                                     'local storage. The associated host uses '
                                     'SAN storage. Mixed storage types are not'
                                     ' supported.'))

        return {'status': status,
                'reasons': reasons}

    def interogate_lpar_misc(self, lpar_info):
        """
        Check various partition properties and resources to make sure the
        configuration is supported by PowerVC.
        - check for PartitionType - Only support AIX/Linux
        - check for SharedProcPoolID - Only default pool
        - check for SharedMemmoryEnabled - Make sure it's false
        """
        status = SUPPORTED
        reasons = []

        if lpar_info.get('curr_shared_proc_pool_id') != 0:
            status = NOT_SUPPORTED
            reasons.append(_('This virtual machine is not a candidate for '
                             'management because it is configured to use a '
                             'shared processor memory pool other than the '
                             'default.  You can only manage virtual machines '
                             'that use the default shared pool.'))

        shared_mem = lpar_info.get('mem_mode')
        if shared_mem != 'ded':
            status = NOT_SUPPORTED
            reasons.append(_('This virtual machine is not a candidate for '
                             'management because it is configured to use a '
                             'shared processor memory pool other than the '
                             'default.  You can only manage virtual machines '
                             'that use the default shared pool.'))
        return {'status': status,
                'reasons': reasons}

    def list_lpar_for_onboard(self):
        """
        Returns a list of instances to be on-boarded excluding the VIOS
        lpar.
        1.The network associated with the lpar is validated as
        supported or not supported using the ImportVMNetworkIVM class.
        2.Volumes associated with the lpar are checked for vscsi and npiv and
        marked as not supported in case of npiv.
        Returns : onboard_list = [{'name': 'nova-01'
                                   'state': 'Running'
                                   'hostuuid': '2'
                                   'support': {'status': 'supported'
                                               'reasons': ''}
                                   }]
        """
        all_lpars = self._operator.list_all_lpars()
        LOG.info(_("list of lpars for on board :  %s ") % all_lpars)

        vscsi_storage_info = self._operator.get_vscsi_storage_info()
        npiv_storage_info = self._operator.get_npiv_storage_info()
        lpar_misc_info = self._operator.get_misc_lpar_info()
        vios_ids = self._parse_vios_lpar_ids(all_lpars)

        vif_import_driver = ImportVMNetworkIVM(self._operator)
        onboard_list = []
        for lpar in all_lpars:
            if lpar['lpar_env'] != 'vioserver':
                status = SUPPORTED
                reasons = []
                hostuuid = lpar['lpar_id']
                state = lpar['state']
                power_specs = {
                    'vm_id': lpar['lpar_id'],
                    'vm_name': lpar['name'],
                    'vios_ids': vios_ids
                }
                onboard_inst = {'name': lpar['name'],
                                'state': constants.IBM_POWERVM_POWER_STATE.get(
                                    state, power_state.NOSTATE),
                                'power_specs': power_specs}

                if lpar['lpar_env'] != 'aixlinux':
                    status = NOT_SUPPORTED
                    reasons.append(_('This virtual machine is not a candidate'
                                     ' for management because it is configured'
                                     ' for an IBM i operating system.  You can'
                                     ' only manage virtual machines configured'
                                     ' for AIX or Linux operating systems.'))

                LOG.debug('network import')
                vif_reasons = vif_import_driver.interogate_lpar(hostuuid)
                if vif_reasons is not None and len(vif_reasons) > 0:
                    status = NOT_SUPPORTED
                    reasons += vif_reasons
                LOG.debug('network import end %s' % vif_reasons)

                LOG.debug('volume import')
                storage_result = self.interogate_lpar_storage(
                    vscsi_storage_info, npiv_storage_info, hostuuid)

                if storage_result.get('status') == NOT_SUPPORTED:
                    status = NOT_SUPPORTED
                    reasons += storage_result.get('reasons')
                LOG.debug('volume import end')

                LOG.debug('lpar misc import end')
                lpar_misc_res = self.interogate_lpar_misc(
                    lpar_misc_info[hostuuid])
                if lpar_misc_res.get('status') == NOT_SUPPORTED:
                    status = NOT_SUPPORTED
                    reasons += lpar_misc_res.get('reasons')
                LOG.debug('lpar misc import end')

                onboard_inst['support'] = {'status': status,
                                           'reasons': reasons}

                onboard_list.append(onboard_inst)

        return onboard_list

    @logcall
    def query_onboarding_instances(self, context, instances):
        """
        Generate the data needed by the discovery_driver.query_instances
        for onboarding.
            uuid:      The UUID of the VM when created thru OS
            name:      The Name of the VM defined on the Hypervisor
            state:     The State of the VM, matching the power_state definition
            hostuuid:  The Hypervisor-specific UUID of the VM (Optional)
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
            network_ports: List of dictionary objects with the network ports:
              mac_address:  The MAC Address of the Port allocated to the VM
              status:       The Status of the Port, matching the definition
              segmentation_id: The VLAN ID for the Network the Port is part of
              physical_network: The Physical Network the Port is part of

        """
        ret_list = []
        # rakatipa: construct a dictionary for name uuid lookup
        # name_uuid_lookup = dict((inst['name'], inst['uuid'])
        #                       for inst in instances)
        lpar_data_dict = {}

        vif_import_driver = ImportVMNetworkIVM(self._operator)
        # [17723] Preparing storage infor dicts for all the vms on the host
        disk_dict, vhost_dict = self._operator.prepare_vhost_dev_lun_dict()
        for inst in instances:
            lpar_info_new = {}
            volumes = []
            ports = []
            flavor = {}

            lpar_id = inst['power_specs']['vm_id']
            instance_name = inst['power_specs']['vm_name']
            lpar_info = self._operator.get_lpar(instance_name, 'lpar')

            # The instance id on host
            lpar_info_new['power_specs'] = inst['power_specs']
            lssyscfg_state = lpar_info['state']
            state = constants.IBM_POWERVM_POWER_STATE.get(lssyscfg_state,
                                                          power_state.NOSTATE)
            LOG.debug("Lpar state is %s " % lssyscfg_state)
            lpar_info_new['state'] = state
            lpar_info_new['name'] = instance_name
            lpar_info_new['architecture'] = 'ppc64'

            vcpu_info = self._operator.get_vcpu_info(instance_name)
            lpar_info_new['vcpus'] = vcpu_info['curr_procs']
            flavor['vcpus'] = vcpu_info['pend_procs']
            flavor['powervm:proc_units'] = vcpu_info['pend_proc_units']

            mem_info = self._operator.get_memory_info_lpar(instance_name)
            lpar_info_new['memory_mb'] = mem_info['curr_mem']
            flavor['memory_mb'] = mem_info['pend_mem']

            lpar_info_new['flavor'] = flavor
            # Volumes
            instance_hex_id = '%#010x' % int(lpar_id)
            vhost_id = vhost_dict[instance_hex_id]
            # Getting volume info by running through the dicts
            volumes = self._operator.get_volumes_by_vhost_from_dict(vhost_id,
                                                                    disk_dict)
            lpar_info_new['volumes'] = volumes
            # rakatipa : calculating root_gb using disk name and root_gb
            # and ephemeral_gb are equal.
            # (TODO)  to evaluate both and differentiate both
            root_gb = self._operator.get_root_gb_disk_from_dict(vhost_id,
                                                                disk_dict)
            lpar_info_new['root_gb'] = root_gb
            lpar_info_new['ephemeral_gb'] = root_gb
            # root_gb and ephemeral_gb in flavor is empty as
            # the pendin gb will be available after on-boarding
            flavor['root_gb'] = root_gb
            flavor['ephemeral_gb'] = root_gb

            # Ports
            vif_ports = vif_import_driver.list_ports(lpar_id)
            for port in vif_ports:
                # extract the info we needed
                ports.append({'mac_address': port.get('mac_address'),
                              'status': port.get('status'),
                              'vswitch': port.get('vswitch'),
                              'segmentation_id':
                              port.get('provider:segmentation_id'),
                              'physical_network':
                              port.get('physical_network')})
            lpar_info_new['ports'] = ports

            lpar_data_dict[lpar_id] = lpar_info_new

        for val in lpar_data_dict.itervalues():
            ret_list.append(val)

        return ret_list

    def get_cpu_utilization_of_instances(self, instances):

        data = []
        for inst in instances:
            name = inst['name']
            uuid = inst['uuid']
            lpar_id = self._get_instance(name)['lpar_id']
            LOG.debug("Data for lookup: %s" % (str(name)))
            try:
                cpu_util = self._operator.get_lpar_cpu_util(lpar_id)
                data.append({'uuid': uuid,
                             'name': name,
                             'cpu_utilization': cpu_util})
            except exception.IBMPowerVMCommandFailed:
                LOG.debug("CPU data not found for: " + str(inst))

        return data

    @logcall
    def get_info(self, instance_name):
        """Get the current status of an LPAR instance.

        Returns a dict containing instance information:

        :raises: PowerVMLPARInstanceNotFound
        """
        lpar = self._get_instance(instance_name)
        lssyscfg_state = lpar['state']
        state = constants.IBM_POWERVM_POWER_STATE.get(
            lssyscfg_state, power_state.NOSTATE)
        LOG.debug("Lpar state is %s " % lssyscfg_state)

        mem = self._operator.get_lpar_mem(instance_name)
        proc = self._operator.get_lpar_proc(instance_name)
        try:
            lpar_attrs = self._operator.get_lpar_info(lpar['lpar_id'])
        except exception.IBMPowerVMCommandFailed:
            lpar_attrs = None

        if lssyscfg_state and \
           lssyscfg_state == constants.IBM_POWERVM_MIGRATING_RUNNING:
            # check RMC state
            # only when LPAR is Running
            migrating = True
        else:
            migrating = False

        info = {'state': state,
                'instance_migrating': migrating,
                'max_mem': mem['curr_max_mem'],
                'mem': mem['curr_mem'],
                'cpu_time': lpar['uptime']
                }

        if lpar_attrs is None:
            info['num_cpu'] = proc['curr_procs']
        else:
            info['num_cpu'] = lpar_attrs['curr_procs']

        return info

    @logcall
    def get_power_specs(self, instance_name):
        """Get the current status of an LPAR instance.

        Returns a dict containing instance information:

        :raises: PowerVMLPARInstanceNotFound
        """
        lpar = self._get_instance(instance_name)
        rmc_state = None
        lssyscfg_state = lpar['state']
        state = constants.IBM_POWERVM_POWER_STATE.get(
            lssyscfg_state, power_state.NOSTATE)

        mem = self._operator.get_lpar_mem(instance_name)
        proc = self._operator.get_lpar_proc(instance_name)
        ret_compat_proc = self._operator.\
            get_curr_and_desired_proc_compat_modes(instance_name)
        if ret_compat_proc is None:
            curr_compat_proc = None
            desired_compat_proc = None
        else:
            curr_compat_proc = ret_compat_proc[0]
            desired_compat_proc = ret_compat_proc[1]
        # [Task 10866]
        # Adding new attribute operating_system as part of power_spec
        # [Issue 17441] Deleting operating_system attribute from power_specs
        # as currently IVM has limitation to update installed
        # operating system. Keeping the get_lpar_operating_system as it is,
        # If IVM defect is fixed we can use it later.

        lpar_operating_system = self._operator.\
            get_lpar_operating_system(instance_name)
        if lpar_operating_system == '0.0.0.0.0.0':
            lpar_operating_system = 'Unknown'
        if lssyscfg_state and lssyscfg_state == constants.IBM_POWERVM_RUNNING:
            # check RMC state only when LPAR is Running
            dlpar, rmc_state = self._operator.\
                check_dlpar_connectivity(instance_name)

        # Note: In power_specs dict 'proc_units' represents
        # processor units, and 'vcpus' represents processors.
        power_specs = {'min_proc_units': proc['curr_min_proc_units'],
                       'max_proc_units': proc['curr_max_proc_units'],
                       'pending_proc_units': proc['pend_proc_units'],
                       'pending_min_proc_units': proc['pend_min_proc_units'],
                       'pending_max_proc_units': proc['pend_max_proc_units'],
                       'run_proc_units': proc['run_proc_units'],
                       'min_vcpus': proc['curr_min_procs'],
                       'max_vcpus': proc['curr_max_procs'],
                       'pending_vcpus': proc['pend_procs'],
                       'pending_min_vcpus': proc['pend_min_procs'],
                       'pending_max_vcpus': proc['pend_max_procs'],
                       'run_vcpus': proc['run_procs'],
                       'min_memory_mb': mem['curr_min_mem'],
                       'max_memory_mb': mem['curr_max_mem'],
                       'pending_memory_mb': mem['pend_mem'],
                       'pending_min_memory_mb': mem['pend_min_mem'],
                       'pending_max_memory_mb': mem['pend_max_mem'],
                       'run_memory_mb': mem['run_mem'],
                       'rmc_state': rmc_state,
                       'power_state': state,
                       'vm_id': lpar['lpar_id'], 'vm_name': instance_name,
                       'current_compatibility_mode': curr_compat_proc,
                       'desired_compatibility_mode': desired_compat_proc,
                       }
        try:
            lpar_attrs = self._operator.get_lpar_info(lpar['lpar_id'])
        except exception.IBMPowerVMCommandFailed:
            lpar_attrs = None

        if lpar_attrs is None:
            power_specs['vcpu_mode'] = proc['curr_proc_mode']
            power_specs['proc_units'] = proc['curr_proc_units']
            power_specs['vcpus'] = proc['curr_procs']
            power_specs['memory_mb'] = mem['curr_mem']
            power_specs['memory_mode'] = mem['mem_mode']
        else:
            power_specs['vcpu_mode'] = lpar_attrs['curr_proc_mode']
            power_specs['proc_units'] = lpar_attrs['curr_proc_units']
            power_specs['vcpus'] = lpar_attrs['curr_procs']
            power_specs['memory_mb'] = lpar_attrs['curr_mem']
            power_specs['memory_mode'] = lpar_attrs['mem_mode']

        # Patch up any values that need changing
        if power_specs['vcpu_mode'] == 'ded':
            power_specs['vcpu_mode'] = 'dedicated'
        if power_specs['memory_mode'] == 'ded':
            power_specs['memory_mode'] = 'dedicated'

        # Set the flag to say that the LPAR is pending a reboot if the
        # Desired Compatibility Mode is different than the Current Mode
        if curr_compat_proc and desired_compat_proc:
            if desired_compat_proc != 'default':
                if curr_compat_proc != desired_compat_proc:
                    power_specs['pending_action'] = 'awaiting_restart'
        return power_specs

    @logcall
    def refresh_lpar_name_map(self, context, instances):
        """Refreshes the Map of OpenStack VM Names to the actual LPAR Names"""
        self._operator.refresh_lpar_name_map(instances)

    @logcall
    @synchronized('cached_stats', 'pvm-')
    def _get_cached_stats(self):
        """Return currently known host stats."""

        LOG.debug("Getting currently known host stats from the cache...")
        static_stats = self._get_static_stats()
        stats = self._cached_stats.get_data()
        if stats is None:
            try:
                stats = self._update_host_stats()
                self._cached_stats.set_data(stats)
            except (exception.IBMPowerVMCommandFailed) as e:
                # TODO : check if scheduler is ok with old statss
                # in case of error return the previously stored stats
                LOG.exception(_("Update of host statistics failed: %s") %
                              e)
                stats = self._cached_stats.get_old_data()
                # Don't raise exception in get_host_stats if svc command errors
            except (svc.SVCCommandException) as e:
                LOG.exception(_("Update of host statistics failed: %s") %
                              e)
                stats = self._cached_stats.get_old_data()
                # Place power state of the lpars into "no state"
                # when the host connection is lost
            except (exception.IBMPowerVMConnectionFailed) as conn_ex:
                stats = self._cached_stats.get_old_data()
                LOG.exception(_("Connection to host failed. %s" %
                                conn_ex))
                lpars = self._list_managed_instances()
                for lpar in lpars:
                    LOG.debug("Placing power state of lpar %s into pending." %
                              lpar['name'])
                    conductor.API().instance_update(
                        context.get_admin_context(), lpar['uuid'],
                        power_state=power_state.NOSTATE,
                        task_state=None)
            except Exception as unexpected_ex:
                LOG.exception(_("host statistics failed unexpectedly: %s")
                              % unexpected_ex)
                raise
        stats.update(static_stats)
        LOG.debug("Currently known host stats: %s" % str(stats))
        return stats

    def get_host_cpu_frequency(self):
        return self._operator.get_host_cpu_frequency()

    def get_host_cpu_util_data(self):
        return self._operator.get_host_cpu_util_data()

    def get_host_stats(self, refresh=False):
        # host_stats = super(PowerVMOperator, self).get_host_stats(refresh)
        host_stats = self._get_cached_stats()
        return host_stats

    def _extract_old_host_stats(self, set_host_error=True):
        """Get the existing host stats. This method is called when the
        get_host_stats method fails to obtain new stats. Otherwise,
        the get_available_resource method fails, and the stats aren't
        updated.
        :param set_host_error: if True, put the host into error state.

        returns: the old host stats
        """
        host_stats = self._cached_stats.get_data()
        if host_stats is None:
            host_stats = {}
            host_stats['stats'] = {}
            host_stats['hypervisor_hostname'] = self._hypervisor_hostname

        if set_host_error:
            host_stats['stats']['hypervisor_state'] = 'error'
        return host_stats

    def _get_static_stats(self):
        data = self._cached_stats.get_static_data()

        if data is None:
            data = {}
            data['hypervisor_type'] = pvc_const.IBM_POWERVM_HYPERVISOR_TYPE
            data['hypervisor_version'] = (
                pvc_const.IBM_POWERVM_HYPERVISOR_VERSION)
            data['supported_instances'] = (
                pvc_const.POWERVM_SUPPORTED_INSTANCES)
            data['hypervisor_hostname'] = self._operator.get_hostname()

            # if hypervisor hostname has changed, update configuration file
            if self._hypervisor_hostname != data['hypervisor_hostname']:
                self._hypervisor_hostname = data['hypervisor_hostname']
                utils.execute('/usr/bin/openstack-config',
                              '--set', CONF.config_file[0], 'DEFAULT',
                              'hypervisor_hostname', self._hypervisor_hostname)

            self._cached_stats.set_static_data(data)

        return data

    def _update_host_stats(self):
        memory_info = self._operator.get_memory_info()
        cpu_info = self._operator.get_cpu_info()
        lpar_memory_info = self._operator.get_memory_info_for_lpars()
        lpar_cpu_info = self._operator.get_cpu_info_for_lpars()
        compatibility_modes = self._operator.get_lpar_proc_compat_modes()

        # Note: disk avail information is not accurate. The value
        # is a sum of all Volume Groups and the result cannot
        # represent the real possibility. Example: consider two
        # VGs both 10G, the avail disk will be 20G however,
        # a 15G image does not fit in any VG. This can be improved
        # later on.
        disk_info = self._operator.get_disk_info()

        # Hypervisor requires additional memory buffer to create lpar.
        memory_buffer = min(constants.POWERVM_MEM_BUFFER,
                            memory_info['avail_mem'])
        memory_reserved = memory_info['sys_firmware_mem'] + memory_buffer
        proc_units_reserved = 0
        powervc_memory_mb_used = 0
        powervc_proc_units_used = 0

        # extract the list of managed instance
        managed_inst_list = self._list_managed_instances()
        for lpar in self._list_unmanaged_instances(managed_inst_list):
            lpar = self._operator.get_actual_lpar_name(lpar)
            if lpar in lpar_cpu_info:
                proc_units_reserved += lpar_cpu_info[lpar]
            if lpar in lpar_memory_info:
                memory_reserved += lpar_memory_info[lpar]

        managed_names = self._list_managed_instance_names(managed_inst_list)
        for lpar in managed_names:
            lpar = self._operator.get_actual_lpar_name(lpar)
            if lpar in lpar_cpu_info:
                powervc_proc_units_used += lpar_cpu_info[lpar]
            if lpar in lpar_memory_info:
                powervc_memory_mb_used += lpar_memory_info[lpar]

        data = {}
        data['vcpus'] = cpu_info['total_procs']
        data['proc_units_used'] = '%.2f' % powervc_proc_units_used
        data['vcpus_used'] = math.ceil(float(data['proc_units_used']))
        data['proc_units'] = '%.2g' % data['vcpus']
        data['proc_units_reserved'] = '%.2f' % proc_units_reserved
        data['cpu_info'] = constants.IBM_POWERVM_CPU_INFO
        data['host_memory_total'] = memory_info['total_mem']
        data['host_memory_free'] = memory_info['avail_mem']
        data['host_memory_reserved'] = memory_reserved
        data['memory_mb_used'] = powervc_memory_mb_used
        data['lmb_size'] = memory_info['mem_region_size']

        data['max_vcpus_per_aix_linux_lpar'] = \
            cpu_info['max_vcpus_per_aix_linux_lpar']
        data['max_procs_per_aix_linux_lpar'] = \
            cpu_info['max_procs_per_aix_linux_lpar']
        data['disk_total'] = disk_info['disk_total']
        data['disk_used'] = disk_info['disk_used']
        data['disk_available'] = disk_info['disk_avail']
        # data['initiator'] = self._disk_adapter.get_initiator()

        data['host_storage_type'] = CONF.host_storage_type
        data['usable_local_mb'] = disk_info['usable_local_mb']
        data['extras'] = ''

        # VIOS virtual slots information
        data['vios_max_virtual_slots'] = \
            self._operator.get_vios_max_virt_slots()

        (data['num_vios_virtual_slots_reserved'],
         data['num_vios_virtual_slots_in_use']) = \
            (self._operator.get_num_reserved_in_use_vios_slots(managed_names))

        data['capabilities'] = self._operator.get_hyp_capability()
        data['compatibility_modes'] = ",".join(compatibility_modes)

        # LPM capability constants
        actv_lpm_capable = data['capabilities'].get(
            pvc_const.ACTIVE_LPAR_MOBILITY_CAPABLE)
        inactv_lpm_capable = data['capabilities'].get(
            pvc_const.INACTIVE_LPAR_MOBILITY_CAPABLE)

        # Get the migration stats
        # inactv_migr_sup,actv_migr_supp,inactv_migr_prg,actv_migr_prg
        if actv_lpm_capable:
            mig = self._operator.get_actv_migration_stats()
            data['active_migrations_supported'] = mig['actv_migr_supp']
            data['preferred_active_migrations_supported'] = mig[
                'actv_migr_supp']
            data['active_migrations_in_progress'] = mig['actv_migr_prg']
            MAX_SUPP_CONCUR_MIGRATONS = mig['actv_migr_supp']
        else:
            data['active_migrations_supported'] = 0
            data['preferred_active_migrations_supported'] = 0
            data['active_migrations_in_progress'] = 0
            MAX_SUPP_CONCUR_MIGRATONS = 0

        if inactv_lpm_capable:
            mig = self._operator.get_inactv_migration_stats()
            data['inactive_migrations_supported'] = mig['inactv_migr_sup']
            data['preferred_inactive_migrations_supported'] = mig[
                'inactv_migr_sup']
            data['inactive_migrations_in_progress'] = mig[
                'inactv_migr_prg']
        else:
            data['inactive_migrations_supported'] = 0
            data['preferred_inactive_migrations_supported'] = 0
            data['inactive_migrations_in_progress'] = 0

        data['source_host_for_migration'] = self.is_source_host_for_migration()

        stats = {}
        stats[pvc_const.HYPERVISOR_STATE] = 'operating'
        stats[pvc_const.MANAGED_BY] = 'IVM'
        stats[pvc_const.ACTIVE_LPAR_MOBILITY_CAPABLE] = actv_lpm_capable
        stats[pvc_const.INACTIVE_LPAR_MOBILITY_CAPABLE] = inactv_lpm_capable

        stats['host_storage_type'] = CONF.host_storage_type
        stats['usable_local_mb'] = disk_info['usable_local_mb']
        # Stats should only contain a specific list
        # of attributes as defined in COMPUTE_STATS
        # If additional properties should be in 'stats',
        # then those properties must be added to the
        # COMPUTE_STATS list
        for key in pvc_const.COMPUTE_STATS:
            if key in data:
                stats[key] = data[key]
            else:
                LOG.warn(_("While updating host stats, key '%s' not found "
                           "in data dictionary.") % key)

        data['stats'] = stats

        return data

    @logcall
    def get_available_resource(self):
        """Retrieve resource info.

        :returns: dictionary containing resource info
        """
        data = self._get_cached_stats()

        # Extract hypevisor hostname
        # We need to handle situation where VIOS is down.
        # Otherwise, an exception would occurr during
        # extraction of hypervisor_hostname and hypervisor_state
        # will not be updated by the periodic task
        try:
            hyp_hostname = self._operator.get_hostname()
        except Exception as e:
            hyp_hostname = data.get('hypervisor_hostname')
            if hyp_hostname is None:
                raise

        stats = data['stats']

        # Handle the case where VIOS is down and data returned from
        # get_host_stats only has hypervisor_hostname set and
        # hypervisor_state set to error.
        if len(stats) == 1 and stats['hypervisor_state'] == 'error':
            resource_dict = {'hypervisor_hostname': hyp_hostname,
                             'stats': stats}
            return resource_dict

        # Memory data is in MB already.
        memory_mb_used = (data['host_memory_total'] -
                          data['host_memory_free'])

        if CONF.host_storage_type == 'local' or \
            CONF.host_storage_type == 'san' or \
            CONF.host_storage_type == 'iscsi':
            # Convert to GB
            local_gb = data['disk_total'] / 1024
            local_gb_used = data['disk_used'] / 1024
        else:
            # Issue 6960. These stats aren't used, but still need to
            # return since resource tracker expects them. Return a large
            # value (max int32) for local_gb so warnings are not logged.
            local_gb = MAXINT32
            local_gb_used = 0
        LOG.debug("updating local_gb:%s & local_gb_used:%s" % (local_gb,
                                                               local_gb_used))

        resource_dict = {'vcpus': data['vcpus'],
                         'memory_mb': data['host_memory_total'],
                         'local_gb': local_gb,
                         'vcpus_used': data['vcpus_used'],
                         'memory_mb_used': memory_mb_used,
                         'local_gb_used': local_gb_used,
                         'hypervisor_type': data['hypervisor_type'],
                         'hypervisor_version': data['hypervisor_version'],
                         'hypervisor_hostname': hyp_hostname,
                         'cpu_info': data['cpu_info'],
                         'disk_available_least': data['disk_total'],
                         'supported_instances': jsonutils.dumps(
                             data['supported_instances']),
                         'stats': stats}

        return resource_dict

    @logcall
    def destroy(self, instance_name, destroy_disks=True):
        """Destroy (shutdown and delete) the specified instance.

        :param instance_name: Instance name.
        """
        try:
            self._cleanup(instance_name, destroy_disks)
        except exception.IBMPowerVMLPARInstanceNotFound:
            LOG.warn(_("During destroy, LPAR instance '%s' was not found on "
                       "PowerVM system.") % instance_name)

    @logcall
    def power_off(self, instance_name,
                  timeout=CONF.ibmpowervm_power_off_timeout):
        self._operator.stop_lpar(instance_name, force_immediate=False,
                                 timeout=timeout)

    @logcall
    def power_on(self, instance_name):
        self._operator.start_lpar(instance_name)

    def get_lpar_state(self, instance_name):
        lpar = self._operator.get_lpar(instance_name)
        if lpar is None:
            LOG.error(_('Virtual machine cannot be found. The '
                        'operation cannot proceed.'
                        'LPAR name: %(lpar_name)s. ')
                      % {'lpar_name': instance_name})
            raise exception.IBMPowerVMLPARInstanceNotFound(
                instance_name=instance_name)
        return lpar['state']

    @logcall
    def power_off_on(self, ctx, instance, network_info, reboot_type,
                     block_device_info=None, bad_volumes_callback=None):
        time_out = CONF.ibmpowervm_power_off_timeout
        instance_name = instance['name']
        """Restart the specified instance.
        :param ctx: Context.
        :param instance: Instance object as returned by DB layer.
        :param network_info:
           :py:meth:`~nova.network.manager.NetworkManager.get_instance_nw_info`
        :param reboot_type: Either a HARD or SOFT reboot
        :param block_device_info: Info pertaining to attached volumes
        :param bad_volumes_callback: Function to handle any bad volumes
            encountered
        """

        if reboot_type.lower() == 'soft':
            # Get the LPAR Name if the OpenStack Name is different than it
            self._operator.stop_lpar(instance_name, False, time_out, True)
            # Starting the lpar after proper shutdown
            self.start_instance(instance)

        elif reboot_type.lower() == 'hard':
            LOG.debug("Forcing an immediate restart for %s. " % instance_name)
            cmd = ('chsysstate -r lpar -o shutdown --immed --restart -n %s'
                   % instance_name)
            self._operator.run_vios_command(cmd)

    def macs_for_instance(self, instance):
        return self._operator.macs_for_instance(instance)

    def validate_extraspecs_attr(self, extra_specs, instance_name):
        # Validate the extra spec keys
        for key in extra_specs.keys():
            # If the extra spec does not begin with powervm, ignore it
            if not key.startswith('powervm:'):
                continue
            # If the extra spec is a powervm extra spec, make sure it is
            # an attribute we support
            if key not in EXPECTED_RESOURCE_SPECS + EXPECTED_OTHER_SPECS:
                ex_args = {'key': key,
                           'instance_name': instance_name}
                LOG.error(_("Invalid attribute name '%(key)s' was passed"
                            " in as part of flavor extra specs for"
                            " virtual machine '%(instance_name)s'.")
                          % ex_args)
                raise exception.IBMPowerVMInvalidExtraSpec(**ex_args)

    @logcall
    def _create_lpar_instance(self, instance, network_info, host_stats=None,
                              resource_deltas=None, extra_specs=None):
        """Translates an OpenStack instance info object into an LPAR object
        :param instance: instance info dict
        :param network_info: network info dict
        :param host_stats: host stats dict
        :param resource_deltas: resource delta dict, populated only during
            resize operation. Used for checking availbilit of host resources
        :param extra_specs: extra_specs attribute of instance_type object.
        """
        LOG.debug("Translating an OpenStack instance info object "
                  "into an LPAR object")
        inst_name = instance['name']
        LOG.debug("Instance name: %s" % inst_name)
        lpar_name = self._operator.get_actual_lpar_name(inst_name)
        LOG.debug("Actual LPAR name: %s" % lpar_name)
        if extra_specs is None:
            LOG.debug("Extra specs are not provided")
            inst_type = flavor_obj.Flavor.get_by_id(
                context.get_admin_context(),
                instance['instance_type_id'])
            extra_specs = inst_type['extra_specs']
        LOG.debug("Extra specs: %s" % str(extra_specs))

        self.validate_extraspecs_attr(extra_specs, inst_name)

        if host_stats is None:
            LOG.debug("Host stats are not provided. Getting the host stats")
            host_stats = self.get_host_stats()

        # Memory
        mem = instance['memory_mb']
        mem_min = constants.IBM_POWERVM_MIN_MEM

        LOG.debug("Requested Memory: desired memory=%(mem)d, "
                  "minimum memory=%(mem_min)d" % locals())

        # Use maximum memory multiplier dictionary to calculate
        # maximum memory based on requested memory
        for range, mult in constants.MAX_MEM_MULTIPLIER_DICT.items():
            if mem >= range[0] and mem <= range[1]:
                mem_max = min((mem * mult), host_stats['host_memory_total'])
                break
        LOG.debug("Maximum memory=%(mem_max)d" % locals())
        # If this is a resize operation, don't change max memory unless
        # desired memory is increased above current max memory
        proc_compat_mode = None
        if resource_deltas is not None:
            system_meta = (instance['system_metadata'])
            proc_compat_mode = resource_deltas['desired_proc_compat']
            try:
                curr_max_mem = int(system_meta['max_memory_mb'])
                if mem < curr_max_mem:
                    mem_max = curr_max_mem
                    LOG.debug("Maximum memory adjusted to "
                              "%(mem_max)d" % locals())
            except KeyError:
                # Handle the case where max_memory_mb is not in instance
                # In this case, value of mem_max comes from above.
                pass

        # CPU
        cpus_min = constants.IBM_POWERVM_MIN_CPUS
        cpus_max = int(host_stats['vcpus'])
        cpus = int(instance['vcpus'])
        cpus_units_min = constants.POWERVM_MIN_PROC_UNITS
        proc_units = extra_specs.get('powervm:proc_units')
        if proc_units is None:
            proc_units = proc_utils.get_num_proc_units(cpus)
            cpus_units = ('%.2f' % proc_units)
        else:
            cpus_units = (proc_units)
        extra_specs['powervm:proc_units'] = cpus_units
        desr_proc_compat_mode = extra_specs.\
            get('powervm:processor_compatibility')
        if proc_compat_mode is not None:
            desr_proc_compat_mode = proc_compat_mode
        if desr_proc_compat_mode is None:
            desr_proc_compat_mode = 'default'
        extra_specs['powervm:processor_compatibility'] = desr_proc_compat_mode
        LOG.debug("Requested CPU: desired cpus=%(cpus)d, minimum "
                  "cpus=%(cpus_min)d, maximum cpus=%(cpus_max)d" % locals())
        LOG.debug("Checking for the availability of (requested) "
                  "resources on the host...")
        self._check_host_resources(instance, host_stats, extra_specs,
                                   resource_deltas)
        LOG.debug("Resource availability check done!!")

        # Network
        mac_base_value, virtual_eth_adapters = \
            self._get_virtual_eth_adapters(network_info)
        self.plug_vifs(instance, network_info, raise_err=True)

        # LPAR configuration data
        # max_virtual_slots is hardcoded to 64 since we generate a MAC
        # address that must be placed in slots 32 - 64
        if virtual_eth_adapters:
            virt_eth_adpts_str = "\\" + "\"" + virtual_eth_adapters + "\\" + \
                "\""
        else:
            virt_eth_adpts_str = None

        LOG.debug("Creating a PowerVC LPAR object")
        lpar_inst = LPAR.LPAR(
            name=lpar_name, lpar_env='aixlinux',
            min_mem=mem_min, desired_mem=mem,
            max_mem=mem_max, proc_mode='shared',
            sharing_mode='uncap', min_procs=cpus_min,
            desired_procs=cpus, max_procs=cpus_max,
            min_proc_units=cpus_units_min,
            desired_proc_units=cpus_units,
            max_proc_units=cpus_max,
            lpar_proc_compat_mode=desr_proc_compat_mode,
            virtual_eth_mac_base_value=mac_base_value,
            max_virtual_slots=64,
            virtual_eth_adapters=virt_eth_adpts_str)
        return lpar_inst

    @logcall
    def _get_virtual_eth_adapters(self, network_info):
        """
        Generates the virtual_eth_adapters parameter string that is passed to
        the IVM mksyscfg command, for configuring the network adapters when
        deploying an LPAR on IVM.

        :param network_info: A list of dicts containing vif data, each dict
                             is required to have the following keys: address
                             and meta.
        :return virtual_eth_mac_base_value: A string that forms the base MAC
                                            address for the LPAR.
        :return virtual_eth_adapters: A string that can be passed to the IVM
                                      mksyscfg command, with comma separated
                                      network adapter information.
        """
        virtual_eth_adapters = ''
        if network_info and len(network_info):
            mac_base_value = (network_info[0]['address'][:-2]).replace(':', '')
            for vif in network_info:
                mac = vif['address']
                mac_base_value = (mac[:-2]).replace(':', '')
                vlan_id, slot_id = self._get_virtual_eth_configs_from_vifs(vif)
                if virtual_eth_adapters:
                    virtual_eth_adapters = virtual_eth_adapters + ','
                virtual_eth_adapters = virtual_eth_adapters + \
                    ('%(slot_id)s/0/%(vlan_id)s//0/0' % locals())
        else:
            # if network_info is None, deploy Vs without network
            mac_base_value = None
            virtual_eth_adapters = None
            LOG.info(_("No network_info found. Deploy without network"))

        return mac_base_value, virtual_eth_adapters

    @logcall
    def _get_virtual_eth_configs_from_vifs(self, vif):
        """
        Inspect vif and extract slot and vlan id for virtual ethernet adpater
        configuration.

        Extract slot num from MAC.  To ensure the MAC address on the guest
        matches the generated value, pull the first 10 characters off the MAC
        address for the mac_base_value parameter and then get the integer
        value of the final 2 characters as the slot_id parameter

        :param vif: dict with 'address' and 'vlan'
        :return vlan_id: taken from the vif dict
        :return slot_id: taken from the vif dict
        """
        mac = vif['address']
        slot_id = int(mac[-2:], 16)
        LOG.debug("slot_id = %s" % slot_id)
        meta = vif['meta']
        netmeta = vif['network']['meta']
        vlan_id = None
        if meta and ('vlan' in meta):
            # Extract the bridge info
            vlan_id = meta['vlan']
        elif netmeta and ('vlan' in netmeta):
            vlan_id = str(netmeta['vlan'])
        else:
            vlan_id = self._operator.get_virtual_eth_adapter_id()

        LOG.debug("vlan_id = %s" % vlan_id)
        return vlan_id, slot_id

    @logcall
    def _check_host_resources(self, instance, host_stats, extra_specs,
                              resource_deltas=None):
        """Checks resources on host for resize, migrate, and spawn
        :param instance: dict of instance data
        :param host_stats: dict of host stats
        :param extra_specs: extra_specs attribute of instance_type object.
        :param resource_deltas: dict of resource deltas. Populated only during
            a resize operation. Deltas will be used instead of total resources
            when comparing against available resources.
        """
        inst_name = instance['name']
        # Get total requested processing units
        req_cpus_units = decimal.Decimal(extra_specs['powervm:proc_units'])
        desr_proc_compat = extra_specs['powervm:processor_compatibility']
        if desr_proc_compat is not None:
            desr_proc_compat = desr_proc_compat.lower()
        # If resource_deltas is not None, this is a resize, so set resources
        # to check to the delta values.
        if resource_deltas is not None:
            mem = resource_deltas['delta_mem']
            vcpus = resource_deltas['delta_vcpus']
            cpus_units = decimal.Decimal('%.2f' %
                                         resource_deltas['delta_cpus_units'])
            desr_proc_compat = resource_deltas['desired_proc_compat'].lower()
        else:
            mem = instance['memory_mb']
            vcpus = instance['vcpus']
            cpus_units = req_cpus_units

        # Memory
        mem_min = constants.IBM_POWERVM_MIN_MEM
        if mem > host_stats['host_memory_free']:
            ex_args = {'instance_name': inst_name,
                       'mem_requested': mem,
                       'mem_avail': host_stats['host_memory_free']}
            LOG.error(_("Insufficient free memory on the host for "
                        "instance '%(instance_name)s' (%(mem_requested)d MB "
                        "requested, %(mem_avail)d MB free)") % ex_args)
            raise common_ex.IBMPowerVMInsufficientFreeMemory(**ex_args)
        if instance['memory_mb'] < mem_min:
            ex_args = {'instance_name': inst_name,
                       'mem_requested': instance['memory_mb'],
                       'mem_min': mem_min}
            LOG.error(_("Memory requested (%(mem_requested)d MB) is less than "
                        "minimum required value (%(mem_min)d MB) for instance "
                        "'%(instance_name)s'") % ex_args)
            raise common_ex.IBMPowerVMMemoryBelowMin(**ex_args)

        # CPU
        if instance['vcpus'] > int(host_stats['vcpus']):
            ex_args = {'instance_name': inst_name,
                       'cpus_requested': instance['vcpus'],
                       'cpus_avail': int(host_stats['vcpus'])}
            LOG.error(_("Insufficient available CPUs on host for instance "
                        "'%(instance_name)s' (%(cpus_requested)d requested, "
                        "%(cpus_avail)d available)") % ex_args)
            raise common_ex.IBMPowerVMInsufficientCPU(**ex_args)
        cpus_min = constants.IBM_POWERVM_MIN_CPUS
        if instance['vcpus'] < cpus_min:
            ex_args = {'instance_name': inst_name,
                       'cpus_requested': instance['vcpus'],
                       'cpus_min': cpus_min}
            LOG.error(_("CPUs requested (%(cpus_requested)d) are less than "
                        "the minimum required (%(cpus_min)d) for instance "
                        "'%(instance_name)s'") % ex_args)
            raise common_ex.IBMPowerVMCPUsBelowMin(**ex_args)

        cpus_units_min = constants.POWERVM_MIN_PROC_UNITS
        avail_cpus_units = decimal.Decimal(
            '%.2g' % (float(host_stats['proc_units']) -
                      (float(host_stats['proc_units_used']) +
                       float(host_stats['proc_units_reserved']))))
        if req_cpus_units < cpus_units_min:
            ex_args = {'instance_name': inst_name,
                       'units_requested': '%.2f' % req_cpus_units,
                       'units_min': '%.2f' % cpus_units_min}
            LOG.error(_("Processing units requested (%(units_requested)s) are "
                        "below minimum required (%(units_min)s) for instance "
                        "'%(instance_name)s'") % ex_args)
            raise common_ex.IBMPowerVMProcUnitsBelowMin(**ex_args)
        if cpus_units > avail_cpus_units:
            ex_args = {'instance_name': inst_name,
                       'units_requested': '%.2f' % cpus_units,
                       'units_avail': '%.2f' % avail_cpus_units}
            LOG.error(_("Insufficient available processing units on host for "
                        "instance '%(instance_name)s' (%(units_requested)s "
                        "requested, %(units_avail)s available)") % ex_args)
            raise common_ex.IBMPowerVMInsufficientProcUnits(**ex_args)
        if req_cpus_units > instance['vcpus']:
            ex_args = {'instance_name': inst_name,
                       'units_requested': '%.2f' % req_cpus_units,
                       'cpus_requested': instance['vcpus']}
            LOG.error(_("Processing units requested (%(units_requested)s) "
                        "cannot be larger than CPUs requested "
                        "(%(cpus_requested)d) for instance "
                        "'%(instance_name)s'") % ex_args)
            raise exception.IBMPowerVMUnitsGreaterThanCPUs(**ex_args)

        # Obtain supported processor compatibility modes on system,
        # and verify that the specified mode is supported
        proc_compat_modes = host_stats['compatibility_modes'].split(',')
        if desr_proc_compat not in [x.lower() for x in proc_compat_modes]:
            ex_args = {'instance_name': inst_name,
                       'processor_compatibility':
                       desr_proc_compat,
                       'valid_values':
                       ', '.join(proc_compat_modes)}
            LOG.error(_("'%(processor_compatibility)s' is not a"
                        " valid option for processor compatibility for"
                        " instance '%(instance_name)s'. Valid options"
                        " are '%(valid_values)s'.") % ex_args)
            raise exception.IBMPowerVMInvalidProcCompat(**ex_args)

    def plug_vifs(self, instance, network_info, raise_err=False):
        """
        Plug VIFs into networks.

        :param instance: network instance
        :param network_info: network information for VM deployment
        :param raise_err: Whether to raise exception when error occurred.
               Default is not to raise exception since it can be called
               as part of init_instance when nova compute starts
        :raises: IBMPowerVMNetworkConfigFailed
        """
        if self._vif:
            try:
                self._vif.plug(instance, network_info)
            except Exception as e:
                LOG.exception(_("plug_vifs failed: %s") % e)
                if raise_err:
                    ex_args = {'instance_name': instance['name'],
                               'error_msg': e}
                    raise exception.IBMPowerVMNetworkConfigFailed(**ex_args)
        else:
            LOG.info(_("No VIF driver available; skipping plug_vifs"))
            if raise_err:
                ex_args = {'instance_name': instance['name'],
                           'error_msg': _("VIF driver is not found")}
                raise exception.IBMPowerVMNetworkConfigFailed(**ex_args)

    def unplug_vifs(self, instance, network_info, raise_err=True):
        """
        Unplug VIFs from networks.

        :param instance: network instance
        :param network_info: network information about VM deployment
        :param raise_err: Whether to raise exception when error occurred.
               Default is to raise exception
        :raises: IBMPowerVMNetworkUnconfigFailed
        """
        if self._vif:
            try:
                self._vif.unplug(instance, network_info)
            except Exception as e:
                LOG.exception(_("unplug_vifs failed: %s") % e)
                if raise_err:
                    ex_args = {'instance_name': instance['name'],
                               'error_msg': e}
                    raise exception.IBMPowerVMNetworkUnconfigFailed(**ex_args)
        else:
            LOG.info(_("No VIF driver available; skipping unplug_vifs"))
            if raise_err:
                ex_args = {'instance_name': instance['name'],
                           'error_msg': _("VIF driver is not found")}
                raise exception.IBMPowerVMNetworkUnconfigFailed(**ex_args)

    @logcall
    def capture_image(self, context, instance, image_id, image_meta,
                      update_task_state):
        """Capture the root disk for a snapshot

        :param context: nova context for this operation
        :param instance: instance information to capture the image from
        :param image_id: uuid of pre-created snapshot image
        :param image_meta: metadata to upload with captured image
        :param update_task_state: Function reference that allows for updates
                                  to the instance task state.
        :raises IBMPowerVMLPARIsRunningDuringCapture if the instance is running
                and does not reach the stopped state by itself within a given
                time frame.
        """

        # Wait up to two minutes (24*5)s for the server to reach the
        # Not Activated state to cover the case of the capture being called
        # immediately after the AE initiated shut down started.
        LOG.debug("Checking to see if the instance is stopped.")
        timeout_count = range(24)
        while timeout_count:
            lpar = self._operator.get_lpar(instance['name'])
            state = lpar['state']
            if state == 'Not Activated':
                LOG.info(_("Instance is stopped."),
                         instance=instance)
                break
            timeout_count.pop()
            if len(timeout_count) == 0:
                LOG.error(_("Instance '%s' failed to stop druing capture.") %
                          instance['name'])
                raise common_ex.IBMPowerVMLPARIsRunningDuringCapture(
                    instance_name=instance['name'])

            LOG.debug("The instance is not stopped. "
                      "Waiting to check again.")
            time.sleep(5)

        # Remove any virtual optical devices so that the VM being captured
        # will run its reset-restore path rather than deploy activation path
        # when it is restarted.
        self._operator.remove_vopt_media_by_instance(instance)

        # get disk_name
        vhost = self._operator.get_vhost_by_instance_id(lpar['lpar_id'])
        disk_name = self._operator.get_disk_name_by_vhost(vhost)

        # do capture and upload
        if CONF.local_or_cinder and CONF.host_storage_type == 'local':
            self._disk_adapter.create_image_from_volume_v2(instance,
                                                        disk_name, context, 
                                                        image_id, image_meta, 
                                                        update_task_state)
        else:
            self._disk_adapter.create_image_from_volume(instance,
                                                    disk_name, context,
                                                    image_id, image_meta,
                                                    update_task_state)

    def instance_exists(self, instance_name):
        lpar_instance = self._operator.get_lpar(instance_name)
        return True if lpar_instance else False

    def _get_instance(self, instance_name):
        """Check whether or not the LPAR instance exists and return it."""
        lpar_instance = self._operator.get_lpar(instance_name)

        if lpar_instance is None:
            LOG.error(_("LPAR instance '%s' not found") % instance_name)
            raise exception.IBMPowerVMLPARInstanceNotFound(
                instance_name=instance_name)
        return lpar_instance

    def list_instances(self):
        """
        Return the names of all the instances known to the virtualization
        layer, as a list.
        """
        lpar_instances = self._operator.list_lpar_instances()
        return lpar_instances

    def _vopt_cleanup(self, vopt_img_name, vdev_fbo_name):
        """Attempt cleanup of vopt device
        :param vopt_img_name: vopt media name stored in media library
        :param vdev_fbo_name: vtdisk name attached to instance
        """
        try:
            self._operator.unload_vopt_device(vdev_fbo_name,
                                              force_release=True)
        except Exception:
            msg = (_('Error unload media from vopt: %(vdev_fbo_name)s') %
                   locals())
            LOG.exception(msg)
        try:
            self._operator.remove_fbo_device(vdev_fbo_name)
        except Exception:
            msg = (_('Error cleaning up FBO: %(vdev_fbo_name)s') %
                   locals())
            LOG.exception(msg)
        try:
            self._operator.remove_vopt_device(vopt_img_name)
        except Exception:
            msg = (_('Error cleaning up vopt: %(vopt_img_name)s') %
                   locals())
            LOG.exception(msg)

    def _attach_iso(self, iso_file_path, instance):
        """
        Private method for attaching an ISO file to a
        virtual machine as a file backed virtual optical.
        :param iso_file_path: path to the ISO file on the IVM
        :param instance: Instance object as returned by DB layer.
        :returns a tuple of virtual optical media name and
                the name of the file backed optical device

        """
        # create virtual optical device to attach ISO

        if not iso_file_path:
            return None, None

        vopt_img_name = None
        vdev_fbo_name = None
        lpar = self._operator.get_lpar(instance['name'])
        lpar_id = lpar['lpar_id']
        vhost = self._operator.get_vhost_by_instance_id(lpar_id)
        lpar_name = lpar['name']
        LOG.info(_("Attaching iso %(iso_file_path)s to lpar %(lpar_name)s") %
                 locals())
        vopt_img_name = 'vopt_%s' % instance['uuid'].replace('-', '')
        self._operator.create_vopt_device(
            vopt_img_name, iso_file_path)
        self._operator.remove_ovf_env_iso(iso_file_path)
        vdev_fbo_name = self.attach_vopt(vopt_img_name, lpar, vhost)
        return vopt_img_name, vdev_fbo_name

    @logcall
    def get_volume_connector(self, instance):
        """Get connector information for the instance for attaching to volumes.

        Connector information is a dictionary representing the ip of the
        machine that will be making the connection, the name of the iscsi
        initiator and the hostname of the machine as follows::

            {
                'host': hostname
                'wwpns': WWPNs
            }
        """
        if not self._fc_wwpns:
            self._fc_wwpns = self._operator.get_wwpns()

        if not self._hypervisor_hostname:
            self._hypervisor_hostname = self._operator.get_hostname()

        # IBMPowerVMDriver will only support FC volume.
        # iSCSI support information is removed.
        connector = {'host': self._hypervisor_hostname}

        if self._fc_wwpns and len(self._fc_wwpns) > 0:
            connector['wwpns'] = self._fc_wwpns
        else:
            LOG.exception(_("PowerVM's FC wwpns list is empty"))
            self._fc_wwpns = None
            #change for paxes
            #raise exception.IBMPowerVMNullWWPNS()

        return connector

    @logcall
    @synchronized('odm_lock', 'pvm-')
    def attach_volume(self, connection_info, instance, mountpoint):
        """
        Attach the disk to the instance at mountpoint using info.
        THe new volume data returned from cinder SAN driver:
         data:
             {'target_lun': '5',
              'target_wwn':
                 ['5005076801100071',
                  '5005076801200071',
                  '50050768011052EC',
                  '50050768012052EC'],
              'target_discovered': False,
              'volume_id': 'afea488b-0049-406b-a782-2479253e3264'
             }
          The steps to attach_volume:
          1. run_cfg_dev to discover the newly attached volume(by cinder)
          2. use the target_wwn:target_lun as the AIX connection key to
             search from lspath -conn <conn string> -fmt :
          3. attach disk to vhost.
          Serialize the volume operation to avoid ODM lock issue
        """
        try:
            volume_data = connection_info['data']

            LOG.info(_("Attaching volume_id %s") % volume_data['volume_id'])

            # Before run run_cfg_dev, check whether there is any
            # stale disk with the same LUN id. If so, remove it.

            self._disk_adapter.handle_stale_disk(volume_data)

            # limit the cfgdev run target. For a multiport device,
            # the most efficient way for device discovery is run
            # against its PHB(pci bus)
            # fcs_parent looks like {'pci0':['fcs0','fcs1']}
            fcs_parent = self._operator.get_fcs_parent_devices()
            for phb in fcs_parent:
                self._operator.run_cfg_dev(phb)
            LOG.debug("config devices %s" % fcs_parent)

            aix_conn = self._operator.get_volume_aix_conn_info(volume_data)
            attach_info = self._operator.get_devname_by_aix_conn(aix_conn)
            # Cinder volume attached on the soure VIOS needs to be
            # LPM ready. no_reserve has to be set before a cinder
            # volume is attached to VM. Otherwise reservation cannot
            # be changed unless the volume is detached and reattached
            # again.

            reserve_policy = self._operator.get_hdisk_reserve_policy(
                attach_info['device_name'])
            if reserve_policy != 'no_reserve':
                # set reserve_policy to no reserve in order to be
                # LPM capable. The reserve policy has to be set before
                # the disk is opened by vscsi driver to attach to The
                # host. So it cannot be done during migration time.
                self._operator.set_no_reserve_on_hdisk(
                    attach_info['device_name'])
            LOG.debug("device to attach: %s" % attach_info['device_name'])

            lpar_id = self._operator.get_lpar(instance['name'])['lpar_id']
            vhost = self._operator.get_vhost_by_instance_id(lpar_id)
            self._operator.attach_disk_to_vhost(attach_info, vhost)

        except Exception as e:
            volume_id = connection_info['data']['volume_id']
            raise exception.\
                IBMPowerVMVolumeAttachFailed(instance,
                                             volume_id,
                                             e)

    @logcall
    @synchronized('odm_lock', 'pvm-')
    def detach_volume(self, connection_info, instance, mountpoint):
        """
        Detach volume storage to VM instance.

         data:
            {'target_lun': '5',
             'target_wwn':
                ['5005076801100071',
                 '5005076801200071',
                 '50050768011052EC',
                 '50050768012052EC'],
             'target_discovered': False,
             'volume_id': 'afea488b-0049-406b-a782-2479253e3264'
            }
         The steps to detch_volume:
        1. get aix connection information from volume data
        2. get device name from aix connection
        3. delete volume from vhost
        4. remove volume device
        Serialize the volume operation to avoid ODM lock issue
        """

        volume_data = connection_info['data']

        aix_conn = self._operator.get_volume_aix_conn_info(volume_data)

        if not aix_conn:
            LOG.error(_("No device to detach from volume data:%s") %
                      volume_data)
            return
        LOG.info(_('Detach volume_uuid: %s') % volume_data['volume_id'])
        # We need to find the hdisk name to pass to detach
        # If we don't find it, there's nothing to detach
        try:
            volume_info = self._operator.get_devname_by_aix_conn(aix_conn)
            self._disk_adapter.detach_volume_from_host(volume_info)
        except Exception:
            LOG.info(_("Could not find conn to hdisk mapping, conn:%s") %
                     aix_conn)

    def attach_vopt(self, vopt_img_name, lpar, vhost):
        LOG.info(_("Attaching vopt %(vopt_img_name)s to lpar %(lpar)s") %
                 locals())
        # create FBO then attach ISO image
        vdev_fbo_name = self._operator.create_fbo_device(vhost)

        if not vdev_fbo_name:
            raise exception.IBMPowerVMISOAttachFailed()

        LOG.info(_("Loading vopt %(vopt_img_name)s to FBO %(vdev_fbo_name)s") %
                 locals())
        self._operator.load_vopt_device(
            vopt_img_name, vdev_fbo_name)
        return vdev_fbo_name

    def _detach_iso(self, vopt_img_name,
                    vdev_fbo_name, instance):
        """Unload the image after LPAR started
        :param vopt_img_name: vopt media name stored in media library
        :param vdev_fbo_name: vtdisk name attached to instance
        :param instance: object of new instance
        """
        lpar = self._operator.get_lpar(instance['name'])
        if lpar is None:
            # Already in error path, attempting iso cleanup
            self._vopt_cleanup(vopt_img_name, vdev_fbo_name)
            raise exception.IBMPowerVMLPARInstanceNotFound(
                instance_name=instance['name'])

        detach_timeout = int(CONF.ibmpowervm_iso_detach_timeout)
        if instance and detach_timeout > 0:
            eventlet.spawn_after(detach_timeout,
                                 self._operator.remove_vopt_media_by_instance,
                                 instance)

    def _transport_config_data(self, instance, injected_files, admin_password,
                               network_info):
        """
        Builds and transport ISO from config_data

        :param instance: instance
        :return remote path of transported ISO, None if there is no
        configuration data to build the ISO.
        """
        drive_path = None
        if configdrive.required_by(instance):
            LOG.debug("Forcing injection to take place on a config drive")
            drive_path = config_iso.create_config_drive_iso(instance,
                                                            injected_files,
                                                            admin_password,
                                                            network_info)
        if not drive_path:
            return None
        # transfer ISO to VIOS
        try:
            iso_remote_path, iso_size = self._disk_adapter.copy_image_file(
                drive_path, CONF.powervm_img_remote_path)
        finally:
            os.remove(drive_path)

        if not self._operator.check_media_library_exists():
            LOG.info(_("There is no media library, creating one on rootvg"))
            self._operator.create_media_library(volume_group="rootvg",
                                                size_gb=1)
        return iso_remote_path

    def get_host_uptime(self, host):
        """Returns the result of calling "uptime" on the target host."""
        return self._operator.get_host_uptime(host)

    def spawn(self, context, instance, image_meta, injected_files,
              admin_password, network_info, block_device_info,
              validate_volumes, attach_volumes,
              validate_attachment):
        pass

    def start_instance(self, instance):
        LOG.debug("Activating the LPAR instance '%s'"
                  % instance['name'])

        self._operator.start_lpar(instance['name'])

        # TODO(mrodden): probably do this a better way
        #                that actually relies on the time module
        #                and nonblocking threading
        # Wait for boot
        timeout_count = range(10)
        while timeout_count:
            state = self.get_info(instance['name'])['state']
            if state == power_state.RUNNING:
                LOG.info(_("Instance spawned successfully."),
                         instance=instance)
                break
            timeout_count.pop()
            if len(timeout_count) == 0:
                LOG.error(_("Instance '%s' failed to boot") %
                          instance['name'])
                self._cleanup(instance['name'])
                raise exception.IBMPowerVMLPAROperationTimeout(
                    operation='start_lpar',
                    instance_name=instance['name'])
            time.sleep(1)

        state_dict = dict(power_state=power_state.RUNNING)
        try:
            self._virtapi.instance_update(context.get_admin_context(),
                                          instance['uuid'],
                                          state_dict)
        except Exception as ex:
            LOG.exception(_('Error updating Power State: %s') % ex)

    def _cleanup(self, instance_name, destroy_disks=True):
        lpar_id = self._get_instance(instance_name)['lpar_id']
        try:
            vhost = self._operator.get_vhost_by_instance_id(lpar_id)
            disk_name = self._operator.get_disk_name_by_vhost(vhost)

            LOG.debug("Shutting down the instance '%s'" % instance_name)
            self._operator.stop_lpar(instance_name)

            # dperaza: LPAR should be deleted first so that vhost is
            # cleanly removed and detached from disk device.
            LOG.debug("Deleting the LPAR instance '%s'" % instance_name)
            self._operator.remove_lpar(instance_name)

            if disk_name and destroy_disks:
                # TODO(mrodden): we should also detach from the instance
                # before we start deleting things...
                volume_info = {'device_name': disk_name}
                # Volume info dictionary might need more info that is lost when
                # volume is detached from host so that it can be deleted
                self._disk_adapter.detach_volume_from_host(volume_info)
                self._disk_adapter.delete_volume(volume_info)
        except Exception as e:
            LOG.exception(_("PowerVM instance cleanup failed"))
            raise common_ex.IBMPowerVMLPARInstanceCleanupFailed(
                instance_name=instance_name, reason=e)

    def migrate_disk(self, device_name, src_host, dest, image_path,
                     instance_name=None):
        """Migrates SVC or Logical Volume based disks

        :param device_name: disk device name in /dev/
        :param dest: IP or DNS name of destination host/VIOS
        :param image_path: path on source and destination to directory
                           for storing image files
        :param instance_name: name of instance being migrated
        :returns: disk_info dictionary object describing root volume
                  information used for locating/mounting the volume
        """
        pass

    def deploy_from_migrated_file(self, lpar, file_path, size,
                                  power_on=True):
        """Deploy the logical volume and attach to new lpar.

        :param lpar: lar instance
        :param file_path: logical volume path
        :param size: new size of the logical volume
        """
        need_decompress = file_path.endswith('.gz')

        try:
            # deploy lpar from file
            self._deploy_from_vios_file(lpar, file_path, size,
                                        decompress=need_decompress,
                                        power_on=power_on)
        finally:
            # cleanup migrated file
            self._operator._remove_file(file_path)

    def _deploy_from_vios_file(self, lpar, file_path, size,
                               decompress=True, power_on=True):
        self._operator.create_lpar(lpar)
        lpar = self._operator.get_lpar(lpar['name'])
        instance_id = lpar['lpar_id']
        vhost = self._operator.get_vhost_by_instance_id(instance_id)

        # Create logical volume on IVM
        diskName = self._disk_adapter._create_logical_volume(size)
        # Attach the disk to LPAR
        disk_info = {'device_name': diskName}
        self._operator.attach_disk_to_vhost(disk_info, vhost)

        # Copy file to device
        self._disk_adapter._copy_file_to_device(file_path, diskName,
                                                decompress)

        if power_on:
            self._operator.start_lpar(lpar['name'])

    def manage_image_cache(self, context, all_instances):
        # (ppedduri): We are currently managing only Local disk
        # Hence, the default behavior is 'pass'
        # Local Disk Operator has current implementation.
        pass

    def _fetch_all_images_from_glance(self, context):
        """
        Returns all images added to the glance registry.
        Makes glance api call.
        """
        glance_service = glance.get_default_image_service()
        all_images = glance_service.detail(context)

        return all_images

    def _clean_up_images_from_host(self, context):
        """
            Tries to clean up ISO & OVF images from caching area of the host
            being de-registered.
            This involves in preparing a script on the fly and
            sending it to host.
        """
        LOG.info(_("Cleaning up the images from host"))
        all_images = self._fetch_all_images_from_glance(context)
        if not all_images:
            LOG.info(_("No image found in glance repository"))
            return

        script = '!/bin/ksh' + '\n'
        for image in all_images:
            image_file_path = os.path.join(CONF.powervm_img_remote_path,
                                           image['id'])
            if image['disk_format'] == 'iso':
                script += 'rm -f ' + image_file_path + '.iso\n'
                script += '/usr/ios/cli/ioscli rmvopt -name ' + \
                    image['id'] + '\n'
            else:
                if(CONF.host_storage_type == 'local'):
                    script += 'rm -f ' + image_file_path + '*\n'
        tmp_file_path = '/var/log/nova/cleanup_' + CONF.host + '.sh'
        remote_file_path = os.path.join(CONF.powervm_img_remote_path,
                                        'cleanup_' + CONF.host
                                        + '.sh')
        script += 'rm -f ' + remote_file_path
        file_handle = None
        try:
            file_handle = open(tmp_file_path, 'w')
            file_handle.writelines(script)
        except Exception as ex:
            LOG.exception(_('Unable to create cleanup script '))
            raise ex
        finally:
            if file_handle:
                file_handle.close()
        try:
            common.scp_command(self.connection_data, tmp_file_path,
                               CONF.powervm_img_remote_path, 'put')
            self._operator.run_vios_command('chmod +x ' + remote_file_path)
            self._operator.run_vios_command_as_root(remote_file_path + ' &',
                                                    False)
        except Exception as ex:
            raise ex
        finally:
            os.remove(tmp_file_path)

    def _find_resize_deltas(self, instance, instance_type):
        """Find the resource deltas before resize

        :param instance: current instance dict
        :param instance_type: new flavor specs

        :return Memory, processor, and processing units deltas
        """
        LOG.debug("Entering")
        # Get existing proc units from instance
        old_metalist = instance.get('system_metadata', {})

        delta_mem = instance_type['memory_mb'] - instance['memory_mb']
        delta_vcpus = instance_type['vcpus'] - instance['vcpus']

        desired_proc_units = instance_type['extra_specs'].\
            get('powervm:proc_units')
        if desired_proc_units is None:
            desired_proc_units = proc_utils.\
                get_num_proc_units(instance_type['vcpus'])
        desired_proc_compat = instance_type['extra_specs'].\
            get('powervm:processor_compatibility')
        # If there is no processor_compatibility in the request
        # we are not changing the mode
        if desired_proc_compat is None or desired_proc_compat == "":
            desired_compatibility_mode = None
            if 'desired_compatibility_mode' in old_metalist:
                if old_metalist['desired_compatibility_mode']:
                        desired_compatibility_mode = \
                            old_metalist['desired_compatibility_mode']
                        desired_proc_compat = desired_compatibility_mode

        curr_proc_units = None
        # Convert the List of Meta-data to a Dictionary before we return it
        if 'vcpus' in old_metalist:
            if old_metalist['vcpus']:
                curr_proc_units = old_metalist['vcpus']

        if curr_proc_units is not None:
            delta_cpus_units = float(desired_proc_units) - \
                float(curr_proc_units)
        else:
            delta_cpus_units = desired_proc_units

        LOG.debug("Exiting (delta_mem = %s, delta_vcpus = %s, \
                            delta_cpus_units = %s), %s" %
                  (delta_mem, delta_vcpus, delta_cpus_units,
                   desired_proc_compat))
        return delta_mem, delta_vcpus, delta_cpus_units, desired_proc_compat

    @synchronized('concur_migr')
    def check_concur_migr_stats_source(self, inst_name, host_name):
        curr = len(self._source_migrate)
        if curr >= MAX_SUPP_CONCUR_MIGRATONS:
            error = (_("Cannot migrate %(inst)s because host %(host)s "
                       "only allows %(allowed)s concurrent migrations, and "
                       "%(running)s migrations are currently running.")
                     % {'inst': inst_name, 'host': host_name,
                        'allowed': MAX_SUPP_CONCUR_MIGRATONS,
                        'running': curr})
            LOG.exception(error)
            raise common_ex.IBMPowerVMMigrationFailed(error)
        self.set_source_host_for_migration(inst_name)

    @synchronized('concur_migr')
    def check_concur_migr_stats_dest(self, inst_name, host_name):
        curr = len(self._dest_migrate)
        if curr >= MAX_SUPP_CONCUR_MIGRATONS:
            error = (_("Cannot migrate %(inst)s because host %(host)s "
                       "only allows %(allowed)s concurrent migrations, and "
                       "%(running)s migrations are currently running.")
                     % {'inst': inst_name, 'host': host_name,
                        'allowed': MAX_SUPP_CONCUR_MIGRATONS,
                        'running': curr})
            LOG.exception(error)
            raise common_ex.IBMPowerVMMigrationFailed(error)
        # Verify the destination host can support another migration
        # Ensures the stat verification even after the service restarted
        self._operator.check_if_migr_reached_max(inst_name)
        self.set_dest_host_for_migration(inst_name)

    def set_source_host_for_migration(self, value):
        if self._source_migrate.count(value) <= 0:
            self._source_migrate.append(value)

    def is_source_host_for_migration(self):
        if len(self._source_migrate) > 0:
            return True
        else:
            return False

    def is_vm_migrating(self, vm_name):
        if self._source_migrate.count(vm_name) > 0:
            return True
        else:
            return False

    def unset_source_host_for_migration(self, value):
        if self._source_migrate.count(value) > 0:
            self._source_migrate.remove(value)

    def set_dest_host_for_migration(self, value):
        if self._dest_migrate.count(value) <= 0:
            self._dest_migrate.append(value)

    def is_dest_host_for_migration(self):
        if len(self._dest_migrate) > 0:
            return True
        else:
            return False

    def is_vm_migrating_dest(self, value):
        if self._dest_migrate.count(value) > 0:
            return True
        else:
            return False

    def unset_dest_host_for_migration(self, value):
        if self._dest_migrate.count(value) > 0:
            self._dest_migrate.remove(value)
            
    def ssh_trust(self, public_key):
        #home = os.path.expanduser("~")
        #ssh_home = home + '/.ssh'
        ssh_home = '/home/padmin/.ssh'
        if not os.path.exists(ssh_home):
            os.makedirs(ssh_home)
        authorized_keys = ssh_home + '/authorized_keys'
        
        file_handle = None
        result = 'sshtrust'
        try:
            file_handle = open(authorized_keys, 'a')
            file_handle.writelines(public_key)
            result = 'sshtrust'
        except Exception as ex:
            LOG.exception(_('Unable to wirte the auth file '))
            raise ex
        finally:
            if file_handle:
                file_handle.close()
        padmin_uid = pwd.getpwnam('padmin').pw_uid
        os.chown(authorized_keys, padmin_uid, -1)
        return result

    def image_create_update(self, image_meta, image_data):
        sourcedir = CONF.image_dest_dir or '/tmp'
        image_data['sourcedir'] = sourcedir
        hdisk_id = image_data.get('hdisk_id')
        context = image_data.get('context')
        destfile = self._downing_image_glance(image_meta, image_data)
        image_meta = self._disk_adapter.volume_api.image_volume_capture(context,image_meta, image_data)
        self._disk_adapter._copy_file_to_device(destfile, hdisk_id, False)
        self._disk_adapter.update_to_glance(destfile, image_meta, image_data)

    def _downing_image_glance(self, image_meta, image_data):
        context = image_data.get('context')
        destfile = self._disk_adapter.download_from_glance(context, image_meta, image_data)
        return destfile

    def image_create(self, image_meta, image_data):
        location = image_data.get('location', None)
        if location is None:
            raise
        sourcedir = CONF.image_dest_dir or '/tmp'
        destfile = self._downing_image(sourcedir, location)
        if destfile is None:
            raise
        else:
            self._update_and_copy(destfile, image_meta, image_data)

    def _downing_image(self, sourcedir, location):
        from six.moves.urllib import parse as urlparse
        from nova.openstack.common import uuidutils
        destfile = sourcedir or '/tmp' + '/' + uuidutils.generate_uuid()
        try:
            import urllib2
        except ImportError:
            raise
        else:
            pieces = urlparse.urlparse(location)
            assert pieces.scheme in ('http', 'https')
            f = urllib2.urlopen(location)
            with open(destfile, "wb") as img:
                img.write(f.read())
        return destfile

    def _update_and_copy(self, destfile, image_meta, image_data):
        is_black = image_data.get('is_back')
        if is_black:
            self._update_to_cinder(destfile, image_meta, image_data)
        else:
            self._update_to_glance(destfile, image_meta, image_data)

    def _update_to_cinder(self, destfile, image_meta, image_data=None):
        context = image_data.get('context', None)
        if 'hdisk_id' not in image_meta:
            raise
        hdisk_id = image_data.get('hdisk_id')
        if not os.path.exists('/dev/%s' % hdisk_id):
            LOG.error(_("NOT Found hdisk %s" % hdisk_id))

        cmd = "dd if=%s of=%s bs=4M" %(destfile, '/dev/%s' % hdisk_id)
        self._operator.run_vios_command_as_root(cmd)

        image_meta = self._disk_adapter.volume_api.image_volume_capture(context, image_meta)
        self._disk_adapter.update_to_glance(destfile, image_meta, image_data)

    def _update_to_glance(self, destfile, image_meta, image_data=None):
        self._disk_adapter.update_to_glance(destfile, image_meta, image_data)

    def image_attach_volume(self, connection_info, encryption=None):
        pass
#
# PowerVMSANOperator extends the base operator
# functinality to give the SAN specific
# operations.
#


class PowerVMSANOperator(PowerVMBaseOperator):

    """PowerVM main operator.

    The PowerVMOperator is intended to wrap all operations
    from the driver and handle either IVM or HMC managed systems.
    """

    global live_migration_data
    live_migration_data = {}

    def __init__(self, virtapi):
        LOG.debug("PVMOperator __init__")

        pvm_conn = common.Connection(CONF.powervm_mgr,
                                     CONF.powervm_mgr_user,
                                     CONF.powervm_mgr_passwd,
                                     CONF.powervm_mgr_port)

        operator = IVMOperator(pvm_conn)
        disk_adapter = blockdev.SANDiskAdapter(operator)
        super(
            PowerVMSANOperator,
            self).__init__(virtapi,
                           operator,
                           disk_adapter)

    @logcall
    def detach_volume_from_lpar(self, instance_name, volume_uid):
        """
        This method is used during cold migration to detach volume.
        It detaches volume from lpar with instance_name.
        1. Detach volume from host (detach disk from vhost and remove hdisk)
        2. Remove the vdisk and hostmap from SVC
        """
        lpar_id = self._get_instance(instance_name)['lpar_id']
        vhost = self._operator.get_vhost_by_instance_id(lpar_id)
        disk_name = self._operator.get_disk_name_by_vhost(vhost)
        LOG.debug("unmap_volume %s" % disk_name)

        if disk_name:
            volume_info = {'device_name': disk_name}
            # Volume info dictionary might need more info that is lost when
            # volume is detached from host so that it can be deleted
            self._disk_adapter.detach_volume_from_host(volume_info)
            # remove vdiskhostmap from SAN
            self._disk_adapter._remove_svc_hostmap(volume_uid, wwpns=None)

    @logcall
    def attach_volume_to_lpar(self, instance_name, volume_uid):
        """
        This method is used by cold migration to attach a volume
        It attaches a volume to a lpar with instance_name.
        1. Scan fibre channel fora new volume device
        2. Find hdisk for the volume
        3. Attach hdisk to vhost
        """
        device_name = self._disk_adapter._add_svc_hostmap(volume_uid)
        LOG.debug("attach_volume_to_lpar device_name %s" % device_name)
        if device_name:
            # scan fibre channel devices for new volume device (run cfgdev)
            self._disk_adapter._update_fibre_channel_devices(device_name)

            lpar = self._operator.get_lpar(instance_name)
            instance_id = lpar.get_attrib('lpar_id')
            vhost = self._operator.get_vhost_by_instance_id(instance_id)
            disk_name = (
                self._operator.get_disk_name_by_volume_uid(volume_uid))
        self._operator.set_no_reserve_on_hdisk(disk_name)
        disk_info = {'device_name': disk_name}
        self._operator.attach_disk_to_vhost(disk_info, vhost)

    @logcall
    def _create_lpar_instance(self, instance, network_info, host_stats=None,
                              resource_deltas=None, extra_specs=None):

        return super(PowerVMSANOperator, self).\
            _create_lpar_instance(instance, network_info,
                                  host_stats, resource_deltas, extra_specs)

    @logcall
    def _handle_ae_iso(self, instance, injected_files, admin_password,
                       network_info):
        """
        Handle config drive ISO
        :param instance: compute instance to spawn
        :return: (vopt_img_name, vdev_fbo_name)
        """
        vopt_img_name = None
        vdev_fbo_name = None

        iso_remote_path = self._transport_config_data(instance, injected_files,
                                                      admin_password,
                                                      network_info)
        if iso_remote_path is not None:
            vopt_img_name, vdev_fbo_name = self._attach_iso(iso_remote_path,
                                                            instance)

        return (vopt_img_name, vdev_fbo_name)

    @logcall
    def spawn(self, context, instance, image_meta, injected_files,
              admin_password, network_info, block_device_info,
              validate_volumes, attach_volumes, validate_attachment):
        """
        Spawn method with extended PowerVC boot-from-volume capability

        :param context: operation context
        :param instance: compute instance to spawn
        :param image_meta: image meta data
        :param network_info: network to plug during spawn
        :param block_device_info: established block_device_mapping before spawn
        :param validate_volumes: callback method to validate volume status
        :param attach_volumes: callback method to attach volumes before boot
        :return: None
        """

        LOG.debug("Going to spawn a new instance on SAN storage")
        spawn_start = time.time()

        try:
            try:
                lpar_inst = self._create_lpar_instance(instance,
                                                       network_info,
                                                       host_stats=None)
                # TODO(mjfork) capture the error and handle the error when the
                #             MAC prefix already exists on the
                #             system (1 in 2^28)
                LOG.debug("Creating LPAR instance '%s'" % instance['name'])
                self._operator.create_lpar(lpar_inst)
            except Exception as ex:
                LOG.exception(_("LPAR instance '%s' creation failed") %
                              instance['name'])
                # raise a new exception with mksyscfg msg
                ex_args = {'instance_name': instance['name'],
                           'error_msg': ex}
                raise exception.IBMPowerVMLPARCreationFailed(**ex_args)

            # check the volume state before boot. If fails, exception
            # will be raised and bail out.
            LOG.debug("Validating the volumes...")
            validate_volumes(context, instance['uuid'])

            # TODO(ajiang): Add support to handle ISO installation.
            # An empty boot volume has been created based on the
            # image "volume_mapping" property. We need code here
            # to transfer install iso to VIOS and attach it to
            # LPAR.We are using an manually imported cinder volume
            # as image volume for now.

            LOG.debug("Handling ae iso...")
            vopt_img_name = None
            vdev_fbo_name = None
            if not image_meta['disk_format'] == 'iso':
                vopt_img_name, vdev_fbo_name = self.\
                    _handle_ae_iso(instance,
                                   injected_files,
                                   admin_password,
                                   network_info)

            # (ppedduri,shyama): write a new method, create_iso_vopt
            # have the checks of crating vopt etc, by checking disk
            # format etc. subseq calls to _prepare_iso...
            if image_meta['disk_format'] == 'iso':
                LOG.info(_("Disk format is iso. Preparing ISO image on host."))
                vopt_dev_name = self._disk_adapter.\
                    _prepare_iso_image_on_host(context,
                                               instance,
                                               CONF.powervm_img_local_path,
                                               CONF.powervm_img_remote_path,
                                               image_meta)
                image_detail = {'vopt': vopt_dev_name}
                lpar_id = self._operator.get_lpar(instance['name'])['lpar_id']
                vhost = self._operator.get_vhost_by_instance_id(lpar_id)
                self._operator.attach_disk_to_vhost(image_detail, vhost)

            # It has been a long way to get here. Attach volumes now.
            LOG.debug("Attaching volumes...")
            attach_volumes(context, instance)
            # last check before we start instance.
            LOG.debug("Validating attachments...")
            validate_attachment(context, instance['uuid'])

            try:
                self.start_san_lpar_instance(instance, image_meta)
            except Exception as ex:
                self._vopt_cleanup(vopt_img_name, vdev_fbo_name)

            if vopt_img_name:
                LOG.debug("Unloading the ISO image")
                self._detach_iso(vopt_img_name, vdev_fbo_name, instance)

        except exception.IBMPowerVMStorageAllocFailed:
            with excutils.save_and_reraise_exception():
                # log errors in cleanup
                try:
                    self._cleanup(instance['name'], destroy_disks=False)
                except Exception:
                    LOG.exception(_('Error while attempting to '
                                    'clean up failed instance launch.'))

        spawn_time = time.time() - spawn_start
        LOG.info(_("Instance spawned in %s seconds") % spawn_time,
                 instance=instance)

    @logcall
    def start_san_lpar_instance(self, instance, image_meta):
        # Changing method name to distinguish this from super class
        # start_instance method. (Helps to restart)
        # if (('ephemeral_gb' in instance and instance['ephemeral_gb'] == 0) or
        #   ('root_gb' in instance and instance['root_gb'] != 0)):
        if image_meta['disk_format'] != 'iso':
            super(PowerVMSANOperator, self).start_instance(instance)
            greenthread.spawn(task_state_checker.check_task_state,
                              context.get_admin_context(), instance,
                              self._operator.check_dlpar_connectivity,
                              self._operator.get_refcode)
        else:
            LOG.debug("ephemeral_gb found in instance," +
                      " skipping instance start")

    @logcall
    def resize(self, context, instance, dest, instance_type, network_info,
               block_device_info):
        """Resizes the running VM to new flavor

        :param instance: the instance dictionary information
        :param dest: instance host destination
        :param instanct_type: new flavor specification
        :param network_info: network specifications of VM
        """
        LOG.debug("Entering")

        pvm_op = self._operator

        eph_volume_id = None
        for bdm in block_device_info['block_device_mapping']:
            if bdm['delete_on_termination'] is True:
                eph_volume_id = bdm['connection_info']['serial']

        # (rakatipa) Incase of SAN not returning any disk_info
        # except when requested disk size is less
        # and if  eph volume is none
        disk_info = {}

        # Resize memory and processer
        host_stats = self.get_host_stats(refresh=True)
        delta_mem, delta_vcpus, delta_cpus_units, desired_proc_compat = \
            self._find_resize_deltas(instance, instance_type)
        resource_deltas = {'delta_mem': delta_mem,
                           'delta_vcpus': delta_vcpus,
                           'delta_cpus_units': delta_cpus_units,
                           'desired_proc_compat': desired_proc_compat}
        instance['memory_mb'] = instance_type['memory_mb']
        instance['vcpus'] = instance_type['vcpus']
        try:
            updated_lpar = self._create_lpar_instance(
                instance, network_info, host_stats=host_stats,
                resource_deltas=resource_deltas,
                extra_specs=instance_type['extra_specs'])
        except Exception as ex:
            LOG.error(_('Cannot resize virtual machine: %s')
                      % instance['name'])
            LOG.exception(_('Error resizing virtual machine: %s') % ex)
            raise ex

        # Resize disk
        # if disk resize fails, we won't continue on with other resize
        # either. we do the resize of disk first because when disk
        # size could be out of sync without us knowing since
        # it's not part of the periodic update
        # (rakatipa-8921) Moved the resize volume after validations
        if eph_volume_id:
            old_size = instance['root_gb']
            size = instance_type['root_gb']
            if size > old_size:
                #
                # Issue 6721: Resize design issue. Now we are
                # going to call cinder directly and the extend
                # volume from cinder will throw exception in bulk.
                #
                # Since we have to set the VM into the error state
                # we are going to catch generic exception and throw an
                # exception in case of any failure of the disk resize.
                #
                try:
                    self._disk_adapter.resize_volume(context,
                                                     eph_volume_id, size)
                except Exception as ex:
                    LOG.error(_('Cannot resize virtual machine: %s')
                              % instance['name'])
                    LOG.exception(_('Error resizing virtual machine %(name)s. '
                                    'Extending the volume failed with the '
                                    'following exception: %(ex)s')
                                  % dict(name=instance['name'], ex=ex))
                    raise ex
            else:
                disk_info = {'is_resize': 'true',
                             'root_gb': old_size,
                             'ephemeral_gb': instance['ephemeral_gb']}
        else:
            disk_info = {'is_resize': 'true',
                         'root_gb': instance['root_gb'],
                         'ephemeral_gb': instance['ephemeral_gb']}
        try:
            # TODO - This updates the profile
            # Is this the expected behavior for both
            # active and inactive lpars?
            pvm_op.update_lpar(updated_lpar)
        except Exception as ex:
            # If new disk size is different than old disk size and
            # we made it to this point, it means resizing the logical
            # partition failed, but resizing the disk succeeded.
            # Update the DB with the new disk size.
            if size != old_size:
                conductor.API().instance_update(
                    get_admin_context(),
                    instance['uuid'],
                    root_gb=size)
            LOG.error(_('Cannot resize virtual machine: %s')
                      % instance['name'])
            LOG.exception(_('Error resizing virtual machine: %s') % ex)
            raise ex

        LOG.debug("Exiting")
        return disk_info

    @logcall
    def _check_can_live_migrate_destination(self, ctxt, instance_ref,
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
        # Get host information and add to dest_check_data
        # migrate_data dictionary
        conn_data = self._operator.connection_data
        hostname = conn_data.host
        username = conn_data.username
        port = conn_data.port
        wwpns = self._operator.get_wwpns()

        # Add the password outside of the 'migrate_data' dictionary.
        # This ensures it is only "seen" by the source validation, and will
        # not propigate through all of the migration methods.
        # The password must be encrypted via STG defect 22143
        #encrypted_pwd = EncryptHandler().encode(conn_data.password) ###xuejinguo comment

        # Get name of management system from destination
        man_sys_name = self._operator.get_management_sys_name()

        # [10227: Ratnaker]Changing host to host_display_name to be
        # consistent with gui messages and for usability.
        # This property change reflects only in notifications
        migrate_data = {'dest_hostname': hostname,
                        'dest_sys_name': CONF.host_display_name,
                        'dest_username': username,
                        'dest_password': conn_data.password, #encrypted_pwd xuejinguo
                        'dest_port': port,
                        'man_sys_name': man_sys_name,
                        'dest_wwpns': wwpns}

        return migrate_data

    @logcall
    def _check_can_live_migrate_source(self, ctxt, instance_ref,
                                       dest_check_data):
        """Check if it is possible to execute live migration.

        This checks if the live migration can succeed, based on the
        results from check_can_live_migrate_destination.

        :param context: security context
        :param instance_ref: nova.db.sqlalchemy.models.Instance
        :param dest_check_data: result of check_can_live_migrate_destination
        """
        pvm_op = self._operator
        conn_data = pvm_op.connection_data
        instance_name = instance_ref['name']
        source_wwpns = self._operator.get_wwpns()

        # Verify the destination host does not equal the source host
        source_host = conn_data.host
        if (source_host == dest_check_data['dest_hostname']):
            error = (_("Cannot live migrate %s because the " +
                       "destination server is the same as the source server") %
                     instance_name)
            LOG.exception(error)
            raise exception.IBMPowerVMLiveMigrationFailed(error)

        # check the default_ephemeral_device in the instance_ref
        # Paxes used default_ephemeral_device to store the
        # VM boot disk's UID. For Paxes 1.1.0.0, only
        # one ephemeral disk is supported per VM.
        ephemeral_uid = instance_ref['default_ephemeral_device']
        if not ephemeral_uid:
            error = (_("Cannot live migrate %(instance_name)s because the "
                       "ephemeral disk is not defined in the instance") %
                     locals())
            LOG.exception(error)
            raise exception.IBMPowerVMLiveMigrationFailed(error)

        # Paxes will establish hostmapping and set no_reserve for the
        # VM's attaced volumes on the target during live partition
        # migration. There are two type of volumes supported by Paxes
        # 1. ephemeral volume:  VM's boot disk(only one for Paxes)
        # 2. attached cinder volumes.
        # Any other volumes that user manually attached to the VM
        # will not be covered by Paxes LPM pre_live_migration code.
        # User has to manually establish hostmapping and set no_reserve
        # for those non-Paxes managed volumes before LPM.

        # Add connection data to inner dictionary
        dest_check_data['migrate_data'] = {}
        copy_keys = ['dest_hostname', 'dest_username',
                     'dest_password', 'dest_wwpns']
        for dest_key in copy_keys:
            dest_check_data['migrate_data'][dest_key] = \
                dest_check_data[dest_key]

        dest_check_data['migrate_data']['source_wwpns'] = source_wwpns

        # Extract destination information and save to global
        # dictionary to be accessed by _live_migration on source
        sys_name = dest_check_data['man_sys_name']
        dest_host = dest_check_data['dest_hostname']
        dest_user = dest_check_data['dest_username']
        dest_wwpns = dest_check_data['dest_wwpns']
        inst_name = instance_ref['name']

        live_migration_data[inst_name] = {}
        inst_migration_data = {'man_sys_name': sys_name,
                               'dest': dest_host,
                               'user': dest_user,
                               'dest_wwpns': dest_wwpns,
                               'source_wwpns': source_wwpns}
        live_migration_data[inst_name] = inst_migration_data

        # The password is stored encrypted (STG defect 22143)
        encrypted_pwd = dest_check_data.get('dest_password')
        destination_password = encrypted_pwd
        #destination_password = EncryptHandler().decode(encrypted_pwd) #xuejinguo comment

        # Create Connection objects for both source & destination
        dest_pvm_conn = common.Connection(dest_host,
                                          dest_user,
                                          destination_password,
                                          dest_check_data['dest_port'])

        # dest_check_data will pass to pre_live_migration() running on
        # the target host. No need to collect attached cinder volume
        # here. It will be passed to pre_live_migration in block_device_info
        # parameter.

        # Make sure sshkey exchange is setup correctly between source and
        # destination host involved in LPM.
        try:
            common.ensure_vios_to_vios_auth(
                self._operator.connection_data.host,
                dest_pvm_conn,
                self._operator.connection_data)
            # Make sure sshkey exchange is established between source
            # and destination hosts
            LOG.debug("Setup sshkey exchange between src_host: "
                      "%(source_host)s and dest_host: %(dest_host)s" %
                      locals())
        except Exception:
            with excutils.save_and_reraise_exception():
                msg = (_("Unable to exchange sshkey between source host: "
                         "%(source_host)s and destination host: %(dest_host)s")
                       % locals())
                LOG.exception(msg)

        dest_check_data['refresh_conn_info'] = True   # Issue 6111
        return dest_check_data

    @logcall
    def _check_can_live_migrate_destination_cleanup(self,
                                                    ctxt,
                                                    dest_check_data):
        """Do required cleanup on dest host after
           check_can_live_migrate_destination

        :param ctxt: security context
        :param dest_check_data: result of check_can_live_migrate_destination
        """
        pass

    @logcall
    def _pre_live_migration(self, context, instance,
                            block_device_info, network_info,
                            migrate_data=None):
        """Prepare live migration and checks
        This is running on the target host before LPM starts.
        There are several things to prepare on the target host
        in order to successfully migrate the VM.
        1.find all the attached cinder volumes from block_device_info
           and discover it on the target host and set no reserve.
           The hostmapping for cinder volumes have been setup by
           nova.compute.manager.pre_live_migration() before calling
           PowerVM driver's pre_live_migration().
        2. call plug_vifs() on target host to enable VLAN bridging
           for VM.

        NOTES:
        If there are any VLANs or disks attached to VM that are not
        managed by PowerVC, user needs to manually establish mapping on
        the target host before LPM started.

        :param context: security context
        :param instance: dict of instance data
        :param block_device_info: instance block device information
        :param network_info: instance network information
        :param migrate_data: implementation specific data dict.
        """

        # establish hostmapping of ephemeral and cinder volumes on the
        # target host.
        self._map_volumes_to_target_host(instance, block_device_info,
                                         migrate_data.get("migrate_data"))

        # Plug VLAN on the target host
        self.plug_vifs(instance, network_info, raise_err=True)

    @logcall
    def _map_volumes_to_target_host(self, instance, block_device_info,
                                    migrate_data):
        """
        Establish hostmap and no reserve for ephemeral disk and attached
        cinder volumes on the target host before live migration.
        This function is running on target host.

        :param instance: VM instance
        :param block_device_info: block_device mapping for cinder volume on
                                  source host
        :param migrate_data: source and target host related migration data.
        """

        # continue to process attached cinder volumes if any
        if not block_device_info:
            # No attached cinder volume info. Just return.
            return

        bdmaps = block_device_info.get("block_device_mapping")
        if(not bdmaps or not migrate_data or
           not migrate_data.get("source_wwpns") or
           not migrate_data.get("dest_wwpns")):
            # no attached cinder volume.
            return

        source_wwpns = migrate_data.get("source_wwpns")
        target_wwpns = migrate_data.get("dest_wwpns")

        # Validate block_disk_mapping
        for bdm in bdmaps:
            volume_type = bdm['connection_info']['driver_volume_type']
            if volume_type != "fibre_channel":
                error = (_("_map_volumes_to_target_host() pre live "
                           "migration: Unsupported Volume connection: "
                           " %(conn_type)s") %
                         {'conn_type': volume_type})
                LOG.exception(error)
                raise exception.IBMPowerVMLiveMigrationFailed(error)

            # Discover the attached cinder volumes and set no reserve.
            # Stale disk will be handled as well
            self._disk_adapter.discover_volumes_on_destination_host(
                source_wwpns, target_wwpns, bdmaps)

    @logcall
    def _live_migration(self, ctxt, instance_ref, dest, post_method,
                        recover_method, migrate_data, block_migration=False):
        """Live migrate from one host to another

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
        pvm_op = self._operator
        inst_name = instance_ref['name']
        lpar_id = pvm_op.get_lpar(inst_name).get_attrib('lpar_id')
        vhost = pvm_op.get_vhost_by_instance_id(lpar_id)
        disknames = pvm_op.get_disk_names_for_vhost(vhost)

        # Extract destination info from global dictionary
        man_sys_name = live_migration_data[inst_name]['man_sys_name']
        dest_host_ip = live_migration_data[inst_name]['dest']
        user = live_migration_data[inst_name]['user']
        dest_wwpns = live_migration_data[inst_name]['dest_wwpns']

        # Update migrate_data booleans such that rollback on
        # destination is called
        migrate_data['is_volume_backed'] = True
        migrate_data['is_shared_storage'] = False

        # Raise exception if backing device is logical volume
        for diskname in disknames:

            # Check the reserve policy of hdisk
            reserve_policy = pvm_op.get_hdisk_reserve_policy(diskname)
            if (reserve_policy != 'no_reserve'):
                recover_method(ctxt, instance_ref, dest,
                               block_migration, migrate_data)
                error = (_("Cannot live migrate %(instance)s because " +
                           "the reserve policy on %(hdisk)s is set to " +
                           "%(policy)s. It must be set to no_reserve.") %
                         {'instance': inst_name,
                          'hdisk': diskname,
                          'policy': reserve_policy})
                LOG.exception(error)
                raise exception.IBMPowerVMLiveMigrationFailed(error)

            # Check for logical volumes
            if pvm_op.is_device_logical_volume(diskname):
                recover_method(ctxt, instance_ref, dest,
                               block_migration, migrate_data)
                error = (_("Cannot live migrate %s because it " +
                           "contains a logical volume.") % inst_name)
                LOG.exception(error)
                raise exception.IBMPowerVMLiveMigrationFailed(error)

        dlpar, rmc_state = pvm_op.check_dlpar_connectivity(inst_name)

        # Do not migrate if rmc_state is not in 'active' state
        if rmc_state != 'active':
            recover_method(ctxt, instance_ref, dest,
                           block_migration, migrate_data)
            error = (_("Cannot live migrate %(inst)s because its rmc " +
                       "state is %(rmc)s. The rmc state must be active.") %
                     {'inst': inst_name,
                      'rmc': rmc_state})
            LOG.exception(error)
            raise exception.IBMPowerVMLiveMigrationFailed(error)

        # Check the DLPAR state for LPAR to be migrated
        if not dlpar:
            recover_method(ctxt, instance_ref, dest,
                           block_migration, migrate_data)
            error = (_("Cannot live migrate %s because DLPAR " +
                       "is not enabled.") % inst_name)
            LOG.exception(error)
            raise exception.IBMPowerVMLiveMigrationFailed(error)

        # Remove the vopt device off of the instance
        LOG.debug('DLPAR connectivity has been established for instance ' +
                  '%s. Removing vopt prior to migration validate.' % inst_name)
        self._operator.remove_vopt_media_by_instance(instance_ref)

        start_time = None
        try:
            # Run migrate validate on source
            pvm_op.live_migrate_validate(man_sys_name,
                                         inst_name,
                                         dest_host_ip,
                                         user)

            start_time = time.time()

            # Run migrate to new host if all checks pass
            pvm_op.live_migrate_lpar(man_sys_name, inst_name,
                                     dest_host_ip, user)

        # Call recover method and cleanup live migration
        except Exception as e:
            recover_method(ctxt, instance_ref, dest,
                           block_migration, migrate_data)
            raise exception.IBMPowerVMLiveMigrationFailed(e)

        timeout = CONF.ibmpowervm_live_migration_timeout
        migr_state = pvm_op.get_live_migration_state(inst_name)
        while migr_state != 'Migration Complete':
            source_manage_name = pvm_op.get_management_sys_name()
            curr_time = time.time()
            if (curr_time - start_time) > timeout:
                pvm_op.stop_live_migration(source_manage_name, inst_name)
                pvm_op.recover_live_migration(source_manage_name, inst_name)
                recover_method(ctxt, instance_ref, dest,
                               block_migration, migrate_data)
                error = (_("Live migration for instance %s " +
                           "timed out.") % inst_name)
                LOG.exception(error)
                raise exception.IBMPowerVMLiveMigrationFailed(error)

            try:
                pvm_op.check_lpar_exists(inst_name)
                migr_state = pvm_op.get_live_migration_state(inst_name)
            except exception.IBMPowerVMLPARInstanceDoesNotExist:
                break

            time.sleep(1)

        # Wait indefinitely for state to change from Migration Complete
        # to LPAR doesn't exist (actually complete)
        try:
            while pvm_op.check_lpar_exists(inst_name):
                time.sleep(1)
        except exception.IBMPowerVMLPARInstanceDoesNotExist:
            pass

        # Call post_method to update host in OpenStack
        # and finish live-migration
        post_method(ctxt, instance_ref, dest, block_migration, migrate_data)

    @logcall
    def _post_live_migration_at_destination(self, ctxt, instance_ref,
                                            network_info, block_migration,
                                            block_device_info=None):
        """Post live migration on the destination host

        :param ctxt: security context
        :param instance_ref: dictionary of info for instance
        :param network_info: dictionary of network info for instance
        :param block_migration: boolean for block migration
        """
        conductor.API().instance_update(ctxt,
                                        instance_ref['uuid'],
                                        task_state='activating')
        # Spawn a thread to wait for RMC to become active
        greenthread.spawn(task_state_checker.check_task_state,
                          ctxt, instance_ref,
                          self._operator.check_dlpar_connectivity,
                          self._operator.get_refcode)

    @logcall
    def _rollback_live_migration_at_destination(self, context, instance):
        """Roll back a failed migration

        :param context: security context
        :param instance: instance info dictionary
        """

        inst_name = instance['name']
        # this function is called on error path, get some
        # detailed logging.
        LOG.error(_("_rollback_live_migration_at_destination: "
                    "instance: %(instance)s context: %(context)s") %
                  {'instance': instance,
                   'context': context})
        # Delete the lpar if it exists.
        # Do not destroy the disks.  We'll carefully clean them up below.
        try:
            self.destroy(inst_name, False)
        except Exception:
            LOG.warn(_("Unable to remove %s on destination. "
                       "It may already have been removed.") % inst_name)

    @logcall
    def remove_volume_device_at_destination(self, context, instance, cinfo):
        """ remove volume device at destination during LPM rollback

        :param context: security context
        :param instance: instances that failed migration
        :param cinfo: block_device_mapping['connection_info']
        """
        if not cinfo:
            return
        try:
            LOG.debug("Remove device at destination during rollback. "
                      "cinfo: %(cinfo)s" % locals())
            aix_conn = self._operator.get_volume_aix_conn_info(cinfo['data'])
            if not aix_conn:
                return

            volume_info = self._operator.get_devname_by_aix_conn(
                aix_conn, all_dev_states=True)
            self._operator.remove_disk(volume_info['device_name'])
        except Exception:
            # It is okay to move on even it fails to remove volume device
            # at the destination host during migration rollback.
            pass

    def image_attach_volume(self, image_meta, image_data, 
                            connection_info, encryption=None):
        
        try:
            volume_data = connection_info['data']

            LOG.info(_("Attaching volume_id %s") % volume_data['volume_id'])
            # Before run run_cfg_dev, check whether there is any
            # stale disk with the same LUN id. If so, remove it.

            self._disk_adapter.handle_stale_disk(volume_data)

            # limit the cfgdev run target. For a multiport device,
            # the most efficient way for device discovery is run
            # against its PHB(pci bus)
            # fcs_parent looks like {'pci0':['fcs0','fcs1']}
            fcs_parent = self._operator.get_fcs_parent_devices()
            for phb in fcs_parent:
                self._operator.run_cfg_dev(phb)
            LOG.debug("config devices %s" % fcs_parent)

            aix_conn = self._operator.get_volume_aix_conn_info(volume_data)
            attach_info = self._operator.get_devname_by_aix_conn(aix_conn)
            # Cinder volume attached on the soure VIOS needs to be
            # LPM ready. no_reserve has to be set before a cinder
            # volume is attached to VM. Otherwise reservation cannot
            # be changed unless the volume is detached and reattached
            # again.

            reserve_policy = self._operator.get_hdisk_reserve_policy(
                attach_info['device_name'])
            if reserve_policy != 'no_reserve':
                # set reserve_policy to no reserve in order to be
                # LPM capable. The reserve policy has to be set before
                # the disk is opened by vscsi driver to attach to The
                # host. So it cannot be done during migration time.
                self._operator.set_no_reserve_on_hdisk(
                    attach_info['device_name'])
            LOG.debug("device to attach: %s" % attach_info['device_name'])
            image_data['hdisk_id'] = attach_info['device_name']
            self.image_create_update(image_meta, image_data)
        except Exception as e:
            volume_id = connection_info['data']['volume_id']
            raise exception.\
                IBMPowerVMVolumeAttachFailed_v2(volume_id,e)


class BaseOperator(object):

    """Base operator for IVM and HMC managed systems."""

    def __init__(self, connection):
        """Constructor.

        :param connection: common.Connection object with the
                           information to connect to the remote
                           ssh.
        """
        self._connection = None
        self.connection_data = connection
        self._pool = SSHConnectionPool.\
            get_pool_instance(connection.host, connection.port,
                              connection.username,
                              connection.password,
                              CONF.ibmpowervm_known_hosts_path,
                              min_pool_size=1,
                              max_pool_size=int(CONF.
                                                powervm_max_ssh_pool_size),
                              wait_if_busy=True,
                              wait_interval=-1, key_file=None,
                              timeout=constants.
                              IBMPOWERVM_CONNECTION_TIMEOUT)
        self._lpar_name_map = dict()

    @contextlib.contextmanager
    def _get_connection(self):
        conn = self._pool.get_connection()
        try:
            if conn.is_closed() is True:
                conn.open_connection()
            yield conn
        finally:
            self._pool.release_connection(conn)

    # Override the super _set_connection so we can save the connection info
    # with the connection.  This allow us to retry connecting.
    def _set_connection(self):
        if self._connection is None:
            self._connection = common.ssh_connect(self.connection_data)
            # Save the connection data in the connection so we can do retries
            self._connection.conn_data = self.connection_data
        else:
            # The conn is set, but is it good?
            self._connection = common.check_connection(self._connection)

    def refresh_lpar_name_map(self, instances):
        """Refreshes the Map of OpenStack VM Names to the actual LPAR Names"""
        for instance in instances:
            # If the VM Name is set in the PowerSpecs, then build a mapping
            if instance.get('power_specs') is not None:
                if instance['power_specs'].get('vm_name') is not None:
                    self._lpar_name_map[instance['name']] = \
                        instance['power_specs']['vm_name']

    def get_actual_lpar_name(self, instance_name):
        """Returns the actual LPAR Name for the OpenStack VM Name given"""
        return self._lpar_name_map.get(instance_name, instance_name)

    def get_lpar(self, instance_name, resource_type='lpar'):
        """Return a LPAR object by its instance name.

        :param instance_name: LPAR instance name
        :param resource_type: the type of resources to list
        :returns: LPAR object
        """
        # Get the LPAR Name if the OpenStack Name is different than it
        lpar_name = self.get_actual_lpar_name(instance_name)
        cmd = self.command.lssyscfg('-r %s --filter "lpar_names=%s"'
                                    % (resource_type, lpar_name))
        output = self.run_vios_command(cmd)
        if not output:
            return None
        lpar = LPAR.load_from_conf_data(output[0])
        return lpar

    def list_lpar_instances(self):
        """List all existent LPAR instances names.

        :returns: list -- list with instances names.
        """
        lpar_names = self.run_vios_command(self.command.lssyscfg(
            '-r lpar -F name'))
        if not lpar_names:
            return []
        # Return a list of OpenStack VM Names for the LPAR's that exist
        os_name_map = dict((v, k) for k, v in self._lpar_name_map.iteritems())
        return [os_name_map.get(name, name) for name in lpar_names]

    def list_all_lpars(self):
        """
        list all lpars in ivm for on boarding
        """
        cmd = self.command.lssyscfg('-r lpar')
        output = self.run_vios_command(cmd)
        if not output:
            return None

        lparLst = []
        for outStr in output:
            lpar = LPAR.load_from_conf_data(outStr)
            lparLst.append(lpar)

        return lparLst

    def get_vcpu_info(self, instance_name):
        """
        Get VCPU info.
        :returns: tuple - vcpu info (curr_procs,pend_procs,pend_proc_units)
        """
        # Get the LPAR Name if the OpenStack Name is different than it
        lpar_name = self.get_actual_lpar_name(instance_name)
        cmd = self.command.lshwres(
            '-r proc --level lpar --filter "lpar_names=%s" -F '
            'curr_procs,pend_procs,pend_proc_units'
            % (lpar_name))
        output = self.run_vios_command(cmd)
        curr_procs, pend_procs, pend_proc_units = output[0].split(',')
        if pend_proc_units == 'null':
            pend_proc_units = int(pend_procs) * 1.0

        return {'curr_procs': curr_procs,
                'pend_procs': pend_procs,
                'pend_proc_units': pend_proc_units}

    def get_memory_info_lpar(self, instance_name):
        """
        Get VCPU info.
        :returns: tuple - memory info (curr_mem, pend_mem)
        """
        # Get the LPAR Name if the OpenStack Name is different than it
        lpar_name = self.get_actual_lpar_name(instance_name)
        cmd = self.command.lshwres(
            '-r mem --level lpar --filter "lpar_names=%s" -F '
            'run_mem,pend_mem'
            % (lpar_name))
        output = self.run_vios_command(cmd)
        curr_mem, pend_mem = output[0].split(',')
        return {'curr_mem': curr_mem,
                'pend_mem': pend_mem}

    @logcall
    def get_wwname_by_disk(self, disk_name):
        """
        Get wwpns

        returns: World wide port names
        """
        cmd = self.command.lsdev('-dev %s -attr ww_name' % disk_name)
        output = self.run_vios_command(cmd)
        # Returned output looks like this: ['value', '', '1']
        if output:
            return output[2]

    def get_stor_type_of_disk(self, disk_name):
        """
        Get parent info of disk
        Helps to seggregate the SAN or local

        returns: World wide port names
        """
        cmd = ''.join(['ioscli lspath -dev ',
                       '%s -field parent' % disk_name])
        output = None
        try:
            output = self.run_vios_command(cmd)
        except Exception as ex:
            LOG.exception(_('Problem while command execution '))
            LOG.debug(" Exception while getting npiv storage info : %s"
                      % ex)
            return output
        # Returned output looks like this: ['value', '', '1']
        if output:
            return output[2]

    def get_disk_of_lv(self, dev_name):
        """
        Get parent disk of LV

        returns: disk name
        """
        cmd = ''.join(['ioscli lslv -pv ',
                       '%s -field PV' % dev_name])
        output = self.run_vios_command(cmd)
        # Returned output looks like this: ['value', '', '1']
        if output:
            return output[2]

    def get_disk_uid_by_name(self, disk_name):
        """
        Return the unique id of the disk by its name.

        :param disk_name: The disk name
        """
        LOG.debug("Entering (disk_name = %s)" % disk_name)
        output = \
            self.run_command(
                'ioscli lsdev -dev %s -attr unique_id | tail -1' %
                disk_name)
        if not output:
            return None
        uid = None
        if len(output[0]):
            match = re.search(r'^[0-9]{5}([0-9A-F]{32}).+$', output[0])
            uid = match.group(1)

        LOG.debug("Exiting (uid = %s)" % uid)
        return uid

    def get_root_gb_disk_from_dict(self, vhost_id, disk_dict):
        """
        Return the root gb by iterating through the prepared dict
        :param vhost _id: vhost id
        :param disk_dict: Prepared dict of storage info of all vhosts

        returns: root_gb: boot info
        """

        is_local = False
        if CONF.host_storage_type.lower() == 'local':
            is_local = True
        disk_names = self.get_disk_names_for_vhost_frm_dict(vhost_id,
                                                            disk_dict,
                                                            is_local)
        disk_size = None
        if len(disk_names) > 0:
            commands = ['oem_setup_env',
                        'bootinfo -s %s' % disk_names[0],
                        'exit']
            disk_size = self.run_interactive(commands)

        if disk_size is None:
            return 0

        root_gb = int(disk_size[0]) / 1024
        return root_gb

    def get_stor_type(self, device):
        # (rakatipa) Get the parent disk if it is lv
        # use the disk to get the parent storage type
        # For lv it starts with 'sas'
        # for physical it starts with 'fsc'
        st_type = None
        dev = device[1]
        if dev is not None:
            vtd = device[0]
            if vtd[:5].lower() == 'vtopt' and len(device) > 2:
                if len(device) >= 5 and device[5] is not None:
                    start = 5
                    index = None
                    dev = None
                    while start <= len(device):
                        if index:
                            tmp = device[start]
                            if int(tmp, 16) < int(index, 16):
                                index = tmp
                                dev = device[start - 1]
                        else:
                            dev = device[start - 1]
                            index = device[start]
                        start += 3

            stor_type = None
            st_type = None
            disk = None
            try:
                disk = self.get_disk_of_lv(dev)
                if disk:
                    stor_type = "lv"
            except Exception as ex:
                LOG.debug("lslv command failed, must be san based disk")
                LOG.debug(" Exception while getting lslv -pv : %s"
                          % ex)

            if disk is None:
                stor_type = self.get_stor_type_of_disk(dev)

            if stor_type is not None:
                st_type = stor_type[:3]
        return st_type

    def get_vscsi_storage_info(self):
        """
        Returns vscsi related storage information
        storage_info ={'0x00000002': ['vtscsi2','vopt2']}
        """
        cmd = self.command.lsmap('-all -field clientid vtd backing lun -fmt :')
        output = self.run_vios_command(cmd)
        if not output:
            return None

        storage_info = {}
        for outstr in output:
            lst = outstr.split(':')
            instance_hex = lst.pop(0)
            storage_info[instance_hex] = lst

        return storage_info

    def get_npiv_storage_info(self):
        """
        Returns npiv related storage information
        storage_info={'0x00000002': ['fcs0']}
        """
        cmd = self.command.lsmap('-all -npiv -field clntid fc -fmt :')
        output = None
        try:
            output = self.run_vios_command(cmd)
        except Exception as ex:
            LOG.exception(_('Problem while command execution '))
            LOG.debug(" Exception while getting npiv storage info : %s"
                      % ex)

        if not output:
            return None

        storage_info = {}
        for outstr in output:
            lst = outstr.split(':')
            instance_hex = lst.pop(0)
            storage_info[instance_hex] = lst

        return storage_info

    def get_misc_lpar_info(self):
        """
        Returns curr_shared_proc_pool_id and mem_mode for the lpar
        """
        lpar_info = {}
        cmd = self.command.lshwres('-r proc --level lpar -F lpar_id,'
                                   'curr_shared_proc_pool_id')
        output = self.run_vios_command(cmd)
        if not output:
            return None

        pool_info = dict(item.split(",") for item in list(output))

        # cmd_mem = "lslparutil -r lpar -F lpar_id,mem_mode"
        # 'lslparutil' is supported for only a maximum of 40 LPARs on an IVM
        cmd_mem = self.command.lshwres('-r mem --level lpar -F lpar_id,'
                                       'mem_mode')
        mem_output = self.run_vios_command(cmd_mem)

        mem_info = dict(item.split(",") for item in list(mem_output))
        for lpar in pool_info:
            proc_pool_id = 0
            if pool_info[lpar] != 'null':
                proc_pool_id = int(pool_info[lpar])
            lpar_info[lpar] = {'curr_shared_proc_pool_id': proc_pool_id,
                               'mem_mode': mem_info[lpar]}

        return lpar_info

    def create_lpar(self, lpar):
        """Receives a LPAR data object and creates a LPAR instance.

        :param lpar: LPAR object
        """
        conf_data = lpar.to_string()
        self.run_vios_command(self.command.mksyscfg('-r lpar -i "%s"' %
                                                    conf_data))

    def start_lpar(self, instance_name):
        """Start a LPAR instance.

        :param instance_name: LPAR instance name
        """
        # Get the LPAR Name if the OpenStack Name is different than it
        lpar_name = self.get_actual_lpar_name(instance_name)
        self.run_vios_command(self.command.chsysstate('-r lpar -o on -n %s'
                                                      % lpar_name))

    def stop_lpar(self, instance_name, timeout=30):
        """Stop a running LPAR.

        :param instance_name: LPAR instance name
        :param timeout: value in seconds for specifying
                        how long to wait for the LPAR to stop
        """
        # Get the LPAR Name if the OpenStack Name is different than it
        lpar_name = self.get_actual_lpar_name(instance_name)
        cmd = self.command.chsysstate('-r lpar -o shutdown --immed -n %s' %
                                      lpar_name)
        self.run_vios_command(cmd)

        # poll instance until stopped or raise exception
        lpar_obj = self.get_lpar(instance_name)
        wait_inc = 1  # seconds to wait between status polling
        start_time = time.time()
        while lpar_obj['state'] != 'Not Activated':
            curr_time = time.time()
            # wait up to (timeout) seconds for shutdown
            if (curr_time - start_time) > timeout:
                raise exception.IBMPowerVMLPAROperationTimeout(
                    operation='stop_lpar',
                    instance_name=instance_name)

            time.sleep(wait_inc)
            lpar_obj = self.get_lpar(instance_name)

    def remove_lpar(self, instance_name):
        """Removes a LPAR.

        :param instance_name: LPAR instance name
        """
        # Get the LPAR Name if the OpenStack Name is different than it
        lpar_name = self.get_actual_lpar_name(instance_name)
        self.run_vios_command(self.command.rmsyscfg('-r lpar -n %s'
                                                    % lpar_name))

    def get_vhost_by_instance_id(self, instance_id):
        """Return the vhost name by the instance id.

        :param instance_id: LPAR instance id
        :returns: string -- vhost name or None in case none is found
        """
        pass

    def get_virtual_eth_adapter_id(self):
        """Virtual ethernet adapter id.

        Searches for the shared ethernet adapter and returns
        its id.

        :returns: id of the virtual ethernet adapter.
        """
        cmd = self.command.lsmap('-all -net -field sea -fmt :')
        output = self.run_vios_command(cmd)
        for str in output:
            if not (str == " " or str == ""):
                sea = str
                break
        cmd = self.command.lsdev('-dev %s -attr pvid' % sea)
        output = self.run_vios_command(cmd)
        # Returned output looks like this: ['value', '', '1']
        if output:
            return output[2]

        return None

    def get_hostname(self):
        """Returns the managed system hostname.

        :returns: string -- hostname
        """
        output = self.run_vios_command(self.command.hostname())
        return output[0]

    def get_disk_name_by_vhost(self, vhost):
        """Returns the first disk name attached to a vhost.

        :param vhost: a vhost name
        :returns: string -- disk name
        """
        names = self.get_disk_names_for_vhost(vhost)
        if names:
            return names[0]

        return None

    def get_disk_names_for_vhost(self, vhost, local=False):
        """Return the disk names attached to a specific vhost

        :param vhost: vhost name
        :returns: list of disk names attached to the vhost,
                  ordered by time of attachment
                  (first disk should be boot disk)
                  Returns None if no disks are found.
        """
        command = self.command.lsmap(
            '-vadapter %s -field LUN backing -fmt :' % vhost)
        output = self.run_vios_command(command)
        if output:
            # assemble dictionary of [ lun => diskname ]
            lun_to_disk = {}
            remaining_str = output[0].strip()
            while remaining_str:
                values = remaining_str.split(':', 2)
                lun_to_disk[values[0]] = values[1]
                if len(values) > 2:
                    remaining_str = values[2]
                else:
                    remaining_str = ''

            lun_numbers = lun_to_disk.keys()
            lun_numbers.sort()

            # assemble list of disknames ordered by lun number
            disk_names = []
            for lun_num in lun_numbers:
                disk_names.append(lun_to_disk[lun_num])

            return disk_names

        return None

    def attach_disk_to_vhost(self, disk_info, vhost):
        """Attach disk name to a specific vhost.

        :param disk_info: a dictionary containing
            disk information the disk name
        :param vhost: the vhost name
        """
        if 'device_name' in disk_info:
            disk = disk_info['device_name']
            cmd = self.command.mkvdev('-vdev %s -vadapter %s') % (disk, vhost)
            try:
                self.run_vios_command(cmd)
            except exception.IBMPowerVMCommandFailed as cmdex:
                exit_code = 0
                if cmdex.kwargs is not None:
                    if 'exit_code' in cmdex.kwargs:
                        exit_code = cmdex.kwargs['exit_code']
                if int(exit_code) == 1:
                    time.sleep(10)
                    self.run_vios_command(cmd)
                else:
                    raise cmdex

    def get_memory_info(self):
        """Get memory info.

        :returns: dictionary of memory info
            {total_mem, avail_mem, sys_firmware_mem, mem_region_size}
        """
        cmd = self.command.lshwres(
            '-r mem --level sys -F configurable_sys_mem,curr_avail_sys_mem,'
            'sys_firmware_mem,mem_region_size')
        output = self.run_command(cmd)
        total_mem, avail_mem, sys_firmware_mem, mem_region_size = \
            output[0].split(',')
        return {'total_mem': int(total_mem),
                'avail_mem': int(avail_mem),
                'sys_firmware_mem': int(sys_firmware_mem),
                'mem_region_size': int(mem_region_size)}

    def get_memory_info_for_lpars(self):
        """Get memory info for every lpar.

        :returns: dictionary of memory assigned to each lpar
            {lpar_name: memory_reserved}
        """
        cmd = self.command.lshwres(
            '-r mem --level lpar -F lpar_name,curr_mem')
        output = self.run_command(cmd)
        lpar_memory_info = {}
        for lpar in output:
            lpar_name, memory_reserved = lpar.split(',')
            lpar_memory_info[lpar_name] = int(memory_reserved)
        return lpar_memory_info

    def get_host_cpu_frequency(self):
        """Get the host's CPU frequency (MHz).

        :returns: the host's CPU frequency in MHz (long integer)
        """
        cmd = self.command.lslparutil('-r sys -F proc_cycles_per_second')
        output = self.run_command(cmd)
        return long(long(output[0]) / math.pow(10, 6))

    def get_host_cpu_util_data(self):
        """Get the host's CPU utilization data.

        :returns: dict of CPU utilization data whose format is:
            {'total_cycles': total cycles in period (long integer),
             'used_cycles': used cycles in period (long integer)}
        """
        cmd = self.command.lslparutil(
            '-r pool -F total_pool_cycles,utilized_pool_cycles')
        output = self.run_command(cmd)
        (total_cycles, used_cycles) = output[0].split(',')

        return {'total_cycles': long(total_cycles),
                'used_cycles': long(used_cycles)}

    def get_host_uptime(self, host):
        """
        Get host uptime.
        :returns: string - amount of time since last system startup
        """
        # The output of the command is like this:
        # "02:54PM  up 24 days,  5:41, 1 user, load average: 0.06, 0.03, 0.02"
        cmd = self.command.sysstat('-short %s' % self.connection_data.username)
        return self.run_vios_command(cmd)[0]

    def get_cpu_info(self):
        """Get CPU info.

        :returns: tuple - cpu info (total_procs, avail_procs)
        """
        cmd = self.command.lshwres(
            '-r proc --level sys -F '
            'configurable_sys_proc_units,curr_avail_sys_proc_units,'
            'max_curr_virtual_procs_per_aixlinux_lpar,'
            'max_curr_procs_per_aixlinux_lpar')
        output = self.run_vios_command(cmd)
        total_procs, avail_procs, max_vcpus, max_procs = output[0].split(',')
        return {'total_procs': float(total_procs),
                'avail_procs': float(avail_procs),
                'max_vcpus_per_aix_linux_lpar': int(max_vcpus),
                'max_procs_per_aix_linux_lpar': int(max_procs)}

    def get_disk_info(self):
        """Get the disk usage information

        :returns: tuple - disk info (disk_total, disk_used, disk_avail)
        """
        commands = ['oem_setup_env',
                    'lsvg -o',
                    'exit']
        online_vgs = self.run_interactive(commands)

        (disk_total, disk_used, disk_avail, usable_local_mb) = [0, 0, 0, 0]

        for vg in online_vgs:
            cmd = self.command.lsvg('%s -field totalpps usedpps freepps -fmt :'
                                    % vg)
            output = self.run_vios_command(cmd)
            # Output example:
            # 1271 (10168 megabytes):0 (0 megabytes):1271 (10168 megabytes)
            (d_total, d_used, d_avail) = re.findall(r'(\d+) megabytes',
                                                    output[0])
            disk_total += int(d_total)
            disk_used += int(d_used)
            disk_avail += int(d_avail)
            if usable_local_mb < int(d_avail):
                usable_local_mb = int(d_avail)

        return {'disk_total': disk_total,
                'disk_used': disk_used,
                'disk_avail': disk_avail,
                'usable_local_mb': usable_local_mb}

    def run_command(self, command, conn=None, raise_exception=True):
        ret = None
        with self._get_connection() as conn:
            try:
                ret = common.ssh_command(conn, command + ';echo $?',
                                         log_warning=False)
            except Exception as ex:
                LOG.exception(_('Problem while command execution '))
                raise exception.IBMPowerVMCommandFailed(command)

        if ret is None:
            output = ''
            err_out = _("Unexpected error while running command %s") % command
            return_code = -1
        else:
            output, err_out = ret
            if len(output) > 0:
                return_code = output.pop()

        if return_code and int(return_code) == 0:
            return output

        if raise_exception:
            ex_args = {'command': command,
                       'error': err_out,
                       'stdout': output,
                       'exit_code': int(return_code)}
            raise exception.IBMPowerVMCommandFailed(**ex_args)
        return None

    def run_vios_command(self, cmd, check_exit_code=True):
        """Run a remote command using an active ssh connection.

        :param command: String with the command to run.
        """
        #LOG.info('\nvios===========: ' + cmd)
        return self.run_command(cmd, self._connection, check_exit_code)

    def run_vios_command_as_root(self, command, check_exit_code=True):
        """Run a remote command as root using an active ssh connection.

        :param command: List of commands.
        """
        #LOG.info('\nroot=========== : ' + cmd)
        stdout = None
        stderr = None
        with self._get_connection() as conn:
            try:
                stdout, stderr = common.ssh_command_as_root(
                    conn, command, check_exit_code=check_exit_code)
            except Exception as ex:
                LOG.exception(_('Problem while command execution '))
                raise exception.IBMPowerVMCommandFailed(command)

        error_text = stderr.read()
        if error_text:
            LOG.debug("Found error stream for command \"%(command)s\":"
                      " %(error_text)s",
                      {'command': command, 'error_text': error_text})

        return stdout.read().splitlines()

    def macs_for_instance(self, instance):
        pass

    @logcall
    def update_lpar(self, lpar_info):
        """Resizing an LPAR

        :param lpar_info: dictionary of LPAR information
        """
        configuration_data = ('name=%s,min_mem=%s,desired_mem=%s,'
                              'max_mem=%s,min_procs=%s,desired_procs=%s,'
                              'max_procs=%s,min_proc_units=%s,'
                              'desired_proc_units=%s,max_proc_units=%s,'
                              'lpar_proc_compat_mode=%s' %
                              (lpar_info['name'], lpar_info['min_mem'],
                               lpar_info['desired_mem'],
                               lpar_info['max_mem'],
                               lpar_info['min_procs'],
                               lpar_info['desired_procs'],
                               lpar_info['max_procs'],
                               lpar_info['min_proc_units'],
                               lpar_info['desired_proc_units'],
                               lpar_info['max_proc_units'],
                               lpar_info['lpar_proc_compat_mode']))

        self.run_command(self.command.chsyscfg('-r prof -i "%s"' %
                                               configuration_data))

    def get_logical_vol_size(self, diskname):
        """Finds and calculates the logical volume size in GB

        :param diskname: name of the logical volume
        :returns: size of logical volume in GB
        """
        configuration_data = ("ioscli lslv %s -fmt : -field pps ppsize" %
                              diskname)
        output = self.run_vios_command(configuration_data)
        pps, ppsize = output[0].split(':')
        ppsize = re.findall(r'\d+', ppsize)
        ppsize = int(ppsize[0])
        pps = int(pps)
        lv_size = ((pps * ppsize) / 1024)

        return lv_size

    def rename_lpar(self, instance_name, new_name):
        """Rename LPAR given by instance_name to new_name

        Note: For IVM based deployments, the name is
              limited to 31 characters and will be trimmed
              to meet this requirement

        :param instance_name: name of LPAR to be renamed
        :param new_name: desired new name of LPAR
        :returns: new name of renamed LPAR trimmed to 31 characters
                  if necessary
        """

        # Get the LPAR Name if the OpenStack Name is different than it
        lpar_name = self.get_actual_lpar_name(instance_name)
        # grab first 31 characters of new name
        new_name_trimmed = new_name[:31]

        cmd = ''.join(['chsyscfg -r lpar -i ',
                       '"',
                       'name=%s,' % lpar_name,
                       'new_name=%s' % new_name_trimmed,
                       '"'])

        self.run_vios_command(cmd)

        return new_name_trimmed

    def _decompress_image_file(self, file_path, outfile_path):
        command = "/usr/bin/gunzip -c %s > %s" % (file_path, outfile_path)
        self.run_vios_command_as_root(command)

        # Remove compressed image file
        command = "/usr/bin/rm %s" % file_path
        self.run_vios_command_as_root(command)

        return outfile_path

    def _remove_file(self, file_path):
        """Removes a file on the VIOS partition

        :param file_path: absolute path to file to be removed
        """
        command = 'rm %s' % file_path
        self.run_vios_command_as_root(command)

    def check_media_library_exists(self):
        """
        Check if there is media library
        :returns: boolean - if the media library is existed
        """
        command = self.command.lsrep('')
        output = self.run_vios_command(command)
        return False if len(output) == 0 else True

    def create_media_library(self, volume_group, size_gb):
        """
        Create new media library
        :param volume_group: The name of volume group
        :param size_gb: Size of media library in GBytes
        """
        command = self.command.mkrep("-sp %s -size %sG" % (volume_group,
                                                           size_gb))
        self.run_vios_command(command)

    def create_vopt_device(self, vopt_img_name, iso_name):
        """
        Create new optical device based on the given ISO
        :param vopt_img_name: Name of the new optical device to be created
        :param iso_name: The ISO full path
        """
        command = self.command.mkvopt('-name %s -file %s -ro' %
                                      (vopt_img_name, iso_name))
        self.run_vios_command(command)

    def remove_vopt_device(self, vopt_img_name):
        """
        Remove virtual optical device, which will remove the ISO from media
        library
        :param vopt_img_name: Name of the optical device
        """
        command = self.command.rmvopt('-name %s' % vopt_img_name)
        self.run_vios_command(command)

    def remove_ovf_env_iso(self, iso_file_path):
        """
        Remove network  ovf configure file
        library
        :param iso_file_path: path of network  ovf configure file
        """
        command = ('rm -f %s' % pipes.quote(iso_file_path))
        self.run_vios_command(command)

    def create_fbo_device(self, vhost):
        """
        Create a new File Backed Optical device
        :param vhost: The vhost which FBO will be attached
        :returns: string, the FBO name or None if the creation failed
        """
        command = self.command.mkvdev('-fbo -vadapter %s' % vhost)
        try:
            output = self.run_vios_command(command)
        except exception.IBMPowerVMCommandFailed as cmdex:
            exit_code = 0
            if cmdex.kwargs is not None:
                if 'exit_code' in cmdex.kwargs:
                    exit_code = cmdex.kwargs['exit_code']
            if int(exit_code) == 1:
                time.sleep(10)
                output = self.run_vios_command(command)
            else:
                raise cmdex
        if output:
            return output[0].split(' ')[0]
        else:
            return None

    def remove_fbo_device(self, vdev_fbo_name):
        """
        Remove the file backed optical device
        :param vdev_fbo_name: Name of FBO device to be removed
        """
        command = self.command.rmvdev('-vtd %s' % vdev_fbo_name)
        try:
            self.run_vios_command(command)
        except exception.IBMPowerVMCommandFailed as cmdex:
            exit_code = 0
            if cmdex.kwargs is not None:
                if 'exit_code' in cmdex.kwargs:
                    exit_code = cmdex.kwargs['exit_code']
            if int(exit_code) == 1:
                time.sleep(10)
                self.run_vios_command(command)
            else:
                raise cmdex

    def load_vopt_device(self, vopt_img_name, vdev_fbo_name):
        """
        Load image to the target optical device
        :param vopt_img_name: Image name to be loaded
        :param vdev_fbo_name: Name of optical device
        """
        command = self.command.loadopt('-disk %s -vtd %s' %
                                       (vopt_img_name, vdev_fbo_name))
        self.run_vios_command(command)

    def unload_vopt_device(self, vdev_fbo_name, force_release=True):
        """
        Unload image from the virtual optical device
        :param vdev_fbo_name: Name of optical device to be unloaded from
        """
        if force_release:
            cmd_str = '-release -vtd %s' % vdev_fbo_name
        else:
            cmd_str = '-vtd %s' % vdev_fbo_name

        command = self.command.unloadopt(cmd_str)
        self.run_vios_command(command)

    def check_lpar_started(self, lpar_id):
        """
        Check if the LPAR has started, return True of False
        :param lpar_id: LPAR ID
        :returns: boolean - Check if the LPAR OS boot successfully
        """
        args = ('-r lpar --filter lpar_ids=%s -F refcode' %
                lpar_id)
        command = self.command.lsrefcode(args)
        output = self.run_vios_command(command)
        # TODO(ldbragst) need to add in a better way to do this
        # the given solution is too reliant on the operating system.
        # Need to come up with a better solution for checking the booted
        # state of an LPAR.
        if output is None:
            return False
        if (len(output) == 0):
            return True
        if (output[0] == 'Linux ppc64' or output[0] == ""):
            return True
        else:
            return False

    @logcall
    def remove_vopt_media_by_instance(self, instance):
        """Remove vopt device attached to LPAR by the instance id.

        :param instance_id: LPAR instance id
        """
        lpar_obj = self.get_lpar(instance['name'])
        lpar_hex_id = None
        if lpar_obj:
            lpar_id = lpar_obj['lpar_id']
            lpar_hex_id = '%#010x' % int(lpar_id)
        if lpar_hex_id is None:
            LOG.debug("Failed to get LPAR ID.")
            return
        cmd = self.command.lsmap("".join(["-all|awk '/^vhost/ "
                                 "{INI=1; VHOST=$1; LPAR=$3} "
                                 "/^VTD/ {VTD=$2} "
                                 "/^Backing device/ {HDISK=$3} "
                                 "/^Physloc/ {printf \"%s,%s,%s,%s;\", "
                                 "VHOST, HDISK, VTD, LPAR}' | grep ",
                                          lpar_hex_id]))
        # not to check the exit code because the grep might not find
        # an entry for the lpar id
        # if exit code is not 0, it will return None
        output = self.run_vios_command(cmd, check_exit_code=False)

        if output is not None:
            hdisks = [item for item in list(output[0].split(";"))
                      if item and item.split(",")[3] == lpar_hex_id]
            for disk in hdisks:
                attributes = disk.split(",")
                if len(attributes) == 4:
                    vopt_img_name = os.path.basename(attributes[1])
                    vdev_fbo_name = attributes[2]
                    if vdev_fbo_name and vdev_fbo_name.startswith("vtopt"):
                        try:
                            self.unload_vopt_device(
                                vdev_fbo_name, force_release=False)
                        except exception.IBMPowerVMCommandFailed:
                            # Wait awhile longer and then force unmount
                            time.sleep(60)
                            self.unload_vopt_device(
                                vdev_fbo_name, force_release=True)
                        self.remove_fbo_device(vdev_fbo_name)
                    if (vopt_img_name and vopt_img_name.startswith("vopt_")):
                        self.remove_vopt_device(vopt_img_name)

    def set_lpar_mac_base_value(self, instance_name, mac):
        """Set LPAR's property virtual_eth_mac_base_value

        :param instance_name: name of the instance to be set
        :param mac: mac of virtual ethernet
        """
        # Get the LPAR Name if the OpenStack Name is different than it
        lpar_name = self.get_actual_lpar_name(instance_name)
        # NOTE(ldbragst) We only use the base mac value because the last
        # byte is the slot id of the virtual NIC, which doesn't change.
        mac_base_value = mac[:-2].replace(':', '')
        cmd = ' '.join(['chsyscfg -r lpar -i',
                        '"name=%s,' % lpar_name,
                        'virtual_eth_mac_base_value=%s"' % mac_base_value])
        self.run_vios_command(cmd)


class IVMOperator(BaseOperator):

    """Integrated Virtualization Manager (IVM) Operator.

    Runs specific commands on an IVM managed system.
    """

    # For IVM managed environment, VIOS is always at lpar_id 1
    vios_lpar_id = 1

    def __init__(self, ivm_connection):
        super(IVMOperator, self).__init__(ivm_connection)

        # superclass set command but overwrite it with ours extensions
        self.command = command.IVMCommand()

    def run_interactive(self, commands, conn=None):
        output = None
        with self._get_connection() as conn:
            try:
                output = common.ssh_interactive(conn, commands)
            except Exception as ex:
                LOG.exception(_('Problem while command execution '))
                raise exception.IBMPowerVMCommandFailed(command)
        return output

    def get_host_ip_addr(self):
        """Retrieves the IP address of the hypervisor host."""
        LOG.debug("In get_host_ip_addr")
        # TODO(mrodden): use operator get_hostname instead
        hostname = CONF.powervm_mgr
        LOG.debug("Attempting to resolve %s" % hostname)
        ip_addr = socket.gethostbyname(hostname)
        LOG.debug("%(hostname)s was successfully"
                  "resolved to %(ip_addr)s" % locals())
        return ip_addr

    @logcall
    def get_volume_aix_conn_info(self, volume_data):
        """
        Generate aix device connection information based on volume_data
        information.
        :param volume_data: A dictionary of volume information. For
                            FC volume, it has target_wwn:<WWPN> and
                            target_lun:<LUNID>. WWPN is a hex string in
                            uppercase. LUNID is a decimal string.
        :ret aix_conn: An AIX connection string: "<WWPN>:<LUNID> or None"
        """

        aix_conn = []
        if not volume_data:
            return None
        # form the aix conn
        try:
            for wwn in volume_data['target_wwn']:
                if volume_data['target_lun'] == '0':
                    lunid = '0'
                else:
                    lunid = (hex(int(volume_data['target_lun'])).lstrip('0x')
                             + '000000000000')
                aix_conn.append(wwn.lower() + ',' + lunid)

        except Exception as e:
            LOG.error(_("Failed to convert volume_data to conn: %s") % e)
            return None
        return aix_conn

    @logcall
    def get_volumes_by_vhost_from_dict(self, vhost_id, disk_dict):
        """
        Get Attached Volume Information
        :param vhost _id: vhost id
        :param disk_dict: Prepared dict of storage info of all vhosts

        returns: Disk Names and UID's for each Volume
        """
        volumes = []
        disk_names = self.get_disk_names_for_vhost_frm_dict(vhost_id,
                                                            disk_dict)
        for disk_name in disk_names:
            unique_id = self.get_disk_uid_by_name(disk_name)
            vol = {'name': disk_name, 'unique_device_id': unique_id}
            volumes.append(vol)

        return volumes

    @logcall
    def get_devname_by_aix_conn(self, conn_info, all_dev_states=False):
        """
        get the device name by AIX conn info.

        :param conn_info: a list with aix conn strings.
        """
        dev_info = {}
        disk = []
        outputs = []
        if conn_info:
            for conn in conn_info:
                cmd = "ioscli lspath -conn %s -fmt :" % conn
                output = self.run_command(cmd)
                outputs.append(output)
                if len(output) == 0:
                    continue
                # output looks like this:
                # Enabled:hdisk2:fscsi0:50050768011052ec,1000000000000
                if(all_dev_states or
                   output[0].split(':')[0].lower() == 'enabled'):
                    devname = output[0].split(':')[1]
                    if disk.count(devname) == 0:
                        disk.append(devname)

        # the disk list should have only one device name for
        # MPIO disk since the conn_info is the paths for one LUN.

        if len(disk) == 1:
            dev_info = {'device_name': disk[0]}
        elif len(disk) > 1:
            raise exception.\
                IBMPowerVMInvalidLUNPathInfoMultiple(conn_info, outputs)
        else:
            raise exception.\
                IBMPowerVMInvalidLUNPathInfoNone(conn_info, outputs)

        return dev_info

    @logcall
    def get_fcs_parent_devices(self):
        """
        Get the FC device names dict with PHB as the keyword
        """
        fcs = {}
        command = \
            'ioscli lsdev -type adapter -field parent name -fmt :|grep fcs'
        output = self.run_command(command)

        if output and len(output) > 0:
            for dev in output:
                phb = dev.split(':')[0]
                fc = dev.split(':')[1]
                if not fcs.get(phb):
                    fcs[phb] = []
                fcs[phb].append(fc)
        return fcs

    @logcall
    def get_wwpns(self):
        """
        Get wwpns

        returns: World wide port names
        """
        commands = ['oem_setup_env',
                    'lscfg -vp -l fcs* | grep "Network Address"',
                    'exit']
        wwpns = []
        output = self.run_interactive(commands)
        for out in output:
            match = re.search(r'^[^\.]+[\.]+([0-9A-F]{16})$', out)
            if not match:
                continue
            wwpns.append(match.group(1))
        return wwpns

    def get_device_name_by_wwpn(self, wwpn):
        """
        Return the name of the device by its wwpn.

        :param wwpn: Network address for the device
        """
        commands = ['oem_setup_env',
                    'lscfg -v -l fcs*',
                    'exit']
        output = self.run_interactive(commands)

        wwpn_to_name = {}
        for out in output:
            if out.strip().startswith('fcs'):
                dev_name = out.strip().split(' ')[0]
            elif out.strip().startswith('Network Address'):
                dev_wwpn = out.strip().split('.')[-1]
                wwpn_to_name[dev_wwpn] = dev_name

        device_name = wwpn_to_name[wwpn]

        # make sure device_name is not empty or None
        assert device_name
        # and return it
        return device_name

    def get_fcs_device_names(self, wwpns):
        """
        Return the names of all fcs devices

        :param wwpn: Network address for the device
        """
        commands = ['oem_setup_env',
                    'lscfg -v -l fcs*',
                    'exit']
        output = self.run_interactive(commands)

        dev_names = []
        for out in output:
            if out.strip().startswith('fcs'):
                dev_name = out.strip().split(' ')[0]
                dev_names.append(dev_name)

        return dev_names

    @synchronized('cfgdev', 'pvm-')
    def run_cfg_dev(self, device_name=None):
        """
        Run cfgdev command. Optionally, for a specific device

        :param device_name: device name passed to cfgdev -dev command
                            if device_name evaluates to False, cfgdev
                            will be run with no -dev argument
        """
        if device_name:
            command = self.command.cfgdev('-dev %s' % device_name)
        else:
            command = self.command.cfgdev('')

        self.run_command(command)

    def update_fibre_channel_devices(self, fc_device_name):
        self.run_cfg_dev(fc_device_name)

    def get_disk_name_by_volume_uid(self, uid):
        """
        Returns the disk name by its volume uid

        :param uid: volume uid
        """
        command = \
            'for dev in $(ioscli lsdev -type disk -field name -fmt :);' + \
            ' do echo "$dev:`ioscli lsdev -dev $dev -attr unique_id | ' + \
            'awk \'{if (NR==3) print $0}\'`"; done | grep %s' % uid
        try:
            output = self.run_command(command)
        except:
            ex_args = {'volume_uid': uid}
            raise exception.IBMPowerVMHDiskLookupFailed(**ex_args)

        disk_name = None
        for out in output:
            match = re.search(r'(hdisk\d+)', out)
            if match:
                disk_name = match.group(1)
                break
        return disk_name

    def remove_disk(self, disk_name):
        """
        Remove a disk

        :param disk: the disk name
        """
        cmd = self.command.rmdev('-dev %s' % disk_name)
        try:
            self.run_command(cmd)
        except exception.IBMPowerVMCommandFailed as cmdex:
            exit_code = 0
            if cmdex.kwargs is not None:
                if 'exit_code' in cmdex.kwargs:
                    exit_code = cmdex.kwargs['exit_code']
            if int(exit_code) == 1:
                time.sleep(10)
                self.run_command(cmd)
            else:
                raise cmdex

    @logcall
    def attach_disk_to_vhost(self, disk_info, vhost):
        """Attach disk name to a specific vhost.

        :param disk_info: a dictionary containing
            disk information the disk name
        :param vhost: the vhost name
        """
        super(IVMOperator, self).attach_disk_to_vhost(disk_info, vhost)

        if 'vopt' in disk_info:
            vopt_img_name = disk_info['vopt']

            LOG.info(_("Attaching iso %s ") % vopt_img_name)

            # create FBO then attach ISO image
            vdev_fbo_name = self.create_fbo_device(vhost)

            if not vdev_fbo_name:
                raise exception.IBMPowerVMISOAttachFailed()

            LOG.info(_("Loading vopt %(vopt_img_name)s"
                       " to FBO %(vdev_fbo_name)s") % locals())

            self.load_vopt_device(vopt_img_name,
                                  vdev_fbo_name)

    def detach_disk_from_vhost(self, vdev):
        """
        Detach disk from a specific virtual device

        :param vdev: the backing device
        """
        # GL: For multiple vhosts attachments, use -vtd instead of -vdev
        command = self.command.rmvdev('-vdev %s') % vdev
        try:
            self.run_command(command)
        except exception.IBMPowerVMCommandFailed as cmdex:
            exit_code = 0
            if cmdex.kwargs is not None:
                if 'exit_code' in cmdex.kwargs:
                    exit_code = cmdex.kwargs['exit_code']
            if int(exit_code) == 1:
                time.sleep(10)
                self.run_command(command)
            else:
                raise cmdex

    def get_lpar_max_virtual_slots(self, lpar_id):
        """
        Get the pend_max_virtual_slots for a given lpar_id.
        :param lpar_id: LPAR ID
        :returns: number of maximum virtual slots allowed
        """
        filter_str = '--filter "lpar_ids=%s"' % lpar_id
        command = self.command.lshwres('-r virtualio --rsubtype slot '
                                       '--level lpar '
                                       '-F pend_max_virtual_slots %s' %
                                       filter_str)
        output = self.run_command(command)
        if output and output[0]:
            return int(output[0])
        else:
            return 0

    def get_vios_max_virt_slots(self):
        return self.get_lpar_max_virtual_slots(self.vios_lpar_id)

    def get_hyp_capability(self):
        """
        Extract the hypervisor capabilities.
        Construct dict based on capabilities lssyscfg -r sys -F capabilities
        It current returns the following capabilities
        active_lpar_mobility_capable
        inactive_lpar_mobility_capable
        """
        # This list can be updated with the keys we're interested in.
        cap_keys = [pvc_const.ACTIVE_LPAR_MOBILITY_CAPABLE,
                    pvc_const.INACTIVE_LPAR_MOBILITY_CAPABLE]
        caps_dict = dict.fromkeys(cap_keys, False)
        cmdstr = '-r sys -F capabilities'
        cmd = self.command.lssyscfg(cmdstr)
        output = self.run_command(cmd)
        if output:
            ret_str = output[0]
            if ret_str.startswith('"') and ret_str.endswith('"'):
                ret_str = ret_str[1:-1]
            sys_caps = ret_str.split(',')
            for cap in sys_caps:
                if cap in cap_keys:
                    caps_dict[cap] = True
        return caps_dict

    def get_migration_stats(self):
        """
        Gets the migration stats from the VIOS using the lslparmigr command
        returns dict of inactv_migr_sup,actv_migr_supp,
        inactv_migr_prg,actv_migr_prg
        """
        cmdstr = "-r sys -F num_inactive_migrations_supported,"\
            "num_active_migrations_supported,"\
            "num_inactive_migrations_in_progress,"\
            "num_active_migrations_in_progress"
        cmd = self.command.lslparmigr(cmdstr)
        output = self.run_command(cmd)
        inactv_migr_sup, actv_migr_supp, inactv_migr_prg, actv_migr_prg = \
            output[0].split(',')
        return {'inactv_migr_sup': int(inactv_migr_sup),
                'actv_migr_supp': int(actv_migr_supp),
                'inactv_migr_prg': int(inactv_migr_prg),
                'actv_migr_prg': int(actv_migr_prg)}

    def get_inactv_migration_stats(self):
        """
        Gets the migration stats from the VIOS using the lslparmigr command
        returns dict of inactv_migr_sup,inactv_migr_prg
        """
        cmdstr = "-r sys -F num_inactive_migrations_supported,"\
            "num_inactive_migrations_in_progress"
        cmd = self.command.lslparmigr(cmdstr)
        output = self.run_command(cmd)
        inactv_migr_sup, inactv_migr_prg = \
            output[0].split(',')
        return {'inactv_migr_sup': int(inactv_migr_sup),
                'inactv_migr_prg': int(inactv_migr_prg)}

    def get_actv_migration_stats(self):
        """
        Gets the migration stats from the VIOS using the lslparmigr command
        returns dict of actv_migr_supp,actv_migr_prg
        """
        cmdstr = "-r sys -F num_active_migrations_supported,"\
            "num_active_migrations_in_progress"
        cmd = self.command.lslparmigr(cmdstr)
        output = self.run_command(cmd)
        actv_migr_supp, actv_migr_prg = \
            output[0].split(',')
        return {'actv_migr_supp': int(actv_migr_supp),
                'actv_migr_prg': int(actv_migr_prg)}

    @logcall
    def check_vopt_exists(self, name):
        """
        Check if a virtual optical backing device file
        with the given name exists in the media library
        :param name: The name of the virtual optical backing device

        Example usage and output of command
        Example usage:

        Usage: lsrep [-field FieldName] [-fmt Delimiter]

        $ lsrep -field name -fmt ,

        b3a70490-93d5-42f2-8ef6-cd3c84d5dbf5

        :returns: boolean - if the a vopt with the name exists
        """

        command = self.command.lsrep("-field name -fmt ,")
        output = self.run_command(command)

        if name in output:
            return True
        else:
            return False

    @logcall
    def get_vopt_size(self):
        """
        Get the free space available in the VIOS media repository.

        :returns: free space in media repository in MB
        """

        command = self.command.lsrep("-field free -fmt ,")
        output = self.run_command(command)

        return output

    @logcall
    def get_staging_size(self, host_dir):
        """
        Get free space in the staging area, the filesystem
        that host_dir is on.
        :param host_dir: The directory being used as the IVM staging area.

        :returns: free space in file system of host_dir in KB
        """

        command = "df -k %s | awk '{print $3}' | grep -v Free" % host_dir
        output = self.run_command(command)

        return output

    def _calc_cpu_utilization(self, output, startline=None, endline=None):
        """ Calculate cpu utilization from output of lslparutil cmd
        :param output: output of lslparutil cmd
        :param startline: index to start on
        :param endline: index to stop on
        :returns: decimal of utilization
        """
        if not startline:
            startline = 0
        if not endline:
            endline = len(output) - 1

        ent_cs_tn, cap_cs_tn, uncap_cs_tn = output[startline].split(',')
        ent_cs_t0, cap_cs_t0, uncap_cs_t0 = output[endline].split(',')
        entitled = float(ent_cs_tn) - float(ent_cs_t0)
        # Make sure we're not dividing by zero
        if entitled == 0:
            return 0

        cpu_util = (((float(cap_cs_tn) - float(cap_cs_t0)) +
                     (float(uncap_cs_tn) - float(uncap_cs_t0))) / entitled)
        return cpu_util

    def get_lpar_mem(self, instance_name):
        """Return a dictionary of an LPAR's memory resources.

        :param instance_name: LPAR instance name
        :returns: memory resource dictionary
        """
        # Get the LPAR Name if the OpenStack Name is different than it
        lpar_name = self.get_actual_lpar_name(instance_name)
        cmd = self.command.lshwres(
            '-r mem --level lpar --filter "lpar_names=%s"'
            % (lpar_name))
        output = self.run_vios_command(cmd)
        if not output:
            return None
        return self._load_cli_output(output[0])

    def get_lpar_proc(self, instance_name):
        """Return a dictionary of an LPAR's processor resources.

        :param instance_name: LPAR instance name
        :returns: memory resource dictionary
        """
        # Get the LPAR Name if the OpenStack Name is different than it
        lpar_name = self.get_actual_lpar_name(instance_name)
        cmd = self.command.lshwres(
            '-r proc --level lpar --filter "lpar_names=%s"'
            % (lpar_name))
        output = self.run_vios_command(cmd)
        if not output:
            return None
        return self._load_cli_output(output[0])

    def get_lpar_cpu_util(self, lpar_id):
        """Get the CPU utilization for an lpar
        :param lpar_id: the lpar id
        :returns: decimal
        """
        sample_size = 3
        command = ('lslparutil --filter lpar_ids=%s -n %s -r lpar -F '
                   'entitled_cycles,capped_cycles,uncapped_cycles'
                   % (lpar_id, sample_size))
        output = self.run_command(command)

        if len(output) < sample_size:
            ex_args = {'command': command,
                       'error': output}
            raise exception.IBMPowerVMCommandFailed(**ex_args)
        return self._calc_cpu_utilization(output)

    def get_lpar_info(self, lpar_id):
        """Get the info for an lpar
        :param lpar_id: the lpar id
        :returns: dict of the current stats
        """
        def _parse_cpu_stats(line):
            stats = self._load_cli_output(line)
            return "%s,%s,%s" % (stats['entitled_cycles'],
                                 stats['capped_cycles'],
                                 stats['uncapped_cycles'])

        sample_size = 3
        command = ('lslparutil --filter lpar_ids=%s  -n %s -r lpar ' %
                   (lpar_id, sample_size))
        output = self.run_command(command)
        if len(output) < sample_size:
            ex_args = {'command': command,
                       'error': output}
            raise exception.IBMPowerVMCommandFailed(**ex_args)

        # Parse the attributes of the first row for the bulk of the settings.
        # First row gives the latest values.
        lpar_attrs = self._load_cli_output(output[0])

        # Now calculate the cpu utilization based on the first and last rows
        cpu_info = []
        cpu_info.append(_parse_cpu_stats(output[0]))
        cpu_info.append(_parse_cpu_stats(output[len(output) - 1]))

        cpu_utl = self._calc_cpu_utilization(cpu_info)
        lpar_attrs['curr_cpu_util'] = str(round(cpu_utl, 2))

        return lpar_attrs

    def get_cpu_info_for_lpars(self):
        """Get CPU info for every lpar.

        :returns: dictionary of proc units assigned to each lpar
            {lpar_name: proc_units_reserved}
        """
        cmd = self.command.lshwres(
            '-r proc --level lpar -F lpar_name,curr_proc_mode,'
            'curr_proc_units,curr_procs')
        output = self.run_command(cmd)
        lpar_proc_info = {}
        for lpar in output:
            (lpar_name, proc_mode, proc_units_reserved,
             procs_reserved) = lpar.split(',')
            if proc_mode == 'ded':
                proc_units_reserved = procs_reserved
            lpar_proc_info[lpar_name] = decimal.Decimal(proc_units_reserved)
        return lpar_proc_info

    def get_disk_names_for_vhost_frm_dict(self, vhost_id, disk_dict,
                                          local=False):
        """Return the disk names attached to a specific vhost

        :param vhost: vhost name
        :param local: returns disk names for local disk also
        :returns: list of disk names attached to the vhost,
                  ordered by time of attachment
                  (first disk should be boot disk)
        """
        # assemble dictionary of [ lun => diskname ]
        lun_to_disk = {}
        values = disk_dict[vhost_id]
        i = 1
        while len(values) > i:
            lun_to_disk[values[i - 1]] = values[i]
            i = i + 2

        lun_numbers = lun_to_disk.keys()
        lun_numbers.sort()

        # assemble list of disknames ordered by lun number
        disk_names = []
        for lun_num in lun_numbers:

            # There may be virtual opticals in the list.  We only want hdisks
            disk_name = lun_to_disk[lun_num]
            if not local and (re.search(r'(hdisk\d+)', disk_name)):
                disk_names.append(disk_name)

            if local and ((re.search(r'(lv\d+)', disk_name)) or self.is_device_logical_volume(disk_name)):
                disk_names.append(disk_name)

        LOG.debug("Exiting (disk_names = %s)" % disk_names)
        return disk_names

    def get_disk_names_for_vhost(self, vhost, local=False):
        """Return the disk names attached to a specific vhost

        :param vhost: vhost name
        :param local: returns disk names for local disk also
        :returns: list of disk names attached to the vhost,
                  ordered by time of attachment
                  (first disk should be boot disk)
        """
        LOG.debug("Entering")
        command = self.command.lsmap(
            '-vadapter %s -field LUN backing -fmt :' % vhost)
        output = self.run_command(command)[0].strip()

        # assemble dictionary of [ lun => diskname ]
        lun_to_disk = {}
        remaining_str = output
        while remaining_str:
            values = remaining_str.split(':', 2)
            lun_to_disk[values[0]] = values[1]
            if len(values) > 2:
                remaining_str = values[2]
            else:
                remaining_str = ''

        lun_numbers = lun_to_disk.keys()
        lun_numbers.sort()

        # assemble list of disknames ordered by lun number
        disk_names = []
        for lun_num in lun_numbers:

            # There may be virtual opticals in the list.  We only want hdisks
            disk_name = lun_to_disk[lun_num]
            if not local and (re.search(r'(hdisk\d+)', disk_name)):
                disk_names.append(disk_name)

            if local and (re.search(r'(lv\d+)', disk_name)):
                disk_names.append(disk_name)

        LOG.debug("Exiting (disk_names = %s)" % disk_names)
        return disk_names

    def is_device_logical_volume(self, diskname):
        """
        Check if a disk in /dev/ is a logical volume or not

        :param diskname: name of device in /dev/
        :returns: True if device is a logical volume, False otherwise
        """
        LOG.debug("Entering (diskname = %s)" % diskname)
        if diskname is None or not diskname.strip():
            return False

        try:
            devDesc = self.run_command(
                'ioscli lsdev -dev %s -field description' %
                diskname)
        except exception.IBMPowerVMCommandFailed as e:
            LOG.debug("Command failed while trying to lsdev %s" % diskname)
            return False

        try:
            uniqueId = self.run_command(
                'ioscli lsdev -dev %s -attr unique_id' %
                diskname)
        except exception.IBMPowerVMCommandFailed as e:
            LOG.debug("Command failed while trying to find disk unique_id")
            uniqueId = None

        # if description indicates its an LV and we don't have a uniqueId
        # then its an LV
        result = False
        if ('Logical volume' in devDesc) and (not uniqueId):
            LOG.debug("Disk %(diskname)s detected as Logical Volume: "
                      "[ %(devDesc)s, %(uniqueId)s ]" % locals())
            result = True

        LOG.debug("Exiting (result = %s)" % result)
        return result

    def is_device_SVC_FC_volume(self, diskname):
        """
        Check if a disk in /dev/ is a logical volume or not

        :param diskname: name of device in /dev/
        :returns: True if device is an SVC FC volume, False otherwise
        """
        LOG.debug("Entering (diskname = %s)" % diskname)

        devDesc = self.run_command('ioscli lsdev -dev %s -field description' %
                                   diskname)
        uniqueId = self.run_command('ioscli lsdev -dev %s -attr unique_id' %
                                    diskname)
        parentDev = self.run_command('ioscli lsdev -dev %s -parent' %
                                     diskname)

        # if description indicates it a fibre channel disk
        # and if it has a unique_id (volume id)
        # and if it has a parent that is a fibre channel scsi device
        # then we assume it is an SVC FC disk

        descFlag = False
        for entry in devDesc:
            if 'FC Disk' in entry:
                descFlag = True
                break

        parentDevFlag = False
        for entry in parentDev:
            if 'fscsi' in entry:
                parentDevFlag = True
                break

        result = False
        if descFlag and (uniqueId) and parentDevFlag:
            LOG.debug("Disk %(diskname)s detected as SVC FC volume: "
                      "[ %(devDesc)s, %(uniqueId)s ]" % locals())
            result = True

        LOG.debug("Exiting (result = %s)" % result)
        return result

    def _load_cli_output(self, csv_output):
        """
        This method will load the csv output from HMC/IVM cli to dict
        :parm csv_output: csv output from HMC/IVM cli
        :returns dictionary of {'attribute':'value'}
        """
        cf_splitter = shlex.shlex(csv_output, posix=True)
        cf_splitter.whitespace = ','
        cf_splitter.whitespace_split = True
        return dict(item.split("=") for item in list(cf_splitter))

    def _get_num_virt_slots_in_use(self, remote_lpar_names, rsubtype):
        """
        Return number of virtual adapter with rsubtype where remote_lpar_name
        attribute is set of one of the ids in remote_lpar_names
        :parm remote_lpar_names: list of lpar names
        :parm rsubtype: scsi, serial, or fc
        """
        num_slots_in_use = 0
        filter_str = '--filter "lpar_ids=%s"' % self.vios_lpar_id
        command = self.command.lshwres('-r virtualio --rsubtype '
                                       + rsubtype +
                                       ' --level lpar ' + filter_str)
        slots = self.run_command(command)
        for entry in slots:
            slot_data = self._load_cli_output(entry)
            if(slot_data['remote_lpar_id'] != 'any' and
               slot_data['remote_lpar_name'] in remote_lpar_names):
                num_slots_in_use += 1
        return num_slots_in_use

    def _get_all_virt_slots_in_use(self, remote_lpar_names):
        """
        Return number of virtual adapter where remote_lpar_names
        attribute is set of one of the ids in remote_lpar_names
        :parm remote_lpar_names: list of lpar names
        """
        num_slots = 0
        for rsubtype in ['scsi', 'serial', 'fc']:
            num_slots += (self._get_num_virt_slots_in_use(
                remote_lpar_names, rsubtype))
        return num_slots

    def get_num_reserved_in_use_vios_slots(self, managed_lpar_names):
        """
        This method will go through all the slots currently allocated to VIOS
        and figure out what's not being used by instances managed by OpenStack.
        The values return will be what's currently allocated but not for
        OpenStack managed lpars and number of adapters currently
        allocated to client with lpar_names matching to what's being managed.
        :parm managed_lpar_ids: list of lpar names of managed instances
        :returns (num_reserved, num_in_use)
        """
        filter_str = '--filter "lpar_ids=%s"' % self.vios_lpar_id
        command = self.command.lshwres('-r virtualio --rsubtype slot '
                                       '--level slot ' + filter_str +
                                       ' -F slot_num,config')
        output = self.run_command(command)
        if output:

            # count number of entries returned
            num_virt_slots = len(output)
            num_slot_in_use = \
                self._get_all_virt_slots_in_use(managed_lpar_names)
            return (num_virt_slots - num_slot_in_use, num_slot_in_use)
        return (0, 0)

    def prepare_vhost_dev_lun_dict(self):
        """
        Prepares the dicts for all the vms under the host
        Return the disk_dict vhost dict .
        :returns: disk_dict, vhost_dict
        """
        LOG.debug("Entering")

        # instance_hex_id = '%#010x' % int(instance_id)
        # lsmap -all -field clientid SVSA -fmt :
        cmd = self.command.lsmap('-all -field LUN backing SVSA clientid'
                                 ' -fmt :')
        output = self.run_vios_command(cmd, False)
        disk_dict = {}
        vhost_dict = {}
        for outstr in output:
            lst = outstr.split(':')
            vhost_id = lst.pop(0)
            inst_hex = lst.pop(0)
            vhost_dict[inst_hex] = vhost_id
            disk_dict[vhost_id] = lst

        LOG.debug("Exiting (vhost = %s)" % disk_dict)
        return disk_dict, vhost_dict

    def get_vhost_by_instance_id(self, instance_id):
        """Return the vhost name by the instance id.

        :param instance_id: LPAR instance id
        :returns: string -- vhost name or None in case none is found
        """
        LOG.debug("Entering")

        instance_hex_id = '%#010x' % int(instance_id)
        cmd = self.command.lsmap(args='-all | grep \'%s\' | awk \'{print $1}\''
                                 % instance_hex_id)
        output = self.run_vios_command(cmd, False)
        vhost = None
        if output:
            vhost = output[0]

        LOG.debug("Exiting (vhost = %s)" % vhost)
        return vhost

    def set_no_reserve_on_hdisk(self, diskname):
        """Run chdev and set attributes no_reserve

        :param diskname: name of the disk
        """
        cmd = self.command.chdev('-dev %s -attr reserve_policy=no_reserve' %
                                 diskname)
        try:
            self.run_command(cmd)
        except exception.IBMPowerVMCommandFailed as cmdex:
            exit_code = 0
            if cmdex.kwargs is not None:
                if 'exit_code' in cmdex.kwargs:
                    exit_code = cmdex.kwargs['exit_code']
            if int(exit_code) == 1:
                time.sleep(10)
                self.run_command(cmd)
            else:
                raise cmdex

    @logcall
    def get_hdisk_reserve_policy(self, diskname):
        """Get the reserver policy attribute of the hdisk

        returns: string containing the reserve_policy
        """
        cmd = self.command.lsdev('-dev %s -attr reserve_policy' %
                                 diskname)
        output = self.run_command(cmd)
        return output[2]

    @logcall
    def get_management_sys_name(self):
        """Get the management system name

        :returns: name of management system
        """
        cmd = 'lssyscfg -r sys -F name'
        output = self.run_command(cmd)

        if output:
            man_sys_name = output[0]

        return man_sys_name

    @logcall
    def check_dlpar_connectivity(self, instance_name, resource_type='lpar'):
        """Check the partition for DLPAR capability and rmc state

        :param instance_name: name of the LPAR to query
        :param resource_type: the type of resources to list (lpar or prof)
        :returns: Returns true or false if DLPAR capable
        :returns: Returns RMC state as string
        """
        # Get the LPAR Name if the OpenStack Name is different than it
        lpar_name = self.get_actual_lpar_name(instance_name)
        cmd = ('-r %s --filter "lpar_names=%s" '
               '-F dlpar_mem_capable,dlpar_proc_capable,rmc_state' %
               (resource_type, lpar_name))
        command = self.command.lssyscfg(cmd)

        output = self.run_command(command)
        dlpar = False
        state = None
        # need to make sure there is output to parse
        # in the case of lpar no longer exists on IVM,
        # VIOS would return exit code of 0 but
        # with error output of [VIOSI01040010-0003] No results were found.
        if output:

            # The values returned for the token capable attributes
            # will either be 1 or 0 (true or false) in the form [1,1]
            token = output[0].split(',')
            if (token[0] and token[1]):
                dlpar = True

            state = token[2]
        return dlpar, state

    @logcall
    def get_refcode(self, instance_name):
        """Returns the refcode of the partition

        :param instance_name: name of the LPAR to query
        :returns: the refcode of the lpar, or None
        if there's no output from the endpoint
        """
        # Get the LPAR Name if the OpenStack Name is different than it
        lpar_name = self.get_actual_lpar_name(instance_name)
        cmd = ('-r lpar --filter "lpar_names=%s" -F refcode,time_stamp' %
               lpar_name)
        command = self.command.lsrefcode(cmd)

        output = self.run_command(command)
        if output is not None:
            refcode = output[0].split(',')[0]
            return refcode

        return None

    @logcall
    def live_migrate_validate(self, man_sys_name, inst_name, dest, user):
        """Run migrate validation on from source host

        :param man_sys_name: name of management system on dest
        :param inst_name: name of instance name to be migrated
        :param dest: destination ip or DNS
        :param user: name of IVM user on destination
        :returns: execution code of command
        """
        # Get the LPAR Name if the OpenStack Name is different than it
        lpar_name = self.get_actual_lpar_name(inst_name)
        cmd = ''.join(['-o v -t %s ' % man_sys_name,
                       '-p %s ' % lpar_name,
                       '--ip %s ' % dest,
                       '-u %s' % user])
        try:
            self.run_command(self.command.migrlpar(cmd))

        except exception.IBMPowerVMCommandFailed as e:
            ex_args = {'instance_name': inst_name,
                       'error': e}
            LOG.error(_("Live migration validation failed for " +
                        "instance %(instance_name)s. %(error)s") % ex_args)
            raise exception.IBMPowerVMMigrateValidateFailed(**ex_args)

    def check_if_migr_reached_max(self, inst_name):

        # Verify the source host can support another migration
        # Ensures the stat verification even after the service restarted
        migr_stat = self.get_migration_stats()
        if (migr_stat and
                (migr_stat['actv_migr_prg'] >= migr_stat['actv_migr_supp'])):
            error = (_("Cannot migrate %(inst)s because host %(host)s "
                       "only allows %(allowed)s concurrent migrations, and "
                       "%(running)s migrations are currently running.")
                     % {'inst': inst_name, 'host': CONF.host_display_name,
                        'allowed': migr_stat['actv_migr_supp'],
                        'running': migr_stat['actv_migr_prg']})
            LOG.exception(error)
            raise common_ex.IBMPowerVMMigrationFailed(error)

    @logcall
    def live_migrate_lpar(self, man_sys_name, inst_name, dest, user):
        """Run migrlpar and carry out live migration to dest

        :param man_sys_name: name of management system on dest
        :param inst_name: name of instance name to be migrated
        :param dest: destination ip or DNS
        :param user: name of IVM user on destination
        """
        self.check_if_migr_reached_max(inst_name)

        # Get the LPAR Name if the OpenStack Name is different than it
        lpar_name = self.get_actual_lpar_name(inst_name)
        cmd = ''.join(['-o m --async -t %s ' % man_sys_name,
                       '-p %s ' % lpar_name,
                       '--ip %s ' % dest,
                       '-u %s' % user])
        try:
            self.run_command(self.command.migrlpar(cmd))

        except exception.IBMPowerVMCommandFailed as e:
            ex_args = {'instance_name': inst_name,
                       'error': e}
            LOG.error(_("Live migration failed for instance " +
                        "%(instance_name)s. %(error)s") % ex_args)
            raise exception.IBMPowerVMMigrateFailed(**ex_args)

    @logcall
    def get_live_migration_state(self, inst_name, resource_type='lpar'):
        """Check for state of live mirgration for given LPAR

        :param inst_name: name of LPAR being migrated
        :param resource_type: defaults to lpar
        :returns: The state of live migration. If there is no
                  output "None" is returned.
        """

        # Get the LPAR Name if the OpenStack Name is different than it
        lpar_name = self.get_actual_lpar_name(inst_name)
        cmd = ''.join(['-r %s ' % resource_type,
                       '--filter "lpar_names=%s" ' % lpar_name,
                       '-F migration_state'])
        output = self.run_command(self.command.lslparmigr(cmd))
        if output is not None:
            migration_state = output[0]
            return migration_state

        return None

    @logcall
    def stop_live_migration(self, man_sys_name, inst_name):
        """Stop a live migration

        :param man_sys_name: name of source management system
        :param inst_name: name of partition to be stopped
        """
        # Get the LPAR Name if the OpenStack Name is different than it
        lpar_name = self.get_actual_lpar_name(inst_name)
        cmd = ''.join(['-o s -m %s ' % man_sys_name,
                       '-p %s ' % lpar_name])
        self.run_command(self.command.migrlpar(cmd))

    @logcall
    def recover_live_migration(self, man_sys_name, inst_name):
        """Recover a failed migration

        :param man_sys_name: name of source management system
        :param inst_name: name of partition to be recovered
        """
        # Get the LPAR Name if the OpenStack Name is different than it
        lpar_name = self.get_actual_lpar_name(inst_name)
        cmd = ''.join(['-o r -m %s ' % man_sys_name,
                       '-p %s ' % lpar_name])
        self.run_command(self.command.migrlpar(cmd))

    @logcall
    def check_lpar_exists(self, instance_name):
        """
        Internal method to check whether a LPAR instance exists.

        :param instance_name: the LPAR instance name
        """
        lpar = self.get_lpar(instance_name)
        if lpar is None:
            raise exception.IBMPowerVMLPARInstanceDoesNotExist(
                instance_name=instance_name)

    @logcall
    def stop_lpar(self, instance_name, force_immediate=True,
                  timeout=CONF.ibmpowervm_power_off_timeout, is_reboot=False):
        """Stop a running LPAR.  RMC state must be active
        before we can do an osshutdown.  If not, an immediate
        shutdown will be done.  If the osshutdown times out
        an immediate shutdown will be done.

        :param instance_name: LPAR instance name
        :param timeout: value in seconds for specifying
                        how long to wait for the LPAR to stop
        :param is_reboot: To differentiate this stop before restart
        """
        if force_immediate:
            LOG.debug("Forcing an immediate shutdown for %s. " %
                      instance_name)
            super(IVMOperator, self).stop_lpar(instance_name,
                                               timeout)
            return

        # Get the LPAR Name if the OpenStack Name is different than it
        lpar_name = self.get_actual_lpar_name(instance_name)
        rmc_state = self.check_dlpar_connectivity(instance_name)[1]
        if rmc_state != 'active':
            LOG.debug("RMC state is not active for %s. "
                      "Performing an immediate shutdown." %
                      instance_name)
            super(IVMOperator, self).stop_lpar(instance_name,
                                               timeout)
        else:
            cmd = self.command.chsysstate('-r lpar -o osshutdown -n %s' %
                                          lpar_name)
            greenthread.spawn(self.run_vios_command, cmd)

            # poll instance until stopped or raise exception
            lpar_obj = self.get_lpar(instance_name)
            wait_inc = 1  # seconds to wait between status polling
            start_time = time.time()
            while lpar_obj['state'] != 'Not Activated':
                curr_time = time.time()
                # wait up to (timeout) seconds for shutdown
                if (curr_time - start_time) > timeout:
                    if is_reboot:
                        raise exception.IBMPowerVMLPAROperationTimeout(
                            operation='stop_lpar',
                            instance_name=instance_name)
                    else:
                        LOG.debug("Operating system shutdown has timed out "
                                  "for %s. Performing an immediate shutdown." %
                                  instance_name)
                        super(IVMOperator, self).stop_lpar(instance_name, 30)
                    return

                time.sleep(wait_inc)
                lpar_obj = self.get_lpar(instance_name)

    @logcall
    def check_state_after_resize(self, instance):
        """Check the power state of the LPAR. If state is 'Not Activated',
        then state is transitioned to STOPPED. This done because an LPAR
        that was in the STOPPED state during a resize, gets set to ACTIVE
        during confirm_resize Compute API, which doesn't reflect the true
        power state of the LPAR.
        :param instance: instance information
        """
        try:
            lpar_obj = self.get_lpar(instance['name'])
            if lpar_obj['state'] == 'Not Activated':
                ctxt = context.get_admin_context()
                vm = conductor.API().instance_get(ctxt, instance['id'])
                if vm['vm_state'] == vm_states.ACTIVE:
                    conductor.API().instance_update(
                        ctxt, instance['uuid'], vm_state=vm_states.STOPPED)
        except Exception as e:
            LOG.exception(
                _("Unable to verify state during resize confirm: %s") % e)

    def get_lpar_proc_compat_modes(self):
        """
        Task 2758
        Get processor compatible mode of lpar.
        :returns: list of compatible proc modes for the ivm
        """
        cmd = self.command.lssyscfg(
            '-r sys -F lpar_proc_compat_modes')
        output = self.run_command(cmd)
        if output:
            ret_str = output[0]
            if ret_str.startswith('"') and ret_str.endswith('"'):
                ret_str = ret_str[1:-1]
            compat_modes = ret_str.split(',')
        else:
            LOG.debug("Failed to get compatibility modes")
            compat_modes = []
        return compat_modes

    def get_curr_and_desired_proc_compat_modes(self, instance_name):
        """
        Task 2759
        Return a dictionary of an LPAR's processor resources.
        :param instance_name: LPAR instance name
        :returns: Current compatible proc mode, Desired compatible proc mode
        """
        # Get the LPAR Name if the OpenStack Name is different than it
        lpar_name = self.get_actual_lpar_name(instance_name)
        cmd = self.command.lssyscfg("".join(['-r lpar --filter',
                                             ' "lpar_names=%s"'
                                             % (lpar_name),
                                            ' -F curr_lpar_proc_compat_mode,',
                                             'desired_lpar_proc_compat_mode']))
        output = self.run_vios_command(cmd)
        if output:
            ret_str = output[0]
            compat_modes = ret_str.split(',')
        else:
            LOG.debug("Failed to get current and desired compatibility modes")
            return None
        return compat_modes

    def get_lpar_operating_system(self, instance_name):
        """
        Task 10866
        Return a string.
        :param instance_name: LPAR instance name
        :returns: Operating system version of VM.
        """
        # Get the LPAR Name if the OpenStack Name is different than it
        lpar_name = self.get_actual_lpar_name(instance_name)
        if lpar_name is None or lpar_name == '':
            return None
        cmd = self.command.lssyscfg("".join(['-r lpar --filter',
                                             ' "lpar_names=%s"'
                                             % (lpar_name),
                                            ' -F os_version']))
        output = self.run_vios_command(cmd)
        if output:
            os_version = output[0]
        else:
            LOG.debug("Failed to get operating system version")
            return None
        return os_version

    def macs_for_instance(self, instance):
        """Generates set of valid MAC addresses for an IVM instance."""
        # NOTE(vish): We would prefer to use 0xfe here to ensure that linux
        #             bridge mac addresses don't change, but it appears to
        #             conflict with libvirt, so we use the next highest octet
        #             that has the unicast and locally administered bits set
        #             properly: 0xfa.
        #             Discussion: https://bugs.launchpad.net/nova/+bug/921838
        # NOTE(mjfork): For IVM-based PowerVM, we cannot directly set a MAC
        #               address on an LPAR, but rather need to construct one
        #               that can be used.  Retain the 0xfa as noted above,
        #               but ensure the final 2 hex values represent a value
        #               between 32 and 64 so we can assign as the slot id on
        #               the system. For future reference, the last octect
        #               should not exceed FF (255) since it would spill over
        #               into the higher-order octect.
        #
        #               FA:xx:xx:xx:xx:[32-64]

        macs = set()
        mac_base = [0xfa,
                    random.randint(0x00, 0xff),
                    random.randint(0x00, 0xff),
                    random.randint(0x00, 0xff),
                    random.randint(0x00, 0xff),
                    random.randint(0x00, 0x00)]
        for n in range(32, 64):
            mac_base[5] = n
            macs.add(':'.join(map(lambda x: "%02x" % x, mac_base)))

        return macs


#
# IBMPowerVMLocalDiskOperator is extending the base
# functinality to give the implementation
# related to the local disk support.
# All the methods related to local disk which are different
# than the base operator should be overriden here
# to give local disk implementation.
#


class PowerVMLocalDiskOperator(PowerVMBaseOperator):

    def __init__(self, virtapi):
        pvm_con = common.Connection(
            CONF.powervm_mgr,
            CONF.powervm_mgr_user,
            CONF.powervm_mgr_passwd)
        self._ld_ivm_operator = LocalDiskIVMOperator(pvm_con, virtapi)
        self._disk_adapter = blockdev.\
            PowerVMLocalVolumeAdapter(pvm_con,
                                      self._ld_ivm_operator)
        super(
            PowerVMLocalDiskOperator,
            self).__init__(virtapi,
                           self._ld_ivm_operator,
                           self._disk_adapter)

    # Overridden spawn method to handle all new error text related defects,
    # added as part of FP2, most of them are tackeled in 4Q already.
    @logcall
    def spawn(self, context, instance, image_meta, injected_files,
              admin_password, network_info, block_device_info,
              validate_volumes, attach_volumes,
              validate_attachment):
        def _attach_image(context, instance, image_id):
            """Spawn method with extended PowerVC boot-from-volume capability"""
            if image_meta['disk_format'] == 'iso':
                LOG.info(_("Disk format is iso. Preparing ISO image on host."))
                vopt_dev_name = self._disk_adapter.\
                    _prepare_iso_image_on_host(context,
                                               instance,
                                               CONF.powervm_img_local_path,
                                               CONF.powervm_img_remote_path,
                                               image_meta)
                image_detail = {'vopt': vopt_dev_name}
                lpar_id = self._operator.get_lpar(instance['name'])['lpar_id']
                vhost = self._operator.get_vhost_by_instance_id(lpar_id)
                self._operator.attach_disk_to_vhost(image_detail, vhost)
            else:
                self._disk_adapter.create_volume_from_image_v2(
                    context, instance, image_id)
            LOG.debug("Validating the volumes...")
            validate_volumes(context, instance['uuid'])
            
            LOG.debug("Attaching volumes...")
            attach_volumes(context, instance)
            # last check before we start instance.
            LOG.debug("Validating attachments...")
            validate_attachment(context, instance['uuid'])

        def _create_image(context, instance, image_id):
            """Fetch image from glance and copy it to the remote system."""
            try:
                root_volume = self._disk_adapter.create_volume_from_image(
                    context, instance, image_id)

                self._disk_adapter.attach_volume_to_host(root_volume)

                lpar_id = self._operator.get_lpar(instance['name'])['lpar_id']
                vhost = self._operator.get_vhost_by_instance_id(lpar_id)
                self._operator.attach_disk_to_vhost(
                    root_volume, vhost)
            except Exception as e:
                LOG.exception(_("PowerVM image creation failed: %s") % e)
                ex_args = {'instance_name': instance['name'],
                           'error_msg': e}
                raise exception.IBMPowerVMStorageAllocFailed(**ex_args)

        spawn_start = time.time()
        image_metadata = None
        try:
            try:
                try:
                    # Getting image meta data
                    image_metadata = self._disk_adapter._get_image_meta(
                        context,
                        instance['image_ref'])
                except Exception as ex:
                    # do nothing
                    LOG.warn(_("unable to get image metadata"))
                LOG.debug("Going to spawn an instance on local storage")
                host_stats = self.get_host_stats(refresh=True)
                lpar_inst = self._create_lpar_instance(
                    instance,
                    network_info, host_stats)
                # TODO(mjfork) capture the error and handle the error when the
                #             MAC prefix already exists on the
                #             system (1 in 2^28)
                LOG.debug("Creating LPAR instance '%s'" % instance['name'])
                self._operator.create_lpar(lpar_inst)
            except Exception as ex:
                LOG.exception(_("LPAR instance '%s' creation failed") %
                              instance['name'])
                # raise a new exception with mksyscfg msg
                ex_args = {'instance_name': instance['name'],
                           'error_msg': ex}
                raise exception.IBMPowerVMLPARCreationFailed(**ex_args)

            if CONF.local_or_cinder:
                _attach_image(context, instance, image_meta['id'])
            else:
                _create_image(context, instance, image_meta['id'])

            iso_remote_path = None
            vopt_img_name = None
            vdev_fbo_name = None
            iso_remote_path = self._transport_config_data(instance,
                                                          injected_files,
                                                          admin_password,
                                                          network_info)
            if iso_remote_path:
                vopt_img_name, vdev_fbo_name = self._attach_iso(
                    iso_remote_path,
                    instance)
            try:
                self.start_lpar_instance(instance, image_metadata)
            except Exception as ex:
                self._vopt_cleanup(vopt_img_name, vdev_fbo_name)
                raise ex

            if vopt_img_name:
                LOG.debug("Unloading the ISO image")
                self._detach_iso(vopt_img_name, vdev_fbo_name, instance)

        except exception.IBMPowerVMStorageAllocFailed:
            with excutils.save_and_reraise_exception():
                # log errors in cleanup
                try:
                    self._cleanup(instance['name'])
                except Exception as ex:
                    LOG.exception(_('Error while attempting to '
                                    'clean up failed instance launch.%s')
                                  % ex)

        spawn_time = time.time() - spawn_start
        LOG.info(_("Instance spawned in %s seconds") % spawn_time,
                 instance=instance)

    @synchronized('image-files-manipulation', 'localdisk-')
    def manage_image_cache(self, context, all_instances):
        # =====================================================================
        # Added for the Local disk support, cache management.
        # As Local disk uses IVM's memory, it becomes important to manage
        # the available disk space that image staging area consumes.
        # =====================================================================
        if self._operator._is_staging_area_full():
            self._operator._clean_up_cache(context)

    @logcall
    def start_lpar_instance(self, instance, image_meta):

        disk_format = None
        if 'disk_format' in image_meta:
            disk_format = image_meta['disk_format']

        # Checking for if the image is an iso
        if disk_format and disk_format != 'iso':
            super(PowerVMLocalDiskOperator, self).start_instance(instance)
            greenthread.spawn(task_state_checker.check_task_state,
                              context.get_admin_context(), instance,
                              self._operator.check_dlpar_connectivity,
                              self._operator.get_refcode)
        else:
            LOG.debug("ephemeral_gb found in instance," +
                      " skipping instance start")

    def resize(self, context, instance, dest, instance_type, network_info,
               block_device_info):
        """Resizes the running VM to new flavor

        :param instance: the instance dictionary information
        :param dest: instance host destination
        :param instanct_type: new flavor specification
        :param network_info: network specifications of VM
        """
        LOG.debug("Entering")

        pvm_op = self._ld_ivm_operator

        # Resize memory and processer
        host_stats = self.get_host_stats(refresh=True)
        delta_mem, delta_vcpus, delta_cpus_units, desired_proc_compat = \
            self._find_resize_deltas(instance, instance_type)
        resource_deltas = {'delta_mem': delta_mem,
                           'delta_vcpus': delta_vcpus,
                           'delta_cpus_units': delta_cpus_units,
                           'desired_proc_compat': desired_proc_compat}
        instance['memory_mb'] = instance_type['memory_mb']
        instance['vcpus'] = instance_type['vcpus']
        old_metalist = instance.get('system_metadata', {})

        if 'new_instance_type_root_gb' in old_metalist:
            if old_metalist['new_instance_type_root_gb']:
                old_metalist['new_instance_type_root_gb'] = instance['root_gb']

        if 'new_instance_type_ephemeral_gb' in old_metalist:
            if old_metalist['new_instance_type_ephemeral_gb']:
                old_metalist['new_instance_type_ephemeral_gb'] = \
                    instance['ephemeral_gb']

        updated_lpar = self._create_lpar_instance(
            instance, network_info, host_stats=host_stats,
            resource_deltas=resource_deltas,
            extra_specs=instance_type['extra_specs'])

        # TODO - This updates the profile
        # Is this the expected behavior for both
        # active and inactive lpars?
        pvm_op.update_lpar(updated_lpar)

        # (rakatipa) Incase of LOCAL returning old disk size
        # disk_info updating db with old disk size
        disk_info = {'is_resize': 'true',
                     'root_gb': instance['root_gb'],
                     'ephemeral_gb': instance['ephemeral_gb']}

        LOG.debug("Exiting")
        return disk_info

    @logcall
    def get_volume_connector(self, instance):
        connector = None
        if CONF.local_or_cinder:
            if not self._hypervisor_hostname:
                self._hypervisor_hostname = self._operator.get_hostname()
            connector = {'host': self._hypervisor_hostname}
        return connector

    @logcall
    @synchronized('odm_lock', 'pvm-')
    def attach_volume(self, connection_info, instance, mountpoint):
        try:
            volume_data = connection_info['data']

            LOG.info(_("Attaching volume_id %s") % volume_data['volume_id'])
            # Before run run_cfg_dev, check whether there is any
            # stale disk with the same LUN id. If so, remove it.

            self._disk_adapter.handle_stale_disk(volume_data)
            aix_conn = self._operator.get_volume_aix_conn_info(volume_data)
            attach_info = self._operator.get_devname_by_aix_conn(aix_conn)
            # Cinder volume attached on the soure VIOS needs to be
            # LPM ready. no_reserve has to be set before a cinder
            # volume is attached to VM. Otherwise reservation cannot
            # be changed unless the volume is detached and reattached
            # again.

            LOG.debug("device to attach: %s" % attach_info['device_name'])

            if CONF.local_or_cinder:
                if volume_data.has_key('is_boot_volume'):
                    attach_info['is_boot_volume'] = volume_data['is_boot_volume']
                    self._disk_adapter.copy_image_file_to_cinder_lv(instance['image_ref'], 
                                                            attach_info)
            lpar_id = self._operator.get_lpar(instance['name'])['lpar_id']
            vhost = self._operator.get_vhost_by_instance_id(lpar_id)
            self._operator.attach_disk_to_vhost(attach_info, vhost)
        except Exception as e:
            volume_id = connection_info['data']['volume_id']
            raise exception.\
                IBMPowerVMVolumeAttachFailed(instance,
                                             volume_id,
                                             e)

#
# local disk operator
#


class LocalDiskIVMOperator(IVMOperator):

    """Integrated Virtualization Manager (IVM) Operator.
    Runs specific commands on an IVM managed system.
    """

    def __init__(self, ivm_connection, virtapi):
        self._virtapi = virtapi
        super(LocalDiskIVMOperator, self).__init__(ivm_connection)

    def get_disk_name_by_vhost(self, vhost):
        """Returns the disk name attached to a vhost.

        :param vhost: a vhost name
        :returns: string -- disk name
        """
        cmd = self.command.lsmap('-vadapter %s -field backing -fmt :' % vhost)
        output = self.run_vios_command(cmd)
        if output:
            disks = output[0].split(':')
            lvName = disks[0]
            return lvName

        return None

    def _fetch_all_active_images(self, context):
        """
        Returns all images added to the glance registry.
        Makes glance api call.
        """
        glance_service = glance.get_default_image_service()

        all_images = glance_service.detail(context)

        glance_image_names = Set()
        for image in all_images:
            if image['status'] == 'active':
                glance_image_names.add(image['id'])

        return glance_image_names

    def _fetch_instances_by_state(self, state, context):
        """
        Returns all instances with supplied status.
        """
        # fetch all instances being managed by this host.
        instfact = compute_dom.ManagedInstanceFactory.get_factory()
        lpars = instfact.find_all_instances_by_host(context, CONF.host)

        managed_instances = Set()
        for lpar in lpars:
            if(lpar['vm_state'] == state):
                managed_instances.add(lpar['image_ref'])
        return managed_instances

    @logcall
    def _fetch_matching_cleanup_list(self, _cleanup_eligible_images):
        """
         Returns all image file names matching all-
         names passed in suppilied list.
        """
        file_names = ""
        for _image_name in _cleanup_eligible_images:
            file_names += _image_name + " "

        commands = ['oem_setup_env',
                    'for name in %s;'
                    ' do ls -ltr | grep $name | awk \'{print $9}\'; done'
                    % file_names,
                    'exit']

        LOG.info(_("Issuing commands %s") % commands)
        # run command
        all_files_in_remote_path = self.run_interactive(commands)

        # (ppedduri): should we have date comparision here?
        return all_files_in_remote_path

    # generic method for fetching instances by supplied status.
    def _fetch_instances_in_building_state(self, context):
        """ Returns all images in BUILD status """
        return self._fetch_instances_by_state(vm_states.BUILDING, context)

    @logcall
    def _delete_image_from_ivm(self, image_to_be_deleted):
        """ Deletes supplied file from ivm """
        # form full file path
        image_file_path = os.path.join(CONF.powervm_img_remote_path,
                                       image_to_be_deleted)

        # prepare delete command
        cmd = "/usr/bin/rm -f %s" % image_file_path
        LOG.info(_("Issuing command %s") % cmd)
        # issue delete
        super(LocalDiskIVMOperator, self).\
            run_vios_command_as_root(cmd)

    @logcall
    def _clean_up_cache(self, context):
        """
         Tries to delete the oldest image file in IVM's image cache directory
         See: nova-<host>.conf's 'powervm_img_remote_path' property
        """
        # try cleaning up
        LOG.info(_("Trying to clean up cache"))
        # fetch all images from glance.
        all_images = self._fetch_all_active_images(context)
        if not all_images:
            # no image exists in glance, skip clean up and notify back.
            LOG.warn(_("Glance registry is empty."))
            return False
        # fetch image-id from instances with BUILDING status
        images_in_building_state = self.\
            _fetch_instances_in_building_state(context)
        # construct a list of clean up eligible images
        cleanup_eligible_images = all_images - images_in_building_state

        # fetch all files from CONF.powervm_img_remote_path,
        # which match cleanup_eligible_images
        fetch_matching_cleanup_list = self._fetch_matching_cleanup_list(
            cleanup_eligible_images)

        if not fetch_matching_cleanup_list:
            LOG.info(_("Could not find any image to be deleted"))
            # return False, since there is no image eligible for delete.
            return False
        # first value from returned list is the Least Accessed one.
        image_to_be_deleted = fetch_matching_cleanup_list[0]
        # delete the image
        self._delete_image_from_ivm(image_to_be_deleted)
        LOG.info(_("Image %s delete command ran successfully")
                 % image_to_be_deleted)
        # return success code
        return True

    @logcall
    def get_staging_free_space(self):
        """
        Returns staging area (configurable param 'powervm_img_remote_path'
        in nova-<host>.conf) free space in kilo bytes.
        """
        # get free disk space
        free_padmin_space = super(LocalDiskIVMOperator, self).\
            get_staging_size(CONF.powervm_img_remote_path)
        LOG.info(_("Free space in staging area : %skb")
                 % free_padmin_space[0])
        return int(free_padmin_space[0])

    def _is_staging_area_full(self):
        """
        Returns 'True' if staging area doesn't have 4GB free space.
        Returns 'False' otherwise.
        """
        staging_free_space = self.get_staging_free_space()
        if staging_free_space >= AVG_IMAGE_SIZE:
            return False
        return True

    @logcall
    def attempt_to_delete_image_cache(self, context, attempt, img_size_mb):
        """
        A recursive functon to free up staging area for the
        specified size.
        Returns True if clean up is successful,
                False if a. clean up failed
                         b. required space can not be accommodated
                             within 3 attempts.
        """
        LOG.info(_("trying to clean up ivm image cache; attempt: %d")
                 % attempt)

        # =====================================================================
        # 1. try to clean up cache
        # 2. if we proceed without any exception and delete unsuccessful,
        #         it means no image is eligible for delete.
        #             Hence, return with code 'False'
        #  3. if we cant proceed due to some exception,
        #         report error and go back.
        #  4. if we get True code and current available memory
        #         affected free memory still lesser than required,
        #             run through the procedure again (call recursive function)
        #
        # =====================================================================

        if attempt == MAX_ATTEMPTS_TO_CACHE_CLEANUP:
            return False
        try:
            if not self._clean_up_cache(context):
                # delete was unsuccessful.
                return False
            # delete succeeded. check for free space.
            staging_free_space_mb = self.get_staging_free_space() / 1024
            if(staging_free_space_mb < img_size_mb):
                attempt += 1
                # recursive call.
                self.attempt_to_delete_image_cache(context, attempt)
            # free space is enough to proceed
            return True
        except Exception as ex:
            msg = (_("Error Cleaning up Cache due to %s") % ex)
            LOG.exception(msg)
            raise ex

    @logcall
    def get_volume_aix_conn_info(self, volume_data):
        """  
        Generate aix device connection information based on volume_data
        information.
        :param volume_data: A dictionary of volume information. For
                            ISCSI volume, it has IQN string in
                            uppercase.
        :ret aix_conn: An AIX connection string: "iqn or None"
        """

        aix_conn = [] 
        if not volume_data:
            return None 
        # form the aix connn
        try: 
            if 'volume_name' in volume_data:
                aix_conn.append(volume_data['volume_name'])

        except Exception as e:
            LOG.error(_("Failed to convert volume_data to conn: %s") % e) 
            return None 
        return aix_conn

    @logcall
    def _get_volume_name(self, volume_name):
        volume_path = '/dev/%s' % volume_name
        if not os.path.exists(volume_path):
            volume_path = '/dev/%s' % volume_name[:15]
            if os.path.exists(volume_path):
                return volume_name[:15]
            else:
                return None
        return volume_name

    @logcall
    def get_devname_by_aix_conn(self, conn_info, all_dev_states=False):
        dev_info = {} 
        disk = [] 
        if conn_info:
            for conn in conn_info:
                disk.append(self._get_volume_name(conn))
        if len(disk) == 1:
            dev_info = {'device_name': disk[0]}
        elif len(disk) > 1: 
            raise exception.\
                IBMPowerVMInvalidLUNPathInfoMultiple(conn_info, disk)
        else:
            raise exception.\
                IBMPowerVMInvalidLUNPathInfoNone(conn_info, disk)

        return dev_info

class PowerVMISCSIDiskOperator(PowerVMBaseOperator):
    def __init__(self, virtapi):
        pvm_conn = common.Connection(CONF.powervm_mgr,
                                     CONF.powervm_mgr_user,
                                     CONF.powervm_mgr_passwd,
                                     CONF.powervm_mgr_port)
        operator = ISCSIIVMOperator(pvm_conn)
        disk_adapter = blockdev.ISCSIDiskAdapter(operator)
        super(PowerVMISCSIDiskOperator, self).__init__(virtapi, 
                                                       operator, 
                                                       disk_adapter)
    
    @logcall
    def _handle_ae_iso(self, instance, injected_files, admin_password,
                       network_info):
        """
        Handle config drive ISO
        :param instance: compute instance to spawn
        :return: (vopt_img_name, vdev_fbo_name)
        """
        vopt_img_name = None
        vdev_fbo_name = None

        iso_remote_path = self._transport_config_data(instance, injected_files,
                                                      admin_password,
                                                      network_info)
        if iso_remote_path is not None:
            vopt_img_name, vdev_fbo_name = self._attach_iso(iso_remote_path,
                                                            instance)

        return (vopt_img_name, vdev_fbo_name)
        
    @logcall
    def spawn(self, context, instance, image_meta, injected_files,
              admin_password, network_info, block_device_info,
              validate_volumes, attach_volumes, validate_attachment):
        """
        Spawn method with extended PowerVC boot-from-volume capability

        :param context: operation context
        :param instance: compute instance to spawn
        :param image_meta: image meta data
        :param network_info: network to plug during spawn
        :param block_device_info: established block_device_mapping before spawn
        :param validate_volumes: callback method to validate volume status
        :param attach_volumes: callback method to attach volumes before boot
        :return: None
        """

        LOG.debug("Going to spawn a new instance on ISCSI storage")
        spawn_start = time.time()

        try:
            try:
                lpar_inst = self._create_lpar_instance(instance,
                                                       network_info,
                                                       host_stats=None)
                # TODO(mjfork) capture the error and handle the error when the
                #             MAC prefix already exists on the
                #             system (1 in 2^28)
                LOG.debug("Creating LPAR instance '%s'" % instance['name'])
                self._operator.create_lpar(lpar_inst)
            except Exception as ex:
                LOG.exception(_("LPAR instance '%s' creation failed") %
                              instance['name'])
                # raise a new exception with mksyscfg msg
                ex_args = {'instance_name': instance['name'],
                           'error_msg': ex}
                raise exception.IBMPowerVMLPARCreationFailed(**ex_args)

            # check the volume state before boot. If fails, exception
            # will be raised and bail out.
            LOG.debug("Validating the volumes...")
            validate_volumes(context, instance['uuid'])

            # TODO(ajiang): Add support to handle ISO installation.
            # An empty boot volume has been created based on the
            # image "volume_mapping" property. We need code here
            # to transfer install iso to VIOS and attach it to
            # LPAR.We are using an manually imported cinder volume
            # as image volume for now.

            LOG.debug("Handling ae iso...")
            vopt_img_name = None
            vdev_fbo_name = None
            if not image_meta['disk_format'] == 'iso':
                vopt_img_name, vdev_fbo_name = self.\
                    _handle_ae_iso(instance,
                                   injected_files,
                                   admin_password,
                                   network_info)

            # (ppedduri,shyama): write a new method, create_iso_vopt
            # have the checks of crating vopt etc, by checking disk
            # format etc. subseq calls to _prepare_iso...
            if image_meta['disk_format'] == 'iso':
                LOG.info(_("Disk format is iso. Preparing ISO image on host."))
                vopt_dev_name = self._disk_adapter.\
                    _prepare_iso_image_on_host(context,
                                               instance,
                                               CONF.powervm_img_local_path,
                                               CONF.powervm_img_remote_path,
                                               image_meta)
                image_detail = {'vopt': vopt_dev_name}
                lpar_id = self._operator.get_lpar(instance['name'])['lpar_id']
                vhost = self._operator.get_vhost_by_instance_id(lpar_id)
                self._operator.attach_disk_to_vhost(image_detail, vhost)
            else:
                self._disk_adapter.create_volume_from_image_v2(
                    context, instance, image_meta['id'])


            # It has been a long way to get here. Attach volumes now.
            LOG.debug("Attaching volumes...")
            attach_volumes(context, instance)
            # last check before we start instance.
            LOG.debug("Validating attachments...")
            validate_attachment(context, instance['uuid'])

            try:
                self.start_san_lpar_instance(instance, image_meta)
            except Exception as ex:
                self._vopt_cleanup(vopt_img_name, vdev_fbo_name)

            if vopt_img_name:
                LOG.debug("Unloading the ISO image")
                self._detach_iso(vopt_img_name, vdev_fbo_name, instance)

        except exception.IBMPowerVMStorageAllocFailed:
            with excutils.save_and_reraise_exception():
                # log errors in cleanup
                try:
                    self._cleanup(instance['name'], destroy_disks=False)
                except Exception:
                    LOG.exception(_('Error while attempting to '
                                    'clean up failed instance launch.'))

        spawn_time = time.time() - spawn_start
        LOG.info(_("Instance spawned in %s seconds") % spawn_time,
                 instance=instance)

    @logcall
    def start_san_lpar_instance(self, instance, image_meta):
        # Changing method name to distinguish this from super class
        # start_instance method. (Helps to restart)
        # if (('ephemeral_gb' in instance and instance['ephemeral_gb'] == 0) or
        #   ('root_gb' in instance and instance['root_gb'] != 0)):
        if image_meta['disk_format'] != 'iso':
            super(PowerVMISCSIDiskOperator, self).start_instance(instance)
            greenthread.spawn(task_state_checker.check_task_state,
                              context.get_admin_context(), instance,
                              self._operator.check_dlpar_connectivity,
                              self._operator.get_refcode)
        else:
            LOG.debug("ephemeral_gb found in instance," +
                      " skipping instance start")        
        
    @logcall
    def _create_lpar_instance(self, instance, network_info, host_stats=None,
                              resource_deltas=None, extra_specs=None):
        """Translates an OpenStack instance info object into an LPAR object
        :param instance: instance info dict
        :param network_info: network info dict
        :param host_stats: host stats dict
        :param resource_deltas: resource delta dict, populated only during
            resize operation. Used for checking availbilit of host resources
        :param extra_specs: extra_specs attribute of instance_type object.
        """
        LOG.debug("Translating an OpenStack instance info object "
                  "into an LPAR object")
        inst_name = instance['name']
        LOG.debug("Instance name: %s" % inst_name)
        lpar_name = self._operator.get_actual_lpar_name(inst_name)
        LOG.debug("Actual LPAR name: %s" % lpar_name)
        if extra_specs is None:
            LOG.debug("Extra specs are not provided")
            inst_type = flavor_obj.Flavor.get_by_id(
                context.get_admin_context(),
                instance['instance_type_id'])
            extra_specs = inst_type['extra_specs']
        LOG.debug("Extra specs: %s" % str(extra_specs))

        self.validate_extraspecs_attr(extra_specs, inst_name)

        if host_stats is None:
            LOG.debug("Host stats are not provided. Getting the host stats")
            host_stats = self.get_host_stats()

        # Memory
        mem = instance['memory_mb']
        mem_min = constants.IBM_POWERVM_MIN_MEM

        LOG.debug("Requested Memory: desired memory=%(mem)d, "
                  "minimum memory=%(mem_min)d" % locals())

        # Use maximum memory multiplier dictionary to calculate
        # maximum memory based on requested memory
        for range, mult in constants.MAX_MEM_MULTIPLIER_DICT.items():
            if mem >= range[0] and mem <= range[1]:
                mem_max = min((mem * mult), host_stats['host_memory_total'])
                break
        LOG.debug("Maximum memory=%(mem_max)d" % locals())
        # If this is a resize operation, don't change max memory unless
        # desired memory is increased above current max memory
        proc_compat_mode = None
        if resource_deltas is not None:
            system_meta = (instance['system_metadata'])
            proc_compat_mode = resource_deltas['desired_proc_compat']
            try:
                curr_max_mem = int(system_meta['max_memory_mb'])
                if mem < curr_max_mem:
                    mem_max = curr_max_mem
                    LOG.debug("Maximum memory adjusted to "
                              "%(mem_max)d" % locals())
            except KeyError:
                # Handle the case where max_memory_mb is not in instance
                # In this case, value of mem_max comes from above.
                pass

        # CPU
        cpus_min = constants.IBM_POWERVM_MIN_CPUS
        cpus_max = int(host_stats['vcpus'])
        cpus = int(instance['vcpus'])
        cpus_units_min = constants.POWERVM_MIN_PROC_UNITS
        proc_units = extra_specs.get('powervm:proc_units')
        if proc_units is None:
            proc_units = proc_utils.get_num_proc_units(cpus)
            cpus_units = ('%.2f' % proc_units)
        else:
            cpus_units = (proc_units)
        extra_specs['powervm:proc_units'] = cpus_units
        desr_proc_compat_mode = extra_specs.\
            get('powervm:processor_compatibility')
        if proc_compat_mode is not None:
            desr_proc_compat_mode = proc_compat_mode
        if desr_proc_compat_mode is None:
            desr_proc_compat_mode = 'default'
        extra_specs['powervm:processor_compatibility'] = desr_proc_compat_mode
        LOG.debug("Requested CPU: desired cpus=%(cpus)d, minimum "
                  "cpus=%(cpus_min)d, maximum cpus=%(cpus_max)d" % locals())
        LOG.debug("Checking for the availability of (requested) "
                  "resources on the host...")
        self._check_host_resources(instance, host_stats, extra_specs,
                                   resource_deltas)
        LOG.debug("Resource availability check done!!")

        # Network
        mac_base_value, virtual_eth_adapters = \
            self._get_virtual_eth_adapters(network_info)
        self.plug_vifs(instance, network_info, raise_err=True)

        # LPAR configuration data
        # max_virtual_slots is hardcoded to 64 since we generate a MAC
        # address that must be placed in slots 32 - 64
        if virtual_eth_adapters:
            virt_eth_adpts_str = "\\" + "\"" + virtual_eth_adapters + "\\" + \
                "\""
        else:
            virt_eth_adpts_str = None

        LOG.debug("Creating a PowerVC LPAR object")
        lpar_inst = LPAR.LPAR(
            name=lpar_name, lpar_env='aixlinux',
            min_mem=mem_min, desired_mem=mem,
            max_mem=mem_max, proc_mode='shared',
            sharing_mode='uncap', min_procs=cpus_min,
            desired_procs=cpus, max_procs=cpus_max,
            min_proc_units=cpus_units_min,
            desired_proc_units=cpus_units,
            max_proc_units=cpus_max,
            lpar_proc_compat_mode=desr_proc_compat_mode,
            virtual_eth_mac_base_value=mac_base_value,
            max_virtual_slots=64,
            virtual_eth_adapters=virt_eth_adpts_str)
        return lpar_inst
    
    @logcall   
    def get_volume_connector(self, instance):
        if self._initiator is None:
            self._initiator = self._operator.get_iscsi_initiator()
        
        if not self._hypervisor_hostname:
            self._hypervisor_hostname = self._operator.get_hostname()
        if not self._hypervisor_hostname:
            self._hypervisor_hostname = CONF.host
        connector = {'ip': CONF.my_ip,
                     'host': CONF.hypervisor_hostname,
                     }
        
        if self._initiator:
            connector['initiator'] = self._initiator
            
        return connector
    
    @logcall
    @synchronized('odm_lock', 'pvm-')
    def attach_volume(self, connection_info, instance, mountpoint):
        try:
            volume_data = connection_info['data']

            LOG.info(_("Attaching volume_id %s") % volume_data['volume_id'])
            # Before run run_cfg_dev, check whether there is any
            # stale disk with the same LUN id. If so, remove it.

            self._disk_adapter.handle_stale_disk(volume_data)
            iscsi_parent = self._operator.get_iscsi_parent_devices()
            iqn_info = self._operator.set_iscsi_cfg(volume_data)
            
            for iparent in iscsi_parent:
                self._operator.run_cfg_dev(iparent)
            LOG.debug("config devices %s %s %s" % iqn_info)
            self._operator.iscsicopyfileto(True)

            aix_conn = self._operator.get_volume_aix_conn_info(volume_data)
            attach_info = self._operator.get_devname_by_aix_conn(aix_conn)
            # Cinder volume attached on the soure VIOS needs to be
            # LPM ready. no_reserve has to be set before a cinder
            # volume is attached to VM. Otherwise reservation cannot
            # be changed unless the volume is detached and reattached
            # again.

            pvid = self._operator.get_hdisk_pvid(attach_info['device_name'])
            if pvid == 'none':
                # set reserve_policy to no reserve in order to be
                # LPM capable. The reserve policy has to be set before
                # the disk is opened by vscsi driver to attach to The
                # host. So it cannot be done during migration time.
                self._operator.chdev_hdisk_pvid(attach_info['device_name'])
            LOG.debug("device to attach: %s" % attach_info['device_name'])

            if volume_data.haskey('is_boot_volume'):
                attach_info['is_boot_volume'] = volume_data['is_boot_volume']
                self._disk_adapter.copy_image_file_to_cinder_lv(instance['image_ref'],
                                                                attach_info)

            lpar_id = self._operator.get_lpar(instance['name'])['lpar_id']
            vhost = self._operator.get_vhost_by_instance_id(lpar_id)
            self._operator.attach_disk_to_vhost(attach_info, vhost)
        except Exception as e:
            volume_id = connection_info['data']['volume_id']
            raise exception.\
                IBMPowerVMVolumeAttachFailed(instance,
                                             volume_id,
                                             e)
    
    def detach_volume(self, connection_info, instance, mountpoint):
        
        volume_data = connection_info['data']
        """ delete iqn info from /etc/iscsi/targets file """
        self._operator.del_iscsi_cfg(volume_data)

        s = super(PowerVMISCSIDiskOperator, self)
        s.detach_volume(connection_info,instance,mountpoint)

    def image_attach_volume(self, image_meta, image_data, 
                            connection_info, encryption=None):
        try:
            volume_data = connection_info['data']

            LOG.info(_("Attaching volume_id %s") % volume_data['volume_id'])
            # Before run run_cfg_dev, check whether there is any
            # stale disk with the same LUN id. If so, remove it.

            self._disk_adapter.handle_stale_disk(volume_data)
            iscsi_parent = self._operator.get_iscsi_parent_devices()
            iqn_info = self._operator.set_iscsi_cfg(volume_data)
            
            for iparent in iscsi_parent:
                self._operator.run_cfg_dev(iparent)
            LOG.debug("config devices %s %s %s" % iqn_info)
            self._operator.iscsicopyfileto(True)

            aix_conn = self._operator.get_volume_aix_conn_info(volume_data)
            attach_info = self._operator.get_devname_by_aix_conn(aix_conn)
            pvid = self._operator.get_hdisk_pvid(attach_info['device_name'])
            if pvid == 'none':
                # set reserve_policy to no reserve in order to be
                # LPM capable. The reserve policy has to be set before
                # the disk is opened by vscsi driver to attach to The
                # host. So it cannot be done during migration time.
                self._operator.chdev_hdisk_pvid(attach_info['device_name'])
            LOG.debug("device to attach: %s" % attach_info['device_name'])
            image_meta['hdisk_id'] = attach_info['device_name']
            self.image_create_update(image_meta, image_data)
        except Exception as e:
            volume_id = connection_info['data']['volume_id']
            raise exception.\
                IBMPowerVMVolumeAttachFailed_v2(volume_id,e)

class ISCSIIVMOperator(IVMOperator):
    def __init__(self,ivm_connection):
        super(ISCSIIVMOperator,self).__init__(ivm_connection)
    
    def get_iscsi_initiator(self):
        try:
            contents = utils.read_file_as_root('/etc/iscsi/initiatorname.iscsi')
        except base_exception.FileNotFound:
            return 'iqn.2010-10.org.openstack'
        
        for l in contents.split('\n'):
            if l.startswith('InitiatorName='):
                return l[l.index('=') + 1:].strip()
    
    @logcall
    def get_volume_aix_conn_info(self, volume_data):
        """
        Generate aix device connection information based on volume_data
        information.
        :param volume_data: A dictionary of volume information. For
                            ISCSI volume, it has IQN string in
                            uppercase.
        :ret aix_conn: An AIX connection string: "iqn and lun or None"
        """

        iqn = None
        lun = None
        aix_conn = []
        if not volume_data:
            return None
        # form the aix connn
        try:
            if 'target_iqn' in volume_data:
                iqn = volume_data['target_iqn']
            if 'target_lun' in volume_data:
                lun = volume_data['target_lun']
            if iqn and lun is not None:
                aix_conn.append((iqn,lun))
            else:
                return None

        except Exception as e:
            LOG.error(_("Failed to convert volume_data to conn: %s") % e)
            return None
        return aix_conn
    
    @logcall
    def get_all_hdisk(self):
        """
        get local aix all hdisk 
        """
        hdisks = []
        cmd = "ioscli lsdev -type disk | grep 'iSCSI'| awk '{print $1}'"
        output = self.run_command(cmd)
        for h in output:
            hdisks.append(h)
        return hdisks
    
    @logcall
    def get_hdisk_by_iqn(self,hdisks,iqn, lun='0x0'):
        if iqn is None:
            return None
        if hdisks is None:
            return None
        attrs = ['target_name','port_num', 'lun_id', 'host_addr','pvid']
        hdiskattr = {}
        hdiskattrs = {}
        hdisklist = []
        for hdisk in hdisks:
            cmd = "ioscli lsdev -dev %s -attr target_name" % hdisk
            nameoutput = self.run_command(cmd)
            cmd = "ioscli lsdev -dev %s -attr lun_id" % hdisk
            lunoutput = self.run_command(cmd)
            if nameoutput[2] == str(iqn) and lunoutput[2] == str(lun):
                for attr in attrs:
                    attrcmd = "ioscli lsdev -dev %s -attr %s" % (hdisk,attr)
                    attroutput = self.run_command(attrcmd)
                    if attroutput is not None:
                        hdiskattr[attr] = attroutput[2]
                hdiskattrs['name'] = hdisk
                hdiskattrs['attr'] = hdiskattr
                hdisklist.append(hdiskattrs)
        return hdisklist
    
    @logcall
    def chdev_hdisk_pvid(self,hdisk,pv=False):
        if hdisk is None:
            return None
        if not pv:
            cmd = "ioscli chdev -dev %s -attr pv=yes" % hdisk
        else:
            cmd = "ioscli chdev -dev %s -attr pv=no" % hdisk
        output = self.run_command(cmd)
        
    
    @logcall
    def get_hdisk_pvid(self,hdisk):
        if hdisk is None:
            return None
        cmd = "ioscli lsdev -dev %s -attr pvid"  % hdisk
        output = self.run_command(cmd)
        if len(output) > 1:
            return output[2]
        return None
        
    @logcall
    def get_devname_by_aix_conn(self, conn_info, all_dev_states=False):
        """
        get the device name by AIX conn info.

        :param conn_info: a list with aix conn strings.
        """
        dev_info = {}
        disk = []
        outputs = []
        hdisks = self.get_all_hdisk()
        if conn_info:
            for iqn,lun in conn_info:
                output = self.get_hdisk_by_iqn(hdisks, iqn, lun)
                #cmd = "ioscli lspath -conn %s -fmt :" % conn
                
                #output = self.run_command(cmd)
                #outputs.append(output)
                if len(output) == 0:
                    continue
                # output looks like this:
                # Enabled:hdisk2:fscsi0:50050768011052ec,1000000000000
                #if(all_dev_states or
                #   output[0].split(':')[0].lower() == 'enabled'):
                #    devname = output[0].split(':')[1]
                for o in output:
                    devname = o['name']
                    devattr = o['attr']
                    
                    if(devattr['pvid'] == 'none'):
                        self.chdev_hdisk_pvid(devname,False)
                        
                    if disk.count(devname) == 0:
                        disk.append(devname)

        # the disk list should have only one device name for
        # MPIO disk since the conn_info is the paths for one LUN.

        if len(disk) == 1:
            dev_info = {'device_name': disk[0]}
        elif len(disk) > 1:
            raise exception.\
                IBMPowerVMInvalidLUNPathInfoMultiple(conn_info, outputs)
        else:
            raise exception.\
                IBMPowerVMInvalidLUNPathInfoNone(conn_info, outputs)

        return dev_info
    
    @logcall
    def set_iscsi_cfg(self,conn_info):
            
        if 'target_iqn' in conn_info:
            iqn = conn_info['target_iqn']
        if 'target_portal' in conn_info:
            host_port = conn_info['target_portal']
            host = host_port.split(':')[0]
            port = host_port.split(':')[1]
            if port is None:
                port = '3260'
        iqn_info = (host,port,iqn)
        iscsi_conf = '/etc/iscsi/targets'
        #iscsi_conf_bak = '/etc/iscsi/targets_bak'
        self.appendiscsifile(iscsi_conf, iqn_info)
        self.iscsicopyfileto(False)
        self.writeiscsifile(iscsi_conf, iqn_info)
        return (host,port,iqn)
    
    @logcall
    @synchronized('writeiscsifile', 'iscsi-')
    def writeiscsifile(self,f,iqn_info):
        writeiqn = str(' '.join(iqn_info)) + '\n'
        with file(f,'w') as fn:
            fn.write(writeiqn)
            
    @logcall
    @synchronized('appendiscsifile', 'iscsi-')
    def appendiscsifile(self,f, iqn_info):
        is_write = True
        writeiqn = str(' '.join(iqn_info))
        with file(f, 'r') as rfn:
            cache_infos = rfn.readlines()
            for cache_info_n in cache_infos:
                cache_info = cache_info_n.split('\n')[0]
                if str(cache_info) == writeiqn:
                    is_write = False
                    break
                
            if is_write:
                writeiqn = str(' '.join(iqn_info)) + '\n'
                with file(f,'a') as fn:
                    fn.write(writeiqn)
    
    @logcall
    def iscsicopyfileto(self, recopy=False):
        iscsi_conf = '/etc/iscsi/targets'
        iscsi_conf_bak = '/etc/iscsi/targets_bak'
        self.copyfileto(iscsi_conf, iscsi_conf_bak, recopy)
        
    @logcall
    @synchronized('copyfileto', 'iscsi-')
    def copyfileto(self, sourceDir,  targetDir, recopy=False):
        if recopy:
            tmpDir = sourceDir
            sourceDir = targetDir
            targetDir = tmpDir
            
        if not os.path.exists(sourceDir):
            LOG.debug(_("%s  :file is not exist") % sourceDir)
        else:
        #iscsi_conf_bak = '/etc/iscsi/targets_bak'
            LOG.debug(_("copy file %s to %s") % (sourceDir, targetDir))
            shutil.copy(sourceDir,  targetDir)
            
    @logcall
    @synchronized('deletetargets', 'iscsi-')
    def del_iscsi_cfg(self,conn_info):
        if 'target_iqn' in conn_info:
            iqn = conn_info['target_iqn']
        if 'target_portal' in conn_info:
            host_port = conn_info['target_portal']
            host = host_port.split(':')[0]
            port = host_port.split(':')[1]
            if port is None:
                port = '3260'
        iqn_info = (host,port,iqn)
        iscsi_conf = '/etc/iscsi/targets'
        if not os.path.exists(iscsi_conf):
            LOG.debug(_("%s  :file is not exist") % iscsi_conf)
            
        is_change = False
        writeiqn = str(' '.join(iqn_info)) + '\n'
        with file(iscsi_conf, 'r') as rfn:
            cache_infos = rfn.readlines()

        #print cache_infos
        cache_infos_num = cache_infos.count(writeiqn)
        for cache_info_n in range(cache_infos_num):
                cache_infos.remove(writeiqn)
                is_change = True
        
        if is_change:
            with file(iscsi_conf,'w') as fn:
                fn.writelines(cache_infos)
    
    @logcall
    def get_iscsi_parent_devices(self):
        """
        Get the iSCSI device names dict with PHB as the keyword
        """
        iscsis = []
        command = \
            "ioscli lsdev -field name description -fmt :|grep 'iSCSI Protocol Device' | awk -F':' '{print $1}'"
        output = self.run_command(command)

        if output and len(output) > 0:
            for dev in output:
                if dev.startswith('iscsi'):
                    iscsis.append(dev)
        return iscsis
    
    @logcall
    def get_disk_uid_by_name(self, disk_name):
        """
        Return the unique id of the disk by its name.

        :param disk_name: The disk name
        """
        LOG.debug("Entering (disk_name = %s)" % disk_name)
        cmd = 'ioscli lsdev -dev %s -attr  target_name| tail -1' % disk_name
        target_name = self.run_command(cmd)
        if not target_name:
            return  None
        cmd = 'ioscli lsdev -dev %s -attr  lun_id| tail -1' % disk_name
        lun_id = self.run_command(cmd)
        lun_id = int(str(lun_id[0]),16)
        output = target_name[0] + '-' + str(lun_id)
        if not output:
            return None
        #uid = None
        #if len(output[0]):
        #    match = re.search(r'^[0-9]{5}([0-9A-F]{32}).+$', output[0])
        #    uid = match.group(1)
        #uid = 
        LOG.debug("Exiting (uid = %s)" % output)
        #return uid
        return output
