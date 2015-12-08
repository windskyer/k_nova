# vim: tabstop=4 shiftwidth=4 softtabstop=4

# =================================================================
# =================================================================

from eventlet import greenthread
from nova import rpc
from nova.compute import power_state
from nova.compute import task_states
from nova.compute import vm_states
from nova.openstack.common import log as logging

from paxes_nova.virt.ibmpowervm.common import exception
from paxes_nova import logcall
from paxes_nova.compute import api

from oslo.config import cfg

from paxes_nova import _

LOG = logging.getLogger(__name__)
CONF = cfg.CONF


@logcall
def verify_reserve_policy(reserve_policy, instance_name, disk_name):
    """
    The reserve policy of the hdisk attached to the lpar
    must be set to 'no_reserve'

    :param reserve_policy: The reserve policy to check
    :param intsance_name: The name of the instance (for logging)
    :param disk_name: The name of the disk (for logging)
    """
    if ((reserve_policy != 'no_reserve') and (reserve_policy != 'NoReserve')):
        error = (_("Cannot migrate %(instance)s because "
                   "the reserve policy on disk %(hdisk)s is set to "
                   "%(policy)s. It must be set to no_reserve.") %
                 {'instance': instance_name,
                  'hdisk': disk_name,
                  'policy': reserve_policy})
        LOG.exception(error)
        raise exception.IBMPowerVMMigrationFailed(error)


@logcall
def verify_rmc_state(rmc_state, instance_name):
    """
    The rmc state of the instance must be 'active'

    :param rmc_state: The rmc_state to check
    :param intsance_name: The name of the instance (for logging)
    """
    if rmc_state != 'active':
        error = (_("Cannot live migrate %(inst)s because its RMC "
                   "state is %(rmc)s. The RMC state must be active.") %
                 {'inst': instance_name,
                  'rmc': rmc_state})
        LOG.exception(error)
        raise exception.IBMPowerVMMigrationFailed(error)


@logcall
def verify_dlpar_enabled(dlpar, instance_name):
    """
    Dlpar must be enabled on the instance

    :param dlpar: Boolean value indicating dlpar is enabled
    :param intsance_name: The name of the instance (for logging)
    """
    if not dlpar:
        error = (_("Cannot live migrate %s because DLPAR "
                   "is not enabled.") % instance_name)
        LOG.exception(error)
        raise exception.IBMPowerVMMigrationFailed(error)


@logcall
def verify_proc_compat_mode(lpar_compat_mode, host_compat_modes,
                            instance_name):
    """
    The processor compatibility mode of the lpar must be in the list of
    supported processor compatibility modes on the target host

    :param lpar_compat_mode: The proc compat mode of the lpar
    :param host_compat_modes: The proc compat modes of the target host
    :param instance_name: The name of the instance (for logging)
    """
    mode_list = host_compat_modes.split(',')
    if not lpar_compat_mode in mode_list:
        error = (_("Cannot migrate %(inst)s because its "
                   "processor compatibility mode %(mode)s "
                   "is not in the list of modes %(modes)s "
                   "supported by the target host.") %
                 {'inst': instance_name,
                  'mode': lpar_compat_mode,
                  'modes': mode_list})
        LOG.exception(error)
        raise exception.IBMPowerVMMigrationFailed(error)


@logcall
def verify_host_capacity_for_migration(migrations_allowed, migrations_running,
                                       instance_name, host_name):
    """
    The host can only support a limited number of concurrent migrations.
    Verify there is room for another.

    :param migrations_allowed: The number of migrations allowed
    :param migrations_running: The number of migrations in progress
    :param instance_name: The name of the instance (for logging)
    :param host_name: The name of the host (for logging)
    """
    if migrations_allowed == migrations_running:
        error = (_("Cannot migrate %(inst)s because host %(host)s "
                   "only allows %(allowed)s concurrent migrations, and "
                   "%(running)s migrations are currently running.") %
                 {'inst': instance_name,
                  'host': host_name,
                  'allowed': migrations_allowed,
                  'running': migrations_running})
        LOG.exception(error)
        raise exception.IBMPowerVMMigrationFailed(error)


