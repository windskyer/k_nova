#
#
# All Rights Reserved.
# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# Copyright 2011 Piston Cloud Computing, Inc.
# Copyright 2012-2013 Red Hat, Inc.
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

from oslo.config import cfg
from oslo import messaging
from nova import rpc
from nova.compute import api as base_api
from nova.compute import rpcapi as base_rpcapi
from nova.objects import base as objects_base
from nova.openstack.common import log as logging
from paxes_nova.objects.network import dom_kvm

from nova.openstack.common import jsonutils

from paxes_nova.scheduler import rpcapi as scheduler_rpcapi

cfg.CONF.register_opts([
    cfg.IntOpt('onboard_query_timeout', default=600,
               help='Timeout for RPC Query Resource Details for On-boarding'),
    cfg.IntOpt('verify_connection_timeout', default=5,
               help='Timeout for RPC Verify Connection Timeout'),
    cfg.IntOpt('check_live_migrate_timeout', default=600,
               help='Timeout for migration validation methods'),
    cfg.IntOpt('pre_live_migration_timeout', default=900,
               help='Timeout for pre_live_migration'),
    cfg.IntOpt('post_live_migration_at_destination_timeout', default=3600,
               help='Timeout for post_live_migration_at_destination'),
    cfg.IntOpt('is_instance_on_host_timeout', default=60,
               help='Timeout for is_instance_on_host'),
    cfg.BoolOpt('wssh_enabled',
                default=True,
                help='Enable VNC related features'),
    cfg.StrOpt('wsshproxy_base_url',
               default='ws://127.0.0.1:6081/remote',
               help='Location of VNC console proxy, in the form '
                    '"ws://127.0.0.1:6081/remote"'),
])

LOG = logging.getLogger(__name__)


class PowerVCComputeAPI(base_api.API):

    """PowerVC Extension to Compute API for Compute Manager communication"""

    def __init__(self, *args, **kwargs):
        super(PowerVCComputeAPI, self).__init__(*args, **kwargs)
        self.compute_rpcapi = PowerVCComputeRPCAPI()

        self.scheduler_rpcapi = scheduler_rpcapi.SchedulerAPI()


#     @wrap_check_policy
#     @check_instance_host
    def get_ssh_console(self, context, instance, console_type):
        """Get a url to an instance Console."""
#         connect_info = self.compute_rpcapi.get_vnc_console(context,
#                 instance=instance, console_type=console_type)
        connect_info = self.compute_rpcapi.get_ssh_console(context,
                instance=instance, console_type=console_type)

        self.consoleauth_rpcapi.authorize_console(context,
                connect_info['token'], console_type,
                connect_info['host'], connect_info['port'],
                connect_info['internal_access_path'], instance['uuid'])

        return {'url': connect_info['access_url']}

    @base_api.hooks.add_hook("create_image")
    def image_create(self, context, image_meta, image_data):
        self.scheduler_rpcapi.image_create(context, image_meta, image_data)

