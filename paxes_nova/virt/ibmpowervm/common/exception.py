# vim: tabstop=4 shiftwidth=4 softtabstop=4

# =================================================================
# =================================================================

from nova import exception

from paxes_nova import _


class IBMPowerVMMigrationFailed(exception.NovaException):
    msg_fmt = _("The migration task failed. %(error)s")


class IBMPowerVMMigrationInProgress(exception.NovaException):
    msg_fmt = _("Migration of %(lpar)s is already in progress.")


class IBMPowerVMProgressUpdateError(exception.NovaException):
    msg_fmt = _("Unable to update progress for virtual machine '%(uuid)s'.")


class IBMPowerVMMediaRepOutOfSpace(exception.NovaException):
    msg_fmt = _("The media library is too small for the ISO image. The "
                "media library requires %(size)s MB of space but only "
                "%(free)s MB is available.")


class IBMPowerVMLPARIsRunningDuringCapture(exception.NovaException):
    msg_fmt = _("Virtual machine '%(instance_name)s' is running during "
                "capture. Virtual machines must be stopped before they "
                "can be captured.")


class IBMPowerVMLPARInstanceNotFound(exception.NovaException):
    msg_fmt = _("Unable to find virtual machine '%(instance_name)s'.")


class IBMPowerVMInsufficientResources(exception.NovaException):
    msg_fmt = _("Insufficient resources")


class IBMPowerVMMemoryBelowMin(IBMPowerVMInsufficientResources):
    msg_fmt = _("The requested memory (%(mem_requested)d MB) is below "
                "the minimum required value (%(mem_min)d MB)")


class IBMPowerVMDiskBelowActual(IBMPowerVMInsufficientResources):
    msg_fmt = _("The requested disk (%(new_disk_size)d GB) is lesser than "
                "the existing disk (%(disk_size)d GB)")


class IBMPowerVMCPUsBelowMin(IBMPowerVMInsufficientResources):
    msg_fmt = _("The requested CPUs (%(cpus_requested)d) are below "
                "the minimum required value (%(cpus_min)d) ")


class IBMPowerVMProcUnitsBelowMin(IBMPowerVMInsufficientResources):
    msg_fmt = _("The requested processing units (%(units_requested)s) are "
                "below the minimum required value (%(units_min)s) ")


class IBMPowerVMInsufficientFreeMemory(IBMPowerVMInsufficientResources):
    msg_fmt = _("Insufficient free memory: (%(mem_requested)d MB "
                "requested, %(mem_avail)d MB free)")


class IBMPowerVMInsufficientCPU(IBMPowerVMInsufficientResources):
    msg_fmt = _("Insufficient available CPUs: (%(cpus_requested)d "
                "requested, %(cpus_avail)d available)")


class IBMPowerVMInsufficientProcUnits(IBMPowerVMInsufficientResources):
    msg_fmt = _("Insufficient available processing units: "
                "(%(units_requested)s requested, "
                "%(units_avail)s available)")


class IBMPowerVMJobRequestFailed(exception.NovaException):
    msg_fmt = _("The '%(operation_name)s' operation failed. %(error)s")


class IBMPowerVMJobRequestTimedOut(IBMPowerVMJobRequestFailed):
    msg_fmt = _("The '%(operation_name)s' operation failed. "
                "Failed to complete the task in %(seconds)s seconds.")


class IBMPowerVMLPARInstanceCleanupFailed(exception.NovaException):
    msg_fmt = _("Virtual machine '%(instance_name)s' cleanup failed. "
                "Reason: %(reason)s")


class IBMPowerVMInstanceTerminationFailure(exception.
                                           InstanceTerminationFailure):
    msg_fmt = _("Failed to delete virtual machine. %(reason)s")


class IBMPowerVMNoInstanceBootDeviceDefined(exception.NovaException):
    msg_fmt = _("The boot device name attribute is not defined for virtual "
                "machine %(instance_name)s with ID %(instance_uuid)s.")


class IBMPowerVMInvalidImageObject(exception.NovaException):
    msg_fmt = _("Unable to save the captured image meta data for virtual "
                "machine %(instance_name)s because the image object is "
                "invalid.")


class IBMPowerVMInvalidBootVolume(exception.NovaException):
    msg_fmt = _("Boot volume %(volume)s is invalid for virtual machine "
                "%(instance_name)s with ID %(instance_uuid)s.")


class IBMPowerVMBootVolumeCloneFailure(exception.NovaException):
    msg_fmt = _("Clone for boot volume %(image_volume)s failed for virtual "
                "machine %(instance_name)s with ID %(instance_uuid)s.")


class IBMPowerVCK2APIError(exception.NovaException):
    msg_fmt = _("An error occurred while working with objects returned "
                "from the K2 API.")


class IBMPowerVMNoOSForVM(exception.NovaException):
    msg_fmt = _("Virtual machine '%(instance_name)s' does not have an"
                " operating system identified.  The process for bringing"
                " the virtual machine under management by PowerVC must "
                "be completed.")


class IBMPowerVMUnsupportedOSForVM(exception.NovaException):
    msg_fmt = _("The operating system %(os_distro)s for virtual machine "
                "'%(instance_name)s' is not supported. Supported operating "
                "systems include the following: %(options)s")


class IBMPowerVMErrorExtendingVolume(exception.NovaException):
    msg_fmt = _("An error occurred when extending volume %(volume_id)s")


class IBMPowerVMNoLicenseSRC(exception.NovaException):

    def __init__(self, message=None, **kwargs):
        message = _("Unable to deploy virtual machine '%(instance_name)s' "
                    "because the host does not support the operating system"
                    " installed on this image") % kwargs
        super(IBMPowerVMNoLicenseSRC, self).__init__(message)
