#
#
# =================================================================
# =================================================================

from nova.objects import service as service_obj
import nova.openstack.common.importutils
from nova.compute import api as base_api
from paxes_nova import _

from functools import wraps
from oslo.config import cfg

from nova import context as ctx
from nova import rpc
from nova.objects import instance as instance_obj
from nova.openstack.common import excutils
from nova.openstack.common import log as logging

LOG = logging.getLogger(__name__)

CONF = cfg.CONF

EVENT_TYPE_ERROR = 'ERROR'
EVENT_TYPE_WARN = 'WARN'


def API(*args, **kwargs):
    importutils = nova.openstack.common.importutils
    class_name = 'paxes_nova.compute.api.PowerVCComputeAPI'
    return importutils.import_object(class_name, *args, **kwargs)


def HostAPI(*args, **kwargs):
    importutils = nova.openstack.common.importutils
    class_name = 'paxes_nova.compute.api.PowerVCHostAPI'
    return importutils.import_object(class_name, *args, **kwargs)


def monkey_patch_is_volume_backed_instance():
    """
    We need to monkey patch the nova.compute.api.API class'
    is_volume_backed_instance method to allow the PowerVM capture path to
    go through the compute manager and the virt-drivers for IVM/HMC.  The
    existing method in OpenStack drives capture of our PowerVM VMs down
    a volume backed / Cinder snapshot path that is not supported on our VMs.
    """
    base_api.API.powervc_mod_is_volume_backed_instance = \
        base_api.API.is_volume_backed_instance

    def powervc_mod_is_volume_backed_instance(self, context, instance,
                                              bdms=None):
        service = service_obj.Service.get_by_compute_host(context.elevated(),
                                                          instance['host'])

        if service and service['compute_node']:
            hypervisor_type = service['compute_node']['hypervisor_type']
        if hypervisor_type and hypervisor_type == 'powervm':
            base_api.LOG.debug("Instance %s is on a PowerVM host, "
                               "returning False" % instance['uuid'])
            return False
        return self.powervc_mod_is_volume_backed_instance(context,
                                                          instance, bdms)

    base_api.API.is_volume_backed_instance = \
        powervc_mod_is_volume_backed_instance

monkey_patch_is_volume_backed_instance()