class PowerVCHostAPI(base_api.HostAPI):

    """PowerVC Extension to Host API for Compute Manager communication"""

    def __init__(self, *args, **kwargs):
        super(PowerVCHostAPI, self).__init__(*args, **kwargs)
        self.rpcapi = PowerVCComputeRPCAPI()

    def change_vif_sea(self, context, data, host_name):
        """Call to make the SEA change for a VIF"""
        return self.rpcapi.change_vif_sea(context, data,
                                          host_name)

    def discover_instances(self, context, host):
        """Returns a list of all of the VM's that exist on the Host"""
        return self.rpcapi.discover_instances(context, host)

    def query_instances(self, context, host, instance_ids):
        """Returns details about the VM's that were requested"""
        return self.rpcapi.query_instances(context, host, instance_ids)

    def get_next_instances_chunk(self, context, host, identifier):
        """Provides Chunking of VM's Lists to avoid QPID Limits"""
        return self.rpcapi.get_next_instances_chunk(context, host, identifier)

    def verify_host_running(self, context, host, max_wait=0):
        """Verifies the nova-compute service for the Host is running"""
        # We will default to checking once, but allow the caller to check more
        for index in range(max_wait / cfg.CONF.verify_connection_timeout + 1):
            if self.rpcapi.verify_host_running(context, host):
                index = index + 1
                return True
        return False

    def unmanage_host(self, context, host):
        """Allows the Driver to do any necessary cleanup on Host Removal"""
        # Only call unmanage if the Nova Compute process is running
        if self.verify_host_running(context, host):
            return self.rpcapi.unmanage_host(context, host)

    def remanage_host(self, context, new_mgmt_ip):
        """Tells the old Mgmt System that a new Mgmt System is managing"""
        return self.rpcapi.remanage_host(context, new_mgmt_ip)

    def get_host_ovs(self, context, host):
        """Retrieve ovs host data from a single kvm host"""
        LOG.info(_("get_host_ovs initiated for %s" % host))

        ovs_dom = None
        try:
            retVal = self.rpcapi.get_host_ovs(context, host)
            ovs_dom = dom_kvm.HostOVSNetworkConfig(host)
            ovs_dom.from_dict(retVal)
        except Exception as e:
            LOG.exception(e)
            if ovs_dom is None:
                ovs_dom = dom_kvm.HostOVSNetworkConfig(host)

            error_obj = dom_kvm.Error("%s" % e)
            ovs_dom.error_list.append(error_obj)

        return ovs_dom

    def update_host_ovs(self, context, host, dom, force_flag, rollback):
        """Verify and/or update kvm host ovs data from dom"""
        LOG.info(_("update_host_ovs initiated for %s" % host))
        ERRORS_KEY = "errors"
        retVal = {}
        try:
            retVal = self.rpcapi.update_host_ovs(context,
                                                 host,
                                                 dom,
                                                 force_flag,
                                                 rollback)
        except Exception as exc:
            LOG.error(exc)
            if ERRORS_KEY not in retVal:
                retVal[ERRORS_KEY] = \
                    [{"%s" % exc.__class__.__name__: "%s" % exc}]
            else:
                retVal[ERRORS_KEY].append(
                    {"%s" % exc.__class__.__name__: "%s" % exc})

        return retVal
    
    def ssh_trust(self, context, host_name, public_key):
        host_name = self._assert_host_exists(context, host_name, must_be_up=True)
        return self.rpcapi.ssh_trust(context, host_name, public_key)