@logcall
def verify_logical_memory_block_size(source_lmb, target_lmb, instance_name):
    """
    The logical memory block size of the source and target host
    in a live migration operation must be the same.

    :param source_lmb: The block size of the source host
    :param target_lmb: The block size of the target host
    :param instance_name: The name of the instance (for logging)
    """
    if (source_lmb != target_lmb):
        error = (_("Cannot migrate %(inst)s because the logical "
                   "memory block size of the source(%(source_lmb)sMB) "
                   "does not match the logical memory block size of the "
                   "target(%(target_lmb)sMB).") % {'inst': instance_name,
                                                   'source_lmb': source_lmb,
                                                   'target_lmb': target_lmb})
        LOG.exception(error)
        raise exception.IBMPowerVMMigrationFailed(error)


@logcall
def send_migration_failure_notification(context, instance,
                                        tgt_host, exception):
    """
    Sends a notification of live migration failure to the GUI

    :param context: security context
    :param instance: The instance that was migrating
    :param tgt_host: The target host name
    :param exception: The exception that was thrown
    """

    if hasattr(exception, 'message'):
        err_msg = _('%s') % exception.message
    else:
        err_msg = _('%s') % exception

    # Send error notification
    info = {'msg': _('Migration of virtual machine {instance_name} '
            'to host {host_name} failed. '
            '{error}'),
            'instance_id': instance['uuid'],
            'instance_name': instance['display_name'],
            'host_name': tgt_host,
            'error': err_msg}
    notifier = rpc.get_notifier(service='compute', host=CONF.host)
    notifier.error(context, 'compute.instance.log', info)


@logcall
def send_migration_success_notification(context, instance, src_host, tgt_host):
    """
    Sends a notification of live migration failure to the GUI

    :param context: security context
    :param instance: The instance that was migrating
    :param src_host: The source host name
    :param tgt_host: The target host name
    """

    # Send error notification
    info = {'msg': _('Migration of virtual machine {instance_name} from '
            '{source_host} to {target_host} was successful.'),
            'instance_id': instance['uuid'],
            'instance_name': instance['display_name'],
            'source_host': src_host,
            'target_host': tgt_host}
    notifier = rpc.get_notifier(service='compute', host=CONF.host)
    notifier.info(context, 'compute.instance.log', info)


@logcall
def send_migration_notification(context, instance, priority, message):
    """
    Sends a general live migration notification to the GUI

    :param context: security context
    :param instance: The instance that was migrating
    :param priority: The level of message (DEBUG, WARN, INFO, ERROR, CRITICAL)
    :param message: The message to print
    """

    # Send event notification
    info = {'msg': '{message}',
            'instance_id': instance['uuid'],
            'instance_name': instance['display_name'],
            'message': message}
    notifier = rpc.get_notifier(service='compute', host=CONF.host)
    if priority == 'error':
        notifier.error(context, 'compute.instance.log', info)
    elif priority == 'info':
        notifier.info(context, 'compute.instance.log', info)
    elif priority == 'critical':
        notifier.critical(context, 'compute.instance.log', info)
    elif priority == 'warn':
        notifier.warn(context, 'compute.instance.log', info)
    elif priority == 'debug':
        notifier.debug(context, 'compute.instance.log', info)


def verify_out_of_band_migration(context,
                                 instance,
                                 uuid,
                                 uuid_in_db,
                                 host_in_db,
                                 host_name,
                                 vm_info,
                                 node):
    # Verify if an out of band migration has moved an instance to current host.
    # Spawn a thread to avoid blocking main function.
    greenthread.spawn(_verify_out_of_band_migration,
                      context,
                      instance,
                      uuid,
                      uuid_in_db,
                      host_in_db,
                      host_name,
                      vm_info,
                      node)


