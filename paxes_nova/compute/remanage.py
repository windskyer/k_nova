#
# =================================================================
# =================================================================

import ConfigParser
from oslo.config import cfg
from nova import config
config.parse_args([])

import nova.context
from nova.conductor import rpcapi


def notify_unmanage(mgmt_ip):
    """Notifies the old Management System it is removing its Management"""
    ctxt = nova.context.get_admin_context()
    #If the Host isn't currently being managed, nothing to do
    if not _is_currently_managed(ctxt):
        return
    #Use the Conductor on the Old Management System to Remove the Host
    conductor_api = rpcapi.ConductorAPI()
    cctxt = conductor_api.client.prepare()
    cctxt.call(ctxt, 'notify_unmanage', host=cfg.CONF.host, mgmt_ip=mgmt_ip)


def _is_currently_managed(context):
    """Helper method to see verify the Host is currently managed"""
    parser = ConfigParser.RawConfigParser()
    parser.read('/etc/nova/nova.conf')
    #Check the qpid_hostname to determine if the Host is currently managed
    if parser.has_option('DEFAULT', 'qpid_hostname'):
        return len(parser.get('DEFAULT', 'qpid_hostname')) > 0
    return False
