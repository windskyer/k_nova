# vim: tabstop=4 shiftwidth=4 softtabstop=4

# =================================================================
# =================================================================

from nova import exception

from paxes_nova import _


class IBMPowerVCBootVolumeCloneFailure(exception.NovaException):
    msg_fmt = _("Failed to create boot volume for instance %(instname)s: "
                "%(instuuid)s, from image volume %(srcvolname)s: %(srcvolid)s."
                " Boot volume error: %(volmeta)s")


class IBMPowerVCVolumeError(exception.NovaException):
    msg_fmt = _("Volume %(volid)s is in failure state. Details: %(volume)s")


class IBMPowerVCVInvalidVolumeConnector(exception.NovaException):
    msg_fmt = _("Volume %(volid)s connector is invalid for instance "
                "%(inst)s")
