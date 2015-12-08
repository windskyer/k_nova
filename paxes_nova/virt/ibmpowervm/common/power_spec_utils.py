# vim: tabstop=4 shiftwidth=4 softtabstop=4

# =================================================================
# =================================================================

"""Utility class for updating the PowerSpec Data in the DB for IVM and HMC"""

from nova import notifications as notifyutil
from nova import rpc
from nova.compute import power_state
from nova.compute import task_states
from nova.compute import vm_states
from nova.openstack.common import log as logging

from paxes_nova import _

LOG = logging.getLogger(__name__)


def update_instance_power_specs(context, virtapi, instance, power_specs):
    """Helper Method to update the Power-specific Attributes in the Database"""
    old_power_spec = instance.get('power_specs')
    old_power_spec = old_power_spec if not None else {}
    # Figure out if there is any Pending Action for this Instance
    _handle_pending_action(context, instance, power_specs)

    # defect 21441
    # move the type conversion up, so we can avoid unnecessary update
    # defect 17007
    # convert uncapped value from string to boolean
    uncapped = power_specs.get('uncapped')
    if uncapped is not None:
        if isinstance(uncapped, str):
            if uncapped.lower() == 'true':
                power_specs['uncapped'] = True
            else:
                power_specs['uncapped'] = False

    # Determine if any of the PowerSpecs actually changed
    for key in power_specs.keys():
        if _compare_spec_value(power_specs.get(key), old_power_spec.get(key)):
            power_specs.pop(key, None)
    # If something did really change, then update the PowerSpecs in the DB
    # We also skip the update if the Instance is currently resizing/changing
    if (len(power_specs) > 0 and not _is_instance_resizing(instance)
            and not _is_instance_spawning(instance)):
        updates = dict(power_specs=power_specs)
        # Temporarily duplicate the info on the System Meta-data
        _duplicate_system_metadata(updates)
        oob_changes = _handle_out_of_band_updates(context, instance, updates)
        # Update the Instance in the DB through the Instance DOM Model
        instance.update(updates)
        instance.save()
        # Send a Notification to the Scheduler with the Instance Updates
        _notify_instance_update_spec(context, instance, updates)
        # If there were Out-of-band Changes, then send a Notification
        if oob_changes:
            _notify_out_of_band_change(context, instance)


def _notify_instance_update_spec(context, instance, updates):
    """Internal Helper Method to send notifications if allocations changed"""
    updates = updates.copy()
    # Parse out the Old and New Meta-Data and PowerSpecs to merge them together
    new_pwrspecs = updates.pop('power_specs', {})
    new_metadata = updates.pop('system_metadata', {})
    old_metadata = instance.get('system_metadata', {}).copy()
    old_pwrspecs = instance.get('power_specs', {}).copy()
    # Merge the Old and New MetaData and PowerSpecs together for the Event
    old_pwrspecs.update(new_pwrspecs)
    old_metadata.update(new_metadata)
    # Use the Utility to Build the base Instance, and add MetaData/PowerSpecs
    info = notifyutil.info_from_instance(context, instance, None, None)
    info.update(dict(system_metadata=old_metadata, power_specs=old_pwrspecs))
    info.update(updates)
    # Now we can send the Notification to the Scheduler that the Specs changed
    notifier = rpc.get_notifier(service='compute', host=instance['host'])
    notifier.info(context, 'compute.instance.update.spec', info)


def _handle_out_of_band_updates(context, instance, updates):
    """Helper method to Update and Notify when Out-of-Band Changes"""
    db_vcpus = instance.get('vcpus')
    db_memmb = instance.get('memory_mb')
    db_procs = instance.get('power_specs', {}).get('proc_units')
    drv_vcpus = updates.get('power_specs', {}).get('vcpus')
    drv_memmb = updates.get('power_specs', {}).get('memory_mb')
    drv_procs = updates.get('power_specs', {}).get('proc_units')
    # If the Driver gave a different value than the DB, update the DB to match
    if db_vcpus and drv_vcpus and not _compare_spec_value(db_vcpus, drv_vcpus):
        updates['vcpus'] = drv_vcpus
    if db_memmb and drv_memmb and not _compare_spec_value(db_memmb, drv_memmb):
        updates['memory_mb'] = drv_memmb
    return ('vcpus' in updates or 'memory_mb' in updates or (db_procs and
            drv_procs and not _compare_spec_value(db_procs, drv_procs)))


