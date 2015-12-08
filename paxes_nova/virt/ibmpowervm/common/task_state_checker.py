# vim: tabstop=4 shiftwidth=4 softtabstop=4

# =================================================================
# =================================================================

from paxes_nova import logcall
import time
import exception
from eventlet import greenthread

from nova.compute import vm_states

from oslo.config import cfg
CONF = cfg.CONF
from nova import conductor
from nova.openstack.common import log as logging
from paxes_nova import _

LOG = logging.getLogger(__name__)
ACTIVATING = 'activating'
ibmpowervm_opts = [
    cfg.IntOpt('ibmpowervm_rmc_active_timeout',
               default=1200,
               help='Timeout in seconds for rmc to be active after deploy.')
]
CONF.register_opts(ibmpowervm_opts)


@logcall
def check_task_state(context, instance, dlpar_connectivity, get_ref_code):
    """Set task state to activating while waiting for RMC to be ready
    :param context: nova context for this operation
    :params instance:
        nova.db.sqlalchemy.models.Instance object
        instance object that was deployed.
    """
    def reset_task_state():
        try:
            conductor.API().instance_update(context,
                                            instance['uuid'],
                                            task_state=None,
                                            expected_task_state=ACTIVATING)
        except Exception:
            LOG.warning(_('Unable to reset task state to None for instance %s')
                        % instance['name'])

    inst_name = instance['name']

    start_time = time.time()
    timeout = CONF.ibmpowervm_rmc_active_timeout
    dlpar, rmc_state = dlpar_connectivity(inst_name)
    # poll instance for reference code
    ref_code_ok = False
    ref_code_fail = False
    wait_inc = 5
    while not (ref_code_ok or ref_code_fail):
        greenthread.sleep(wait_inc)
        curr_time = time.time()
        # wait up to (timeout) seconds for shutdown
        if (curr_time - start_time) > timeout:
            LOG.debug("Timed out waiting for ok or failed reference code"
                      "for %s." % inst_name)
            reset_task_state()
            return
        refcode = get_ref_code(inst_name)
        if refcode is not None:
            if refcode == 'Linux ppc64' or refcode == "":
                ref_code_ok = True
            if refcode == 'No license':
                ref_code_fail = True

    if ref_code_fail:
        # set to error state
        no_license_args = {'instance_name': inst_name}
        err_msg = exception.IBMPowerVMNoLicenseSRC(
            **no_license_args).message
        values = {'instance_uuid': instance['uuid'],
                  'code': 500,
                  'message': unicode(err_msg),
                  'details': unicode(err_msg),
                  'host': CONF.host}
        conductor.API().instance_fault_create(context, values)
        conductor.API().instance_update(context,
                                        instance['uuid'],
                                        vm_state=vm_states.ERROR,
                                        task_state=None)
        return

    # poll instance until rmc is up
    wait_inc = 90  # seconds to wait between status polling
    while rmc_state is None or rmc_state != 'active' or dlpar is False:
        curr_time = time.time()
        # wait up to (timeout) seconds for shutdown
        if (curr_time - start_time) > timeout:
            LOG.debug("Timed out waiting for RMC to be active  "
                      "for %s." % inst_name)
            reset_task_state()
            return

        greenthread.sleep(wait_inc)
        instance_existance = (
            conductor.API().instance_get_by_uuid(context, instance['uuid']))
        if instance_existance:
            dlpar, rmc_state = dlpar_connectivity(inst_name)
        else:
            break

    else:
        reset_task_state()