class PowerVCComputeRPCAPI(base_rpcapi.ComputeAPI):

    """PowerVC Extension to RPC API for Compute Manager communication"""

    def __init__(self):
        super(PowerVCComputeRPCAPI, self).__init__()
        self.topic = cfg.CONF.compute_topic

    def change_vif_sea(self, context, data, host):
        """Makes a Unplug/Plug call into the VIF Driver"""
        msg = self.make_msg('change_vif_sea', data=data)
        topic = '%s.%s' % (self.topic, host)
        return self.call(context, msg, topic=topic)

    def discover_instances(self, context, host):
        """Returns a list of all of the VM's that exist on the Host"""
        msg = self.make_msg('discover_instances')
        topic = '%s.%s' % (self.topic, host)
        return self.call(context, msg, topic=topic,
                         timeout=cfg.CONF.onboard_query_timeout)

    def query_instances(self, context, host,
                        instance_ids, allow_unsupported=False):
        """Returns details about the VM's that were requested"""
        msg = self.make_msg('query_instances', instance_ids=instance_ids,
                            allow_unsupported=allow_unsupported)
        topic = '%s.%s' % (self.topic, host)
        return self.call(context, msg, topic=topic,
                         timeout=cfg.CONF.onboard_query_timeout)

    def get_next_instances_chunk(self, context, host, identifier):
        """Provides Chunking of VM's Lists to avoid QPID Limits"""
        msg = self.make_msg('get_next_instances_chunk', identifier=identifier)
        topic = '%s.%s' % (self.topic, host)
        return self.call(context, msg, topic=topic)

    def verify_host_running(self, context, host):
        """Verifies the nova-compute service for the Host is running"""
        msg = self.make_msg('verify_host_running')
        topic = '%s.%s' % (self.topic, host)
        try:
            return self.call(context, msg, topic=topic,
                             timeout=cfg.CONF.verify_connection_timeout)
        # If we got a timeout, just log for debug that the process is down
        except Exception:
            LOG.debug('RPC verify_host_running timed-out for ' + host)
            return False

    def unmanage_host(self, context, host):
        """Allows the Driver to do any necessary cleanup on Host Removal"""
        msg = self.make_msg('unmanage_host')
        topic = '%s.%s' % (self.topic, host)
        return self.call(context, msg, topic=topic)

    def remanage_host(self, context, new_mgmt_ip):
        """Tells the old Mgmt System that a new Mgmt System is managing"""
        # We will do a no-op now until the Compute Mgr is overriden on KVM
        pass

    def get_host_ovs(self, context, host):
        """ Make rpc call to retrieve ovs host data from kvm host"""
        LOG.info(_("initiated get_host_ovs rpc call to host %s") % host)

        msg = self.make_msg('get_host_ovs')
        topic = '%s.%s' % (self.topic, host)

        retval = {}

        try:
            retval = self.call(context, msg, topic=topic, timeout=10)

        except messaging.MessagingTimeout as exc:
            LOG.exception(_("Exception from RPC call for host-ovs GET for "
                            "host %s." % host))
            """
            If we get here, we know that we have no return data from
            the call and therefore must create a new dict representing
            the dom that contains the error info for the timeout.
            """
            error_dom = dom_kvm.HostOVSNetworkConfig(host)
            error_obj = dom_kvm.Error("%s" % exc)
            error_dom.error_list = [error_obj]

            retval = error_dom.to_dict()

        return retval

    def update_host_ovs(self, context, host, dom, force_flag, rollback):
        """ Make rpc call to update ovs host data on kvm host"""
        LOG.info(_("initiated update_host_ovs rpc call to host %s") % host)
        ERRORS_KEY = "errors"

        msg = self.make_msg('update_host_ovs',
                            dom=dom,
                            force_flag=force_flag,
                            rollback=rollback)
        topic = '%s.%s' % (self.topic, host)

        retVal = {}

        try:
            retVal = self.call(context, msg, topic=topic, timeout=120)

        except messaging.MessagingTimeout as exc:
            LOG.exception(_("Exception from RPC call for host-ovs PUT for "
                            "host %s." % host))
            """
            If we get here, we know that we have no return data from
            the call and therefore must create a new dict containing
            the error info for the timeout.
            """
            LOG.error(exc)
            if ERRORS_KEY not in retVal:
                retVal[ERRORS_KEY] = \
                    [{"%s" % exc.__class__.__name__: "%s" % exc}]
            else:
                retVal[ERRORS_KEY].append(
                    {"%s" % exc.__class__.__name__: "%s" % exc})

        return retVal

    def check_can_live_migrate_destination(self, ctxt, instance, destination,
                                           block_migration, disk_over_commit):
        topic = '%s.%s' % (self.topic, destination)
        return self.call(ctxt,
                         self.make_msg('check_can_live_migrate_destination',
                                       instance=instance,
                                       block_migration=block_migration,
                                       disk_over_commit=disk_over_commit),
                         topic,
                         timeout=cfg.CONF.check_live_migrate_timeout,
                         serializer=objects_base.NovaObjectSerializer())

    def check_can_live_migrate_source(self, ctxt, instance, dest_check_data):
        topic = '%s.%s' % (self.topic, instance['host'])
        return self.call(ctxt, self.make_msg('check_can_live_migrate_source',
                                             instance=instance,
                                             dest_check_data=dest_check_data),
                         topic,
                         timeout=cfg.CONF.check_live_migrate_timeout,
                         serializer=objects_base.NovaObjectSerializer())

    def pre_live_migration(self, ctxt, instance, block_migration, disk,
                           host, migrate_data=None):
        topic = '%s.%s' % (self.topic, host)
        return self.call(ctxt, self.make_msg('pre_live_migration',
                                             instance=instance,
                                             block_migration=block_migration,
                                             disk=disk,
                                             migrate_data=migrate_data),
                         topic,
                         timeout=cfg.CONF.pre_live_migration_timeout,
                         serializer=objects_base.NovaObjectSerializer())

    def post_live_migration_at_destination(self, ctxt, instance,
                                           block_migration, host,
                                           migrate_data):
        topic = '%s.%s' % (self.topic, host)
        return self.call(ctxt,
                         self.make_msg('post_live_migration_at_destination',
                                       instance=instance,
                                       block_migration=block_migration,
                                       migrate_data=migrate_data),
                         topic,
                         timeout=(cfg.CONF.
                                  post_live_migration_at_destination_timeout),
                         serializer=objects_base.NovaObjectSerializer())

    def is_instance_on_host(self, ctxt, instance_uuid, instance_name, host):
        """
        Verify if an instance is on a remote host
        """
        answer = True
        topic = '%s.%s' % (self.topic, host)
        mesg = self.make_msg('is_instance_on_host',
                             instance_uuid=instance_uuid,
                             instance_name=instance_name)
        try:
            answer = self.call(ctxt,
                               mesg,
                               topic,
                               timeout=(cfg.CONF.
                                        is_instance_on_host_timeout),
                               serializer=objects_base.NovaObjectSerializer())
        except Exception as e:
            LOG.exception(_('Exception encountered when checking if an '
                            'instance %(name)s (%(uuid)s) is being managed '
                            'by remote host %(host)s. Exception: %(exp)s') %
                          {'uuid': instance_uuid, 'name': instance_name,
                           'host': host, 'exp': e})
        finally:
            return answer

    def end_compute_with_hmc(self, context, data):
        """
        End nova compute process if it's communicating via the
        specified HMC.
        """
        LOG.debug('Making end_compute_with_hmc cast %s' % data)
        msg = self.make_msg('end_compute_with_hmc', data=data)
        return self.fanout_cast(context, msg, topic=self.topic)

    def make_msg(self, method, **kwargs):
        """
        This method used to exist in base OpenStack, but was removed during
        Icehouse.  It is included here to preserve compatibility with existing
        PowerVC code.
        """
        return {'method': method, 'namespace': None, 'args': kwargs}
    
    def ssh_trust(self, ctxt, host, public_key):
        version = self._get_compat_version('3.0', '2.0')
        cctxt = self.client.prepare(server=host, version=version)
        return cctxt.call(ctxt, 'ssh_trust', public_key=public_key)

    def call(self, ctxt, rpc_message, topic, version='3.0', timeout=None,
             serializer=None):
        """
        This method used to exist in base OpenStack, but was removed during
        Icehouse.  It is included here to preserve compatibility with existing
        PowerVC code.
        """
        topic, _sep, server = topic.partition('.')
        version = self._get_compat_version(version, '3.0')
        cctxt = rpc.get_client(messaging.Target(topic=topic,
                                                server=server or None,
                                                version=version),
                               serializer=serializer)
        method = rpc_message['method']
        kwargs = rpc_message['args']

        cctxt = cctxt.prepare(timeout=timeout, version=version)
        return cctxt.call(ctxt, method, **kwargs)

    def get_ssh_console(self, ctxt, instance, console_type):
#         if self.client.can_send_version('3.2'):
#             version = '3.2'
#         else:
#             # NOTE(russellb) Havana compat
#             version = self._get_compat_version('3.0', '2.0')
#             instance = jsonutils.to_primitive(instance)
        version = self._get_compat_version('3.0', '2.0')
        instance = jsonutils.to_primitive(instance)
        cctxt = self.client.prepare(server=base_rpcapi._compute_host(None, instance),
                version=version)
        return cctxt.call(ctxt, 'get_ssh_console',
                          instance=instance, console_type=console_type)

    def image_create(self, ctxt, image_meta, image_data, host):
        version = self._get_compat_version('3.0', '2.0')
        cctxt = self.client.prepare(server=host, version=version)
        msg_kwargs = {'image_meta': image_meta,
                      'image_data': image_data}
        cctxt.cast(ctxt, 'image_create', **msg_kwargs)