def _verify_out_of_band_migration(context,
                                  instance,
                                  uuid,
                                  uuid_in_db,
                                  host_in_db,
                                  host_name,
                                  vm_info,
                                  node):
    """
    Verify if an out of band migration has moved an instance to current host

    :param context: Security context
    :param instance: The instance that was migrating
    :param uuid: uuid of the instance
    :param uuid_in_db: uuid of the instance in database
    :param host_name: Current host name
    :param vm_info: Result of driver.get_info()
    :param node: Current node name
    """

    state_in_db = instance.vm_state
    # If LPAR appears on different host, check to see
    # if it is migrating.
    # Check task_state, skip the rest if the instance
    # is deploying, deleting or migrating.
    instance_task_state = instance.task_state
    instance_vm_state = instance.vm_state
    instance_name = instance.display_name
    if (instance_task_state in (task_states.SCHEDULING,
                                task_states.
                                BLOCK_DEVICE_MAPPING,
                                task_states.NETWORKING,
                                task_states.SPAWNING,
                                task_states.MIGRATING,
                                task_states.DELETING)
            or instance_vm_state in
            (vm_states.BUILDING)):
        LOG.info(_('Virtual machine %(name)s is in '
                   'task state: %(task_state)s and '
                   'virtual machine state: '
                   '%(vm_state)s. The operation to '
                   'determine whether a migration was '
                   'performed outside of PowerVC will '
                   'be skipped. '
                   'UUID on host: %(uuid_on_host)s. '
                   'UUID in DB: %(uuid_in_db)s. '
                   'Source host: %(src_host)s. Target '
                   'host: %(tgt_host)s')
                 % {'name': instance_name,
                    'task_state': instance_task_state,
                    'vm_state': instance_vm_state,
                    'uuid_on_host': uuid,
                    'uuid_in_db': uuid_in_db,
                    'src_host': host_in_db,
                    'tgt_host': host_name})
        return
    # Check 'instance_migrating' in case migration is
    # out of band
    is_migrating = vm_info.get('instance_migrating')
    if is_migrating:
        LOG.debug('Instance %s is migrating. '
                  'Skipped.'
                  % instance_name)
        return
    # If instance is still on source host (host_in_db), skip.
    if _is_instance_on_source_host(context, host_in_db, uuid, instance_name):
        return
    # Sync up host
    instance.host = host_name
    # Sync up node
    instance.node = node
    # Check the instance state in DB, compare it with
    # instance state known to hypervisor. Update DB if
    # needed
    vm_power_state = vm_info['state']
    if state_in_db == vm_states.ERROR:
        if vm_power_state == power_state.RUNNING:
            instance.vm_state = vm_states.ACTIVE
        elif vm_power_state == power_state.SHUTDOWN:
            instance.vm_state = vm_states.STOPPED
    elif state_in_db == vm_states.STOPPED:
        if vm_power_state == power_state.RUNNING:
            instance.vm_state = vm_states.ACTIVE
    inst_output = instance.obj_to_primitive()
    LOG.info(_('Virtual machine information: %s')
             % inst_output)
    instance.save()
    LOG.info(_('Virtual machine %(name)s '
               'migrated from %(src)s to %(tgt)s. '
               'UUID on host: %(uuid)s. UUID in DB: '
               '%(uuid_in_db)s') %
             {'name': instance_name, 'src': host_in_db,
              'tgt': host_name, 'uuid': uuid,
              'uuid_in_db': uuid_in_db},
             instance=instance)
    # Send event notification
    info = {'msg': _('Virtual machine {instance_name} '
            'has been migrated from host '
            '{source_host_name} to '
            'host {destination_host_name}.'),
            'instance_id': instance.uuid,
            'instance_name': instance_name,
            'source_host_name': host_in_db,
            'destination_host_name': host_name}
    notifier = rpc.get_notifier(service='compute',
                                host=host_name)
    notifier.info(context,
                  'compute.instance.log',
                  info)


def _is_instance_on_source_host(context,
                                source_host,
                                instance_uuid,
                                instance_name):
    """
    While handling possible out of band migration, we send a message to
    source host, asking if instance is still on the source host.
    """
    # Cooperative yield
    greenthread.sleep(0)
    answer = api.PowerVCComputeRPCAPI().is_instance_on_host(context,
                                                            instance_uuid,
                                                            instance_name,
                                                            source_host)
    if answer:
        LOG.info(_('Virtual machine %(inst)s is being managed by remote host '
                   '%(host)s. This could indicate the virtual machine is on '
                   'two hosts simultaneously after migration '
                   'outside of PowerVC') %
                 {'inst': instance_name, 'host': source_host})
    else:
        LOG.info(_('Instance %(inst)s is not being managed by remote '
                   'host %(host)s.') %
                 {'inst': instance_name, 'host': source_host})
    return answer
