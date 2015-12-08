# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# =================================================================
# =================================================================

"""
This RAS component provides two functions:
1. An extensiable unified message catalog interface
2. A trace function(flight recorder) that is served as
component trace with level control, a dynamic brkpoint
for debug.
This is the initial experiment to provide an enterprise
level RAS (eRAS) infrastructure for Openstack. In the long
run, it should be replaced by a Openstack frame level
component once it is available.

TODO(ajiang): This needs to be pulled out once the VFlex based
              RAS framework is available.
"""

import sys
import time

from oslo.config import cfg

from paxes_nova import _

from nova.openstack.common import log as logging

# define the trace level for logging
TRACE_INFO = 1  # trace the normal path
TRACE_ERROR = 2  # trace the error condition
TRACE_EXCEPTION = 3  # trace the exception condition
TRACE_DEBUG = 4  # debug trace and set a brkpoint at the trace point
TRACE_NONE = 5  # do not trace anything
TRACE_WARNING = 6  # trace the warning condition

LOG = logging.getLogger(__name__)


def vif_get_msg(msg_type, msg_id):
    """
    Return formated message string based on error/message type and id
    """

    # TODO(AJIANG): It is better to uniform those ERRID into
    # UNIXERRID.COMPONENT.SUBCOMPONENT.SEQUENCE form and manage it across the
    # board to standardize the error message. For example:
    # EINVAL_POWERVM_VIF_0001
    MSGIDS = {
        'error':
        {
            'VLAN_OUTOFRANGE':  # ERRID
            _('The value specified for the VLAN ID, %(vlanid)d, is not valid. '
              'Valid values are integers between 1 and 4094. Specify a valid '
              'VLAN ID and retry the operation.'),
            'SLOT_INVALIDNUMBER':  # ERRID
            _('The value specified for the virtual slot identifier, '
              '%(slotnum)d, is not valid. Valid values are positive integers. '
              'Specify a valid virtual slot identifier and retry the '
              'operation.'),
            'VSWITCH_NULL':  # ERRID
            _('A valid virtual switch name is not specified for the Virtual '
              'Ethernet Adapter. An example of a valid name is "ETHERNET0". '
              'Specify a valid virtual switch name and retry the operation.'),
            'ADDLVLANIDS_INVALID':  # ERRID
            _('The following VLANs that are specified on the addl_vlan_ids '
              'attribute of the virtual Ethernet adapter are not valid: '
              '%(addlvlans)s. Valid values are integers between 1 and 4094. '
              'Specify valid VLANs and retry the operation.'),
            'DEVNAME_INVALID':  # ERRID
            _('The value specified for the name of the device, %(devname)s, '
              'is not valid. Specify a valid device name and retry the '
              'operation.'),
            'OBJECT_INVALIDINST':  # ERRID
            _('The specified software object is not instance of %(classname)s.'
              ' Specify a valid object and retry the operation.'),
            'OBJECT_INVALIDATTR':  # ERRID
            _('The following attribute of object %(objname)s is not valid: '
              '%(attrs)s. Specify an object with valid attributes and retry '
              'the operation.'),
            'VLANID_CONFLICT':  # ERRID
            _('The VLAN ID %(vlanid)d conflicts with the VEA reserved '
              'untagged pvid %(pvid)d. Reconfiguration of this VEA is '
              'required and will now be attempted. If the reconfiguration '
              'fails, use the IVM or HMC console to reconfigure the VEA '
              'manually, then retry the operation.'),
            'VLANID_CTL_CHANNEL_CONFLICT':  # ERRID
            _('VLAN ID %(vlanid)d conflicts with the VEA reserved vlans of '
              'the SEA ctl channel. Reconfiguration of this VEA is required. '
              'Use the IVM or HMC console to reconfigure the VEA manually, '
              'then retry the operation.'),
            'VEA_8021QVLANFULL':  # ERRID
            _('The addl_vlan_ids list addl_vlan_ids=%(addlvlans)s for Veth '
              '%(vethname)s at slot %(slot)d is full. Specify a different '
              'adapter and retry the operation.'),
            'SEA_NO_VEA':  # ERRID
            _('A VEA is not configured for SEA %(seaname)s. The configuration '
              'of at least one VEA is required on the SEA. Define a primary '
              'VEA and retry the operation.'),
            'SEA_FULL':  # ERRID
            _('SEA %(seaname)s already has the maximum of 15 VEAs attached.  '
              'To add VLAN %(vid)d, remove another VLAN from the system and '
              'retry the operation.'),
            'SEA_VEACFG':  # ERRID
            _('VEA %(veaname)s is already configured on the pvid_adapter of '
              'SEA %(seaname)s. SEAs pvid_adapters may have only one '
              'configured VEA. Specify a different VEA or SEA and retry the '
              'operation.'),
            'SEA_FAILTOADDVEA':  # ERRID
            _('VEA %(veaname)s was not added to SEA %(seaname)s. There are '
              'various potential triggers for this error, so further '
              'investigation is necessary. See error log and look for error '
              'messages. Resolve the specified error and retry the '
              'operation.'),
            'SEA_VEANOTFOUND':  # ERRID
            _('VEA %(veaname)s is not configured on SEA %(seaname)s. Specify '
              'a different VEA or SEA and retry the operation.'),
            'NET_INVALIDCFG':  # ERRID
            _('An unsupported virtual Ethernet configuration was detected. '
              'There are various potential triggers for this error, so '
              'further investigation is necessary. See previous error logs '
              'and look for error messages. Resolve the specified error and '
              'retry the operation.'),
            'SEA_FAILTOPLUG':  # ERRID
            _('An error occurred with the plug call into SEA for VLAN '
              '%(vid)d. There are various potential triggers for this error, '
              'so further investigation is necessary. See previous error logs '
              'and look for error messages. Resolve the specified error and '
              'retry the operation.'),
            'SEA_VIDNOTFOUND':  # ERRID
            _('VLAN %(vid)d was not found because it is not bridged by '
              'PowerVM. If the VLAN was already removed, you can ignore this '
              'error. Otherwise, there are various potential triggers for '
              'this error, so further investigation is necessary. See '
              'previous error logs. Resolve the specified error and retry '
              'the operation.'),
            'SEA_FAILTOUNPLUG':  # ERRID
            _('An error occurred with the unplug call into SEA for '
              'VLAN %(vid)d. There are various potential triggers for this '
              'error, so further investigation is necessary. See previous '
              'error logs. Resolve the specified error and retry the '
              'operation.'),
            'SEA_INVALIDSTATE':  # ERRID
            _('An invalid Shared Ethernet Adapter object or configuration was '
              'specified. The likely trigger for this error is that the SEA '
              'is in a state other than available. Change the SEA state to '
              'available and retry the operation.'),
            'SEA_OUTOFVIDPOOL':  # ERRID
            _('The new Virtual Ethernet Adapter was not created on the VIOS. '
              'There are no remaining available VLAN IDs to use. Remove '
              'another VLAN from the system and retry the operation.'),
            'VETH_NOTCFG':  # ERRID
            _('Configuration of the VETH at slot %(slotnum)d was damaged '
              'after a new slot was added. Use the IVM or HMC console to '
              'clean up the VETH manually, then retry the operation.'),
            'VETH_FAILTOREMOVE':  # ERRID
            _('The VETH at slot %(slotnum)d was not properly cleaned up after '
              'a failure. Use the IVM or HMC console to clean up the VETH '
              'manually, then retry the operation.'),
            'VIOS_CMDFAIL':  # ERRID
            _('The following VIOS command failed: %(cmd)s. Change the command '
              'and retry the operation.'),
            'VIOS_SEAINVALIDLOCS':  # ERRID
            _('SEA %(devname)s has no physloc. Configure the SEA, ensuring '
              'proper configuration of the physloc, and retry the operation.'),
            'VIOS_NOSEA':  # ERRID
            _('A Shared Ethernet Adapter (SEA) is not configured for VIOS. '
              'The configuration of at least one SEA is required on the VIOS. '
              'Configure a SEA on the VIOS and retry the operation.'),
            'VIOS_NOADAPTERS':  # ERRID
            _('An adapter is not configured for VIOS. At least one Shared '
              'Ethernet Adapter (SEA) must be configured on at least one VIOS '
              'to use this product. If the operation fails, configure a SEA '
              'on the VIOS and retry the operation.'),
            'VIOS_NORMC':  # ERRID
            _('RMC state for VIOS on lpar %(lpar)s: %(state)s'),
            'VIOS_RMC_DOWN':  # ERRID
            _('RMC state is inactive for the VIOS with lpar id %(lpar)s '
              ' on host %(host)s'),
            'VIOS_UNKNOWN':  # ERRID
            _('Unknown VIOS error. Retry the operation. If the error '
              'persists, contact your support representative.'),
            'VIOS_NONE':  # ERRID
            _('A VIOS was not found on host %s. A VIOS is needed to manage '
              'adapters. Create a VIOS on the host and retry the operation.'),
            'VIF_NOVLANID':  # ERRID
            _('The plug operation failed. A VLAN ID was not found for the '
              'VIF. Valid VLAN ID values are integers between 1 and 4094. '
              'Specify a valid VLAN ID and retry the operation.'),
            'TRUNK_PRI_OUTOFRANGE':  # ERRID
            _('The trunk priority %(trunkpri)d is not valid. Valid values are '
              'integers 0 or greater. Specify a valid trunk priority and '
              'retry the operation.'),
            'SEA_ERROR':  # ERRID
            _('The validation operation for the following SEA failed: '
              'devname: %(devname)s pvidvea: %(pvidvea)s. A valid primary VEA '
              'must be provided. Configure the SEA with a valid primary VEA '
              'and retry the operation.'),
            'VLAN_NOT_AVAIL':  # ERRID
            _('The VLAN %(vlan_id)s was not found in the available pool. '
              'Specify a different VLAN ID and retry the operation.'),
            'NETWORK_CONFLICT':  # ERRID
            _('Warning: %(networklist)s all share VLAN %(vlanid)s. Changing '
              'adapter %(sea)s on host %(hostid)s will result in changing the '
              'adapter for all the networks that apply to this host.'),
            'VIF_NO_VIOS':  # ERRID
            _('VIF operation will fail. A VIOS object was not available for '
              'VLAN %(vlanid)d.'),
            'HOST_RECONCILE_ERROR':  # ERRID
            _('An unknown error has occurred which has caused an issue with '
              'host network reconciliation. There are various potential '
              'triggers for this error, so further investigation is '
              'necessary. See previous error logs. Resolve the specified '
              'error and retry the operation.'),
            'PARAM_ERROR_BOTH':  # ERRID
            _('The return_only_id_list and network_id parameters must both be '
              'specified.  Provide both or neither and retry.'),
            'PARAM_ERROR_ONE':  # ERRID
            _('The return_only_id_list and network_id parameters cannot both '
              'be specified.  Provide one or the other and retry.'),
            'PLACEMENT_PARAM_MISSING':  # ERRID
            _('The return_only_id_list parameter requires either a host or '
              'network_id.  Specify a host or network_id and retry.'),
            'PLACEMENT_PARAM_ERROR_BOTH':  # ERRID
            _('The host and network_id parameters cannot both be specified.  '
              'Provide one or the other and retry.')
        },
        'info':
        {
            'MGMT_IP_FOUND': 'Management IP found on SEA at %(sea_devname)s',
            'SINGLE_SEA_FOUND': 'Single SEA found at %(sea_devname)s',
            'NON_MAIN_SEA': 'Found non-main SEA at %(sea_devname)s',
            'HOST_NOT_FOUND': 'Host not found in the DB for %(hostid)s',
            'NETWORK_ASSOCIATION_PUT': 'Network Association added/updated for'
            ' %(networkid)s and  %(hostid)s',
            'FOUND_VIOS': 'Found VIOS on id %(lpar_id)s, name %(lpar_name)s',
            'FOUND_SEAS': 'Found %(num_seas)d SEAs on VIOS %(vios_name)s',
            'NO_CLIENT_LPAR':
            'No client LPARs found on %s',
            'NO_CLIENT_ADAPTERS':
            'No client adapters found on lpar %(lpar)s, lpar_id %(lparid)d',
            'K2_GET_MS':
            'Querying K2 for ManagedSystems',
            'VEA_CREATED':
            'Created VEA %(devname)s at %(slot)s',
            'DEFAULT_ADAPTER': 'Found default adapter as %(default)s',
            'NON-MODIFIABLE': 'Found non-modifiable adapter as'
            '%(non-modifiable)s',
            'NON_MOD_ADPT': 'During non-mod-adpt with Pvid as %(pvid)s'
            'and VLAN ID as %(vlan)s',
            'NO_MOD_ADPT': 'No Modifiable Adapter found but default found'
            'for SEA %(sea)s and VLAN %(vlan)s',
            'PEER_SEA': 'Found Peer SEA with the name as %(peer_sea)s',
            'HOST_SEAS_RETURN': 'Returning the host-seas for vlan: %(vlan)s'
            'for host: %(host_name)s with value: %(list)s',
            'LOWEST_PVID': 'Found Lowest PVID Sea as %(lowest_sea)s',
            'GET_HOST_SEAS': 'GET request for Host-seas with parameters'
            'Hostname %(host)s, vlan id %(vlan)s, vswitch %(vswitch)s, and '
            'net_id %(net_id)s',
            'LPAR_DETAILS': 'Creating VIOS DOM object with lparid: %(lpar_id)s'
            'and LPAR Name: %(lpar_name)s',
            # API
            'NO_NET_FOUND': 'No network found for the host %(host)s',
            'NO_HOST': 'No Host found with the instance',
            'NET_VLAND_ID': 'Found VLAN ID: %(vlan)s during network deploy',
            'NO_NET_VLAN': 'No VLAN ID found during Network Deploy hence'
            'setting to default value of 1',
            'SEA_DEPLOY': 'No SEA assicated with network id %(net_id)s',
            'HOST_SEA_RESPONSE': 'Host-seas response during deploy: %(resp)s',
            'DEFAULT_ADPT_RESP': 'Default adapter during deploy: %(resp)s',
            'PRIMARY_SEA_DEPLOY': 'Primary SEA found during deploy: %(sea)s',
            'SEA_NOT_FOUND': 'No Valid SEA found on the system for net id:'
            '%(net_id)s',
            'NET_ID_SUPPLIED': 'Neutron Net id provided for host:%(host_name)s'
            'with vlan id:%(vlan_id)s and network id: %(net_id)s',
            'SEA_FOUND_NET': '%(sea)s SEA Found for a query with network UUID',
            'NON_MOD_SEA_NET': '%(sea)s is a Non-Mod SEA found with a query'
            'made with the network UUID',
            # IVM_DRIVER
            'VEA_DEV_NAME': 'The VEA device name during VEA on VIOS is:'
            '%(vea_dev)s with command output %(cmd)s',
            'CONNECTION_IVM': 'Obtaining connection information from conf'
            'params for driver_ivm',
            'CONNECTION_IVM_TOPO': 'Obtaining connection information from conf'
            'params for driver_ivm_topo',
            'LIST_VETH': 'Output List of Virtual Ethernet Adapters: %(cmd)s',
            'PVID_FOUND': 'Found VLAN matching the PVID %(pvid)s',
            'VLANID_IN_ADDL': 'Found VLAN id: %(vlanid)s in Additional VLANs',
            'VLAN_USERS': 'Num of users for VLAN id: %(vlanid)s is: %(num)s',
            'SLOT_NUM': 'The Free slot Number for VEA deploy is: %(snum)d',
            'MAX_SLOTS': 'The Maximum Available slots are: %(output)d',
            'PHYSC_LOC_LIST': 'The Physical Location List is: %(output)s',
            # Reconciliation codes
            'RECONCILE_HOST_START': 'Reconcile-%(host)s: Started',
            'RECONCILE_HOST_END': 'Reconcile-%(host)s: Completed',
            'RECONCILE_VIOS_START':
            'Reconcile-%(host)s-%(vios)s: Started on VIOS',
            'RECONCILE_VIOS_END':
            'Reconcile-%(host)s-%(vios)s: Completed on VIOS',
            'RECONCILE_VIOS_ADD_START':
            'Reconcile-%(host)s-%(vios)s-ADD: Started on VIOS',
            'RECONCILE_VIOS_ADD_END':
            'Reconcile-%(host)s-%(vios)s-ADD: Completed on VIOS',
            'RECONCILE_VIOS_DEL_START':
            'Reconcile-%(host)s-%(vios)s-DEL: Started on VIOS',
            'RECONCILE_VIOS_DEL_END':
            'Reconcile-%(host)s-%(vios)s-DEL: Completed on VIOS',
            'RECONCILE_SEA_START':
            'Reconcile-%(host)s-%(vios)s-SEA-%(adpt)s: Started on SEA',
            'RECONCILE_SEA_END':
            'Reconcile-%(host)s-%(vios)s-SEA-%(adpt)s: Completed on SEA',
            'RECONCILE_SEA_ADD_START':
            'Reconcile-%(host)s-%(vios)s-SEA-ADD-%(adpt)s: Started on SEA',
            'RECONCILE_SEA_ADD_END':
            'Reconcile-%(host)s-%(vios)s-SEA-ADD-%(adpt)s: Completed on SEA',
            'RECONCILE_SEA_DEL_START':
            'Reconcile-%(host)s-%(vios)s-SEA-DEL-%(adpt)s: Started on SEA',
            'RECONCILE_SEA_DEL_END':
            'Reconcile-%(host)s-%(vios)s-SEA-DEL-%(adpt)s: Completed on SEA',
            'RECONCILE_VEA_START':
            'Reconcile-%(host)s-%(vios)s-VEA-%(adpt)s: Started on VEA',
            'RECONCILE_VEA_END':
            'Reconcile-%(host)s-%(vios)s-VEA-%(adpt)s: Completed on VEA',
            'RECONCILE_VEA_ADD_START':
            'Reconcile-%(host)s-%(vios)s-VEA-ADD-%(adpt)s: Started on VEA',
            'RECONCILE_VEA_ADD_END':
            'Reconcile-%(host)s-%(vios)s-VEA-ADD-%(adpt)s: Completed on VEA',
            'RECONCILE_VEA_DEL_START':
            'Reconcile-%(host)s-%(vios)s-VEA-DEL-%(adpt)s: Started on VEA',
            'RECONCILE_VEA_DEL_END':
            'Reconcile-%(host)s-%(vios)s-VEA-DEL-%(adpt)s: Completed on VEA',
            'RECONCILE_SAVE_START':
            'Reconcile-%(host)s-%(vios)s: Started save operation',
            'RECONCILE_SAVE_END':
            'Reconcile-%(host)s-%(vios)s: Completed save operation',

            # Topo code
            'VIOS_NOVEA':  # Info ID
            'VIOS has no VEA configured',

            # VM Import
            'IMPORT_NON_VIRTUAL_PORT':
            _('This virtual machine is not a candidate for importation '
              'because it has an adapter that is not virtual.  You can import '
              'only virtual machines that have pure virtual adapters. To '
              'import this virtual machine into PowerVC, first remove all '
              'physical adapters from the virtual server.'),
            'IMPORT_QBG_PORT':
            _('This virtual machine is not a candidate for importation '
              'because it has an 802.1Qbg enabled adapter.  802.1Qbg enabled '
              'adapters are not currently enabled for importation. To import '
              'this virtual machine into PowerVC, first remove all 802.1Qbg '
              'enabled adapters.'),
            'IMPORT_ADDL_VLANS':
            _('This virtual machine is not a candidate for importation '
              'because it has a virtual Ethernet adapter with \'Additional '
              'VLANs\' configured.  Virtual Ethernet adapters with additional '
              'VLANs are not currently enabled for importation. To import '
              'this virtual machine into PowerVC, first remove the '
              '\'Additional VLANs\'.'),
            'IMPORT_ORPHAN_VLANS':
            _('This virtual machine is not a candidate for importation '
              'because it has a virtual Ethernet adapter with a primary VLAN '
              'ID that is not bridged by a shared Ethernet adapter. Virtual '
              'Ethernet adapters with a primary VLAN ID that is not bridged '
              'by a Shared Ethernet Adapter are not currently enabled for '
              'importation. To import this virtual machine into PowerVC, '
              'first remove the virtual Ethernet adapter that is not bridged '
              'by a shared Ethernet adapter, or bridge the virtual Ethernet '
              'adapter.')
        }
    }

    try:
        catlog = MSGIDS[msg_type]
    except KeyError:
        return _('vif_get_msg(), Undefined message '
                 'type %(msg_type)') % locals()

    try:
        msg = catlog[msg_id]
    except KeyError:
        return _('vif_get_msg(), msg_type: %(msg_type)s, '
                 'undefined msg_id: %(msg_id)s') % locals()

    return msg