def _handle_pending_action(context, instance, power_specs):
    """Helper method to Update the Pending Action flag for Awaiting Reboot"""
    vm_state = instance.get('vm_state', '')
    pwr_state = power_specs.pop('power_state', 0)
    active_states = [vm_states.ACTIVE, vm_states.RESIZED]
    pending_action = power_specs.pop('pending_action', None)
    attribs = ['memory_mb', 'min_memory_mb', 'max_memory_mb',
               'proc_units', 'min_proc_units', 'max_proc_units',
               'vcpus', 'min_vcpus', 'max_vcpus']
    power_specs['pending_action'] = None
    # If the VM State is in-progress we want to wait until it finishes,
    # also if the VM is Stopped then obviously a reboot isn't needed
    if vm_state not in active_states or pwr_state != power_state.RUNNING:
        return
    # If we were provided a Pending Action already, don't need to check it
    if pending_action is not None:
        power_specs['pending_action'] = pending_action
        return
    # Compare the Pending/Current Values to see if a Reboot is still needed
    for attr in attribs:
        cur_attr = power_specs.get(attr)
        run_attr = power_specs.pop('run_' + attr, None)
        pend_attr = power_specs.pop('pending_' + attr, None)
        #Compare the Current and Pending Value to see if they are different
        if cur_attr and pend_attr and cur_attr != pend_attr:
            power_specs['pending_action'] = 'awaiting_restart'
        #If this is a min/max, there is only pending, no running value
        if attr.startswith('min_') or attr.startswith('max_'):
            continue
        #If Running Value is 0, the VM is stopped so exclude
        if str(run_attr) == '0' or str(run_attr) == '0.00':
            continue
        #Compare the Current and Running Value to see if they are different
        if cur_attr and run_attr and cur_attr != run_attr:
            power_specs['pending_action'] = 'awaiting_restart'


def _notify_out_of_band_change(context, instance):
    """Helper method to Notify when Out-of-Band Changes occur"""
    # First write out a statement to the log file saying it changed
    LOG.info(_('Resource allocation of instance %(instance_name)s '
               'on host %(host_name)s has been modified.') %
             {'instance_name': instance['display_name'],
              'host_name': instance['host']})
    # Construct a translatable message to log and send in the notification
    msg = _('Resource allocation of instance {instance_name} '
            'on host {host_name} has been modified.')
    # Construct the full payload for the message that is being sent
    info = {'msg': msg, 'instance_id': instance['uuid']}
    info['instance_name'] = instance['display_name']
    info['host_name'] = instance['host']
    # Send the notification that the Allocations changed Out-of-Band
    notifier = rpc.get_notifier(service='compute', host=instance['host'])
    notifier.info(context, 'compute.instance.log', info)


def _is_instance_resizing(instance):
    """Helper method to see if the Instance is currently being resized"""
    resize_states = [task_states.RESIZE_PREP, task_states.RESIZE_MIGRATING,
                     task_states.RESIZE_MIGRATED, task_states.RESIZE_FINISH]
    # If the VM State is Resized then it has just finished resizing
    if instance['vm_state'] == vm_states.RESIZED:
        return True
    # Otherwise see if the Task State is one of the Resizing Tasks
    return instance['task_state'] in resize_states


def _is_instance_spawning(instance):
    """Helper method to see if the Instance is currently being deployed"""
    spawn_states = [task_states.SCHEDULING,
                    task_states.BLOCK_DEVICE_MAPPING,
                    task_states.NETWORKING,
                    task_states.SPAWNING]
    # If the VM State is BUILDING then it's in the middle of deploying
    if instance['vm_state'] == vm_states.BUILDING:
        return True
    # Otherwise see if the Task State is one of the spawning Tasks
    return instance['task_state'] in spawn_states


def _compare_spec_value(oldval, newval):
    """Helper method to compare if the old and new spec value are the same"""
    # If both are None, then obviously they are the same value
    if oldval is None and newval is None:
        return True
    # We checked if both are None, so if either is None they aren't equal
    if oldval is None or newval is None:
        return False
    # We need to do something special if it is a float specified
    if isinstance(oldval, float):
        oldval, newval = ("%0.2f" % oldval, "%0.2f" % float(newval))
    # Otherwise we can do the comparison as string values
    return str(oldval) == str(newval)


def _duplicate_system_metadata(updates):
    """Temporary Helper method to duplicate the System Meta-data"""
    # TODO: Remove this when Scheduler/REST API's aren't using Meta-data
    system_metadata = dict(updates_only=True)
    power_specs = updates.get('power_specs', {})
    spec_to_metadata_map = {
        'vcpus': 'cpus', 'min_vcpus': 'min_cpus', 'max_vcpus': 'max_cpus',
        'proc_units': 'vcpus', 'min_proc_units': 'min_vcpus',
        'max_proc_units': 'max_vcpus', 'memory_mb': 'memory_mb',
        'min_memory_mb': 'min_memory_mb', 'max_memory_mb': 'max_memory_mb',
        'current_compatibility_mode': 'current_compatibility_mode',
        'desired_compatibility_mode': 'desired_compatibility_mode',
        'rmc_state': 'rmc_state'}
    # Loop through the PowerSpecs, mapping to their equivalent Meta-data
    for key, val in power_specs.iteritems():
        if key in spec_to_metadata_map:
            system_metadata[spec_to_metadata_map[key]] = str(val)
    updates['system_metadata'] = system_metadata
