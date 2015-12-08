# vim: tabstop=4 shiftwidth=4 softtabstop=4

# =================================================================
# =================================================================
from nova.openstack.common import log as logging
from nova.compute import vm_states
from nova import rpc
from nova import context
from nova import conductor
from paxes_nova import _
#from nova.openstack.common.rpc.amqp import RpcContext

LOG = logging.getLogger(__name__)

EVENT_TYPE_ERROR = 'ERROR'
EVENT_TYPE_WARN = 'WARN'


def set_instance_error_state_and_notify(instance):
    """
    Set an instance to ERROR state and send out a notification
    """
    # set instance to error state. Instance
    # could be a dictionary when this method is called during
    # virtual machine delete process.
    instance['vm_state'] = vm_states.ERROR
    conductor.API().instance_update(
        context.get_admin_context(), instance['uuid'],
        vm_state=vm_states.ERROR,
        task_state=None)
    instance_name = instance['name']
    host_name = instance['host']
    LOG.warn(_('Unable to find virtual machine %(inst_name)s '
               'on host %(host)s. Set state to ERROR')
             % {'inst_name': instance_name,
                'host': host_name})
    # Send event notification
    note = {'event_type': 'compute.instance.log',
            'msg': _('Unable to find virtual machine {instance_name} on '
                     'host {host_name}. An operation might have been '
                     'performed on the virtual machine outside of PowerVC or'
                     ' the deploy of the virtual machine failed.'
                     'The virtual machine is now set to Error state in the '
                     'database.'),
            'instance_name': instance_name,
            'host_name': host_name}
    notifier = rpc.get_notifier(service='compute', host=host_name)
    notifier.warn(context.get_admin_context(), 'compute.instance.log',
                  note)