def send_ui_notification(error_message, success_message,
                         error_event_type=EVENT_TYPE_ERROR,
                         migrate_op=False, log_exception=False):
    """
    Send a notification to the GUI. If the decorated method throws an
    exception, an error notification will be sent, else a successful
    info notification will be sent. lpar name and uuid will be pulled from
    the instance object or dict parameter passed to the decorated method.

    Note: message string parameters (error_message and success_message) must be
          translated (wrappered with _() ). Replacement text variables
          must be surrounded with brackets.

          Replacement variables must be from the following list:'
                        {instance_id}
                        {instance_name}
                        {host_name}
                        {volume_id}
                        {source_host_name}
                        {target_host_name}
                        {error}

          Example: Stop of virtual machine {instance_name} on host {host_name}
                   failed with exception: {error}

    :param error_message: message string for exception notifications
                          (see note above)
    :param success_message: message string for success notifications
                            (see note above)
    :param error_event_type: either ERROR (EVENT_TYPE_ERROR)
                             or WARN (EVENT_TYPE_WARN)
    :param migrate_op: set to true for migation operations. Tells the decorator
                       to look for the migrate data dict. Only use when you
                       have replacement variables for source and target host.
    :param log_exception: if true in and error case, the event message is
                          also logged (LOG.exception)
    """
    def func_parms(f):
        @wraps(f)
        def wrapper(*args, **kwds):

            instance_dict_keys = ['uuid', 'display_name',
                                  'vm_state', 'task_state']
            migrate_dict = ['migrate_data']
            migrate_dict_source = ['src_sys_name']
            migrate_dict_target = ['dest_sys_name']

            def check_keys(arg, inst_keys):
                for key in inst_keys:
                    if key not in arg:
                        return False
                # Found them all!
                return True

            def check_inst(arg):
                # check if this is a Nova Instance object
                if isinstance(arg, instance_obj.Instance):
                    return True
                # check for a dict with all the right keys to match instance
                if isinstance(arg, dict) and check_keys(arg,
                                                        instance_dict_keys):
                    return True
                return False

            def check_migrate_source(arg):
                # check for a dict and it has the source migration host
                if isinstance(arg, dict) and check_keys(arg,
                                                        migrate_dict_source):
                    return True
                return False

            def check_migrate_target(arg):
                # check for a dict and it has the target migration host
                if isinstance(arg, dict) and check_keys(arg,
                                                        migrate_dict_target):
                    return True
                return False

            def check_migrate_target_embedded(arg):
                # check for an embedded migrate dict
                if isinstance(arg, dict) and check_keys(arg,
                                                        migrate_dict):
                    if isinstance(arg, dict) and (
                            check_keys(arg['migrate_data'],
                                       migrate_dict_target)):
                        return True
                return False

            def _notify(exception_msg=None):
                payload = {}
                payload['msg'] = ''
                payload['instance_id'] = lpar_uuid
                payload['instance_name'] = lpar_name
                payload['host_name'] = CONF.host_display_name
                if volume_id is not None:
                    payload['volume_id'] = volume_id
                notifier = rpc.get_notifier(service='compute',
                                            host=CONF.host)
                try:
                    if migrate_op:
                        payload['source_host_name'] = source_host
                        payload['target_host_name'] = target_host
                    if exception_msg is not None:
                        if error_message is not None:
                            payload['msg'] = error_message
                            payload['error'] = exception_msg
                            if log_exception:
                                try:
                                    # Convert the brackets to replacement
                                    # variables
                                    log_exception_msg = error_message.replace(
                                        "{", "%(").replace("}", ")s")
                                    LOG.exception(log_exception_msg % payload)
                                except Exception as exc:
                                    LOG.debug('LOG.exception failed with '
                                              'exception: %s' % exc)
                            if error_event_type is EVENT_TYPE_ERROR:
                                notifier.error(context,
                                               'compute.instance.log',
                                               payload)
                            elif error_event_type is EVENT_TYPE_WARN:
                                notifier.warn(context,
                                              'compute.instance.log',
                                              payload)
                            else:
                                LOG.debug('Exception was handled, but the '
                                          'error_event_type parameter was not '
                                          'set to a supported value. No GUI '
                                          'notification sent.')
                        else:
                            LOG.debug('Operation failed, but no error_message '
                                      'was provided. No GUI notification '
                                      'sent.')
                    else:
                        if success_message is not None:
                            payload['msg'] = success_message
                            notifier.info(context,
                                          'compute.instance.log',
                                          payload)
                        else:
                            LOG.debug('Operation completed successfully, but '
                                      'no success_message was provided. No GUI'
                                      ' notification sent.')
                except Exception as exc:
                    LOG.debug('Notify failed with exception: %s' % exc)

            lpar_uuid = None
            lpar_name = None
            source_host = None
            target_host = None
            volume_id = None
            context = None
            for arg in args:
                if check_inst(arg):
                    lpar_uuid = arg['uuid']
                    lpar_name = arg['display_name']
                if migrate_op:
                    if check_migrate_source(arg):
                        source_host = arg['src_sys_name']
                    if check_migrate_target(arg):
                        target_host = arg['dest_sys_name']
                    elif check_migrate_target_embedded(arg):
                        migrate_data = arg['migrate_data']
                        target_host = migrate_data['dest_sys_name']
                # TODO: need to see what type of context object is passed
                # with the new osee build installed
                #if isinstance(arg, RpcContext):
                #    context = arg
                if (context is not None and
                    lpar_uuid is not None and
                        (not migrate_op or target_host is not None)):
                    break

            if 'volume_id' in kwds:
                volume_id = kwds['volume_id']
            if 'instance' in kwds:
                if check_inst(kwds['instance']):
                    if lpar_uuid is None:
                        lpar_uuid = kwds['instance']['uuid']
                    if lpar_name is None:
                        lpar_name = kwds['instance']['display_name']

            if lpar_uuid is None:
                LOG.debug('Did not find the Instance object in '
                          'the decorated method arguments')
                lpar_uuid = ''
                lpar_name = ''
            if migrate_op:
                if source_host is None:
                    source_host = ''
                if target_host is None:
                    LOG.debug('Did not find the migrate dict in '
                              'the decorated method arguments')
                    target_host = ''
            if not context:
                context = ctx.get_admin_context()
            try:
                r = f(*args, **kwds)
                # notify success
                _notify()
                return r
            except Exception as exc:
                with excutils.save_and_reraise_exception():
                    # notify failure
                    exception_msg = _('%s') % exc
                    if not exception_msg:
                        exception_msg = exc.__class__.__name__
                    _notify(exception_msg)

        return wrapper
    return func_parms


def ui_notifier(notify_message, instance, except_to_raise=None,
                error_event_type=EVENT_TYPE_ERROR,
                log_exception=False):

    error_msg = None
    success_msg = None
    if except_to_raise is not None:
        error_msg = notify_message
    else:
        success_msg = notify_message

    @send_ui_notification(error_msg, success_msg,
                          error_event_type=error_event_type,
                          log_exception=log_exception)
    def _ui_notifier(instance, except_to_raise=None):
        if except_to_raise is not None:
            raise except_to_raise

    _ui_notifier(instance, except_to_raise=except_to_raise)
