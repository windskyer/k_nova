#
# =================================================================
# =================================================================

from paxes_nova.common.threadgroup import GreenThreadGroup
from nova.openstack.common import log as logging
from paxes_nova import compute
from paxes_nova import _

LOG = logging.getLogger(__name__)


def get_host_ovs(context, host_list):
    """
    Make rpc call to retrieve ovs data from hosts.
    :param context: The HTTP request context.
    :param host_list: Lists of hosts to retrieve ovs data from
    :returns: List of doms, one for each host, contain ovs data
              and error information, if any, from the api call.
    """
    api = compute.HostAPI()
    return _rpc_call_compute_host(context, api.get_host_ovs, host_list)


def update_host_ovs(context, host, dom, force_flag, rollback):
    """
    Run update with dom and/or return warnings/errors
    :param context: The HTTP request context.
    :param host: host to run update on
    :param dom: dom to use for update
    :param force_flag: if false, do not update if
            warnings are found, if true, update regardless
            of warnings (errors cannot be overridden)
    :param rollback: Whether the operation should be rolled back before
                     completion, often to test the rollback mechanism.
    :returns: dom with any errors/warnings.
    """
    api = compute.HostAPI()
    return _rpc_call_compute_host(context,
                                  api.update_host_ovs,
                                  [host],
                                  dom,
                                  force_flag,
                                  rollback)


def _rpc_call_compute_host(context, rpc_function, host_list, *args):
    """
    Generic class to make fork/join style rpc calls to one or more
    compute hosts.
    :param context: The HTTP request context.
    :param rpc_function: The rpc function to call
    :param host_list: Lists of hosts to make rpc call to
    :returns: List of return data from rpc call, one entry for
              each host in host_list
    """
    thread_list = {}
    tg = GreenThreadGroup()

    for host in host_list:

        arg_list = []
        arg_list.append(host)
        for arg in args:
            arg_list.append(arg)

        try:
            thread_list[host] = \
                (tg.spawn(rpc_function,
                          context,
                          *arg_list))

            LOG.debug("%s initiated for host %s" %
                      (rpc_function.__name__, host))

        except Exception:
            LOG.exception(_("Exception with %(func)s rpc call thread for "
                            "host %(host)s" %
                            {'func': rpc_function.__name__,
                             'host': host}))

    # wait for all threads to return with data.
    return_data_list = []
    for key in thread_list.keys():
        return_data_list.append(thread_list[key].wait())

    LOG.debug("Returned data from %s call(s): %s" %
              (rpc_function.__name__, return_data_list))

    return return_data_list
