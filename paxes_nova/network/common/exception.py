# vim: tabstop=4 shiftwidth=4 softtabstop=4

# =================================================================
# =================================================================

from webob import exc
from nova import exception

from paxes_nova import _


class IBMPowerVMNetworkAPIUnsupported(exception.NovaException):
    msg_fmt = _('This API is not supported in this environment.')


class IBMPowerVMHostOvsCreateNotImpl(exception.NovaException):
    msg_fmt = _('The host-ovs POST method is not currently supported.')


class IBMPowerVMHostOvsDeleteNotImpl(exception.NovaException):
    msg_fmt = _('The host-ovs DELETE method is not currently supported.')


class IBMPowerVMNetworkAPIUnsupportedOutsidePowerVM(exc.HTTPNotImplemented):
    explanation = _('The API is not supported outside a PowerVM environment.')


class IBMPowerVMNetworkAPIUnsupportedOutsidePowerKVM(exc.HTTPNotImplemented):
    explanation = _('The API is not supported outside a PowerKVM environment.')


class IBMPowerKVMVswitchNotFound(exception.Invalid):
    msg_fmt = _("Virtual Switch not found: %(ovs_name)s.")


class IBMPowerKVMVswitchUpdateException(exception.NovaException):
    msg_fmt = _("An exception occurred updating the switch.  Check "
                "/var/log/nova/api.log for details.")