def msg(msg_type, msg_id):
    """
    Shorter name pass-through to vif_get_msg to help live within 80 char line
    lengths.

    :param msg_type: Whether this is an error or info message
    :param msg_id: The ID to look up in this file
    """
    return vif_get_msg(msg_type, msg_id)


def function_tracepoint(logger, func_name, trace_level, trcbuf='',
                        start_timebase=-1):
    """
    This is the RAS function for VIF driver to generate function
    trace with timestamp, function name and trace messages.

    It also print out the time elapsed since the last timestamp, which
    is useful to trace function timing.

    It can also trigger bugger on the brkpoint condition.

    The trace level by default is high enough to avoid any INFO level
    tracing during runtime and only log trace if it error or above. This
    ensures the good path performance

    If the trace level is set to TRACE_DEBUG, not only the trace will be
    logged, the brkpoint will be set as well. Use it carefully.

    :param logger: logger
    :param func_name: caller function name to log
    :param trace_level: trace verbose level
    :param trcbuf: trace details buffer
    :param start_timebase: start timebase if need to log the time delta
    """

    if trace_level == TRACE_DEBUG:
        logfunc = logger.debug
    elif trace_level == TRACE_ERROR:
        logfunc = logger.error
    elif trace_level == TRACE_INFO:
        logfunc = logger.info
    elif trace_level == TRACE_EXCEPTION:
        logfunc = logger.exception
    elif trace_level == TRACE_WARNING:
        logfunc = logger.warning
    else:
        logfunc = None

    # if the logger is not specified just use the stdout for trace output
    # Needs to find out the signature of "logger not found"
    if not logfunc:
        def local_print(strbuf):
            print(strbuf)
        logfunc = local_print

    if start_timebase >= 0:
        # timing tracing is alway under debug
        now = time.time()
        logfunc(_('start timestamp: %(start)f, End timestamp: %(end)f,'
                  'time delta(in seconds) = %(delta)f') %
                {'start': start_timebase,
                 'end': now,
                 'delta': now - start_timebase})

    line_ = sys._getframe(1).f_lineno
    code_ = sys._getframe(1).f_code.co_name
    logfunc(_('%(timestamp)f\t: module:%(mod_name)s:'
              '%(line)s'
              ':%(code)s'
              ':(L:%(level)s)'
              '\t\t%(trcbuf)s') %
            {'timestamp': time.time(),
             'mod_name': func_name,
             'line': line_,
             'code': code_,
             'level': trace_level,
             'trcbuf': trcbuf})


def trace(logger, func_name, trace_level, trcbuf='', start_timebase=-1):
    """
    Shorter name pass-through to function_tracepoint to help live
    within 80 char line lengths.
    """
    return function_tracepoint(logger, func_name, trace_level, trcbuf,
                               start_timebase)


def dump_sys_data(pvm_op):
    """
    Will provide a dump of the system level data to the log.

    :param pvm_op: It is the PowerVMOperator._operator initialized
                   by __init__() of PowerVMOperator.
    """
    LOG.debug("System Network Data")
    LOG.debug("LPAR Virtual IO Configuration")
    __run_and_log_cmd(pvm_op,
                      "lshwres -r virtualio --rsubtype eth --level lpar")

    LOG.debug("VIOS TCP/IP Configuration")
    __run_and_log_cmd(pvm_op, "lstcpip")


def __run_and_log_cmd(pvm_op, cmd):
    """
    Will run the command against the pvm_op and log its results to the logger.

    :param pvm_op: It is the PowerVMOperator._operator initialized
                   by __init__() of PowerVMOperator.
    :param cmd: The command to run against the endpoint
    """
    LOG.debug(cmd)
    results = pvm_op.run_command(cmd)
    for result in results:
        LOG.debug(result)
