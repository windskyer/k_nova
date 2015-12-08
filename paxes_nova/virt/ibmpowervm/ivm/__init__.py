# vim: tabstop=4 shiftwidth=4 softtabstop=4

# =================================================================
# =================================================================

from oslo.config import cfg

ibmpowervm_opts = [
    cfg.StrOpt('powervm_mgr_type',
               default='ivm',
               help='PowerVM manager type (ivm, hmc)'),
    cfg.StrOpt('powervm_mgr',
               default=None,
               help='PowerVM manager host or ip'),
    cfg.StrOpt('powervm_mgr_user',
               default=None,
               help='PowerVM manager user name'),
    cfg.StrOpt('powervm_mgr_passwd',
               default=None,
               help='PowerVM manager user password',
               secret=True),
    cfg.StrOpt('powervm_img_remote_path',
               default='/home/padmin',
               help='PowerVM image remote path where images will be moved.'
               ' Make sure this path can fit your biggest image in glance'),
    cfg.StrOpt('powervm_img_local_path',
               default='/tmp',
               help='Local directory to download glance images to.'
               ' Make sure this path can fit your biggest image in glance'),
    cfg.IntOpt('powervm_mgr_port',
               default=22,
               help='PowerVM manager port'),
    cfg.StrOpt('ibmpowervm_remote_debug',
               default='',
               help='Turns on the pydev remote debugger'),
    cfg.StrOpt('ibmpowervm_remote_debug_host',
               default='',
               help='Remote debug server'),
    cfg.IntOpt('ibmpowervm_remote_debug_port',
               default=5678,
               help='Remote debug server port'),
    cfg.StrOpt('ibmpowervm_known_hosts_path',
               default='/opt/ibm/powervc/data/known_hosts',
               help='Path to known hosts key file'),
    cfg.StrOpt('hypervisor_hostname',
               default='',
               help='hostname from hypervisor'),
    cfg.StrOpt('host_storage_type',
               default='san',  # supported value "san" or "local"
               help='The host storage type supported by IVM'),
    cfg.StrOpt('powervm_max_ssh_pool_size',
               default='5',
               help='The maximum ssh connection pool size for IVM')
]

CONF = cfg.CONF
CONF.register_opts(ibmpowervm_opts)
