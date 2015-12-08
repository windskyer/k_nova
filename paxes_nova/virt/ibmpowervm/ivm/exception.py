#
#
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

from nova import exception
from paxes_nova.virt.ibmpowervm.common import exception as cmn_ex
from nova.openstack.common import processutils

from paxes_nova import _
from nova.openstack.common import log as logging

from paxes_nova import logcall

LOG = logging.getLogger(__name__)


class IBMPowerVMError(exception.NovaException):

    def __init__(self, message=None, **kwargs):
        message = _('An unknown error occurred')
        super(IBMPowerVMError, self).__init__(message)


class IBMPowerVMNoSpaceError(exception.NovaException):

    def __init__(self, message=None, **kwargs):
        message = _('No space is available')
        super(IBMPowerVMNoSpaceError, self).__init__(message)

class IBMPowerVMVolumeAttachFailed_v2(exception.NovaException):
    def __init__(self, volume_id, error):
        message = _("Could not attach volume %(volume_id)s "
                    "%(error)s") \
                    % {"volume_id": volume_id,
                    "error": error}
        super(IBMPowerVMVolumeAttachFailed_v2, self).__init__(message)

class IBMPowerVMVolumeAttachFailed(exception.NovaException):

    def __init__(self, instance, volume_id, error):
        message = _("Could not attach volume %(volume_id)s to virtual server "
                    "%(instance_name)s (%(instance_id)s); %(error)s") \
            % {"volume_id": volume_id,
               "instance_name": instance['name'],
               "instance_id": instance['id'],
               "error": error}
        super(IBMPowerVMVolumeAttachFailed, self).__init__(message)


class IBMPowerVMHDiskLookupFailed(exception.NovaException):

    def __init__(self, message=None, **kwargs):
        if not message:
            message = _("hdisk lookup for '%(volume_uid)s' failed") % kwargs
        super(IBMPowerVMHDiskLookupFailed, self).__init__(message)


class IBMPowerVMCommandFailed(exception.NovaException):

    """
    Inherit from ProcessExecutionError since most OpenStack codes
    expect ProcessExecutionError if error occurred via command
    execution.
    """

    @logcall
    def __init__(self, command=None, error='', **kwargs):
        self.command = command
        self.error = error
        self.kwargs = kwargs
        exit_code = 0
        stdout = ''
        if 'command' in self.kwargs:
            command = self.kwargs['command']
        if 'exit_code' in self.kwargs:
            exit_code = self.kwargs['exit_code']
        if 'error' in self.kwargs:
            error = self.kwargs['error']
        if 'stdout' in self.kwargs:
            stdout = self.kwargs['stdout']
        description = _("Unexpected exception while running IVM command.")
        LOG.error(description)
        LOG.error(_("Command: %(cmd)s") % {'cmd': command})
        LOG.error(_("Exit Code: %(exit_code)s") % {'exit_code': exit_code})
        LOG.error(_("Stdout: %(out)r") % {'out': stdout})
        LOG.error(_("Stderr: %(err)r") % {'err': self.error})
        message = _("Unexpected exception while running IVM command. "
                    "Review the log for additional details."
                    "Command: %s Exit Code: %s  Stdout: %s Stderr: %s" 
                    % (command, exit_code, stdout, self.error))
        super(IBMPowerVMCommandFailed, self).__init__(message)


class IBMPowerVMUnitsGreaterThanCPUs(cmn_ex.IBMPowerVMInsufficientResources):

    def __init__(self, message=None, **kwargs):
        if not message:
            message = _("Processing units requested (%(units_requested)s)"
                        " cannot be larger than CPUs requested "
                        "(%(cpus_requested)d) ") % kwargs
        super(IBMPowerVMUnitsGreaterThanCPUs, self).__init__(message)


class IBMPowerVMMediaLibraryNotAvailable(exception.NovaException):

    def __init__(self, message=None, **kwargs):
        if not message:
            message = _("Media library not available")
        super(IBMPowerVMMediaLibraryNotAvailable, self).__init__(message)


class IBMPowerVMISOFileNotFound(exception.NovaException):

    def __init__(self, message=None, **kwargs):
        if not message:
            message = _("Could not find ISO file '%(ISO_file)s'") % kwargs
        super(IBMPowerVMISOFileNotFound, self).__init__(message)


class IBMPowerVMInvalidLUNPathInfo(exception.NovaException):

    def __init__(self, message=None):
        super(IBMPowerVMInvalidLUNPathInfo, self).__init__(message)


class IBMPowerVMInvalidLUNPathInfoMultiple(IBMPowerVMInvalidLUNPathInfo):

    """
    Specialisation for the case where multiple disks that match the
    connectivity info are detected
    """

    def __init__(self, conn_info, outputs):
        message = _("More than one LUN in the path info. "
                    "conn_info %(conn_info)s, lspath "
                    "output: %(outputs)s") % locals()
        super(IBMPowerVMInvalidLUNPathInfoMultiple, self).__init__(message)


class IBMPowerVMInvalidLUNPathInfoNone(IBMPowerVMInvalidLUNPathInfo):

    """
    Specialisation for the case where none of the disks that match the
    connectivity info are detected
    """

    def __init__(self, conn_info, outputs):
        message = _("No disk has been found based on the "
                    "conn_info %(conn_info)s, lspath "
                    "output: %(outputs)s") % locals()
        super(IBMPowerVMInvalidLUNPathInfoNone, self).__init__(message)


class IBMPowerVMNullWWPNS(exception.NovaException):

    def __init__(self, message=None, **kwargs):
        if not message:
            message = _("The FC WWPN information is not available on PowerVM "
                        "system ")
        super(IBMPowerVMNullWWPNS, self).__init__(message)


class IBMPowerVMNetworkConfigFailed(exception.NovaException):

    def __init__(self, message=None, **kwargs):
        if not message:
            message = _("Unable to configure network for instance "
                        "'%(instance_name)s' because of the following error:"
                        " '%(error_msg)s'") % kwargs
        super(IBMPowerVMNetworkConfigFailed, self).__init__(message)


class IBMPowerVMNetworkUnconfigFailed(exception.NovaException):

    def __init__(self, message=None, **kwargs):
        if not message:
            message = _("Unable to remove network configuration for instance "
                        "'%(instance_name)s' because of the following error: "
                        "'%(error_msg)s'") % kwargs
        super(IBMPowerVMNetworkUnconfigFailed, self).__init__(message)


class IBMPowerVMNoEphemeralDisk(exception.NovaException):

    def __init__(self, message=None, **kwargs):
        if not message:
            message = _("No ephemeral disk found. An ephemeral disk is "
                        "required for deploys from ISO.") % kwargs
        super(IBMPowerVMNoEphemeralDisk, self).__init__(message)


class IBMPowerVMMigrateValidateFailed(exception.NovaException):

    def __init__(self, message=None, **kwargs):
        if not message:
            message = _("Migration validation failed on the endpoint "
                        "for instance '%(instance_name)s'. %(error)s") % kwargs
        super(IBMPowerVMMigrateValidateFailed, self).__init__(message)


class IBMPowerVMMigrateFailed(exception.NovaException):

    def __init__(self, message=None, **kwargs):
        if not message:
            message = _("Migration failed on the endpoint for instance "
                        "'%(instance_name)s'. %(error)s") % kwargs
        super(IBMPowerVMMigrateFailed, self).__init__(message)


class IBMPowerVMLiveMigrationFailed(exception.NovaException):

    def __init__(self, message=None, **kwargs):
        if not message:
            message = _("The live migration task failed. %(error)s") % kwargs
        super(IBMPowerVMLiveMigrationFailed, self).__init__(message)


class IBMPowerVMLPARInstanceDoesNotExist(exception.NovaException):

    def __init__(self, message=None, **kwargs):
        if not message:
            message = _("PowerVM LPAR '%(instance_name)s' does not "
                        "exist") % kwargs
        super(IBMPowerVMLPARInstanceDoesNotExist, self).__init__(message)


class IBMPowerVMSVCAddHostMapFailure(exception.NovaException):

    def __init__(self, message=None, **kwargs):
        if not message:
            message = _("SVC failed to map vdisk_UID = %(vdisk_uid)s to "
                        "initiator: %(initiator)s") % kwargs
        super(IBMPowerVMSVCAddHostMapFailure, self).__init__(message)


class IBMPowerVMInvalidParameters(exception.InvalidParameterValue):
    msg_fmt = _("%(err)s")


class IBMPowerVMStorageAllocFailed(exception.NovaException):

    def __init__(self, message=None, **kwargs):
        if not message:
            message = _("Unable to allocate storage for instance "
                        "'%(instance_name)s' because of the following error: "
                        " '%(error_msg)s'") % kwargs
        super(IBMPowerVMStorageAllocFailed, self).__init__(message)


class IBMPowerVMLPARCreationFailed(exception.NovaException):

    def __init__(self, message=None, **kwargs):
        if not message:
            message = _("Failed to create instance '%(instance_name)s' "
                        "because of the following error: "
                        "'%(error_msg)s'") % kwargs
        super(IBMPowerVMLPARCreationFailed, self).__init__(message)


class IBMPowerVMStagingAreaOutOfSpace(exception.NovaException):

    def __init__(self, message=None, **kwargs):
        if not message:
            message = _("Insufficient space in the host ISO temporary "
                        "storage area: '%(dir)s'. %(size)s MB is needed"
                        " but only %(free)s MB is available.") % kwargs
        super(IBMPowerVMStagingAreaOutOfSpace, self).__init__(message)


class IBMPowerVMDiskResizeBelowExisting(exception.NovaException):

    def __init__(self, message=None, **kwargs):
        if not message:
            message = _("Resize operation failed: Disk size cannot be "
                        "decreased. Disk size requested (%(req_size)i GB) is"
                        "  below the current disk size "
                        "(%(disk_size)i GB).") % kwargs
        super(IBMPowerVMDiskResizeBelowExisting, self).__init__(message)


class IBMPowerVMLPARInstanceNotFound(exception.NovaException):

    """
    Not to inherit from from PowerVMLPARInstanceNotFound because OpenStack
    has special handling for InstanceNotFound exception. It won't update
    instance fault with InstanceNotFound exception
    """

    def __init__(self, message=None, **kwargs):
        if not message:
            message = _("LPAR instance '%(instance_name)s' could not "
                        "be found") % kwargs
        super(IBMPowerVMLPARInstanceNotFound, self).__init__(message)


class IBMPowerVCDiscoverVolumesFailure(exception.NovaException):

    def __init__(self, message=None, **kwargs):
        if not message:
            message = _("Failed to discover attached volumes on host "
                        "initiator: %(initiator)s, vdisk_uid: "
                        "%(vdisk_uid)s") % kwargs
        super(IBMPowerVCDiscoverVolumesFailure, self).__init__(message)


class IBMPowerVMEphemeralDiskNotFound(exception.NovaException):

    def __init__(self, message=None, **kwargs):
        if not message:
            message = _("Unable to locate the ephemeral disk with "
                        "UID '%(uid)s'.") % kwargs
        super(IBMPowerVMEphemeralDiskNotFound, self).__init__(message)


class IBMPowerVMConnectionFailed(exception.NovaException):

    def __init__(self, message=None, **kwargs):
        if not message:
            message = _('Connection to PowerVM manager failed')
        super(IBMPowerVMConnectionFailed, self).__init__(message)


class IBMPowerVMFileTransferFailed(exception.NovaException):

    def __init__(self, message=None, **kwargs):
        if not message:
            message = _("File '%(file_path)s' transfer to PowerVM "
                        "manager failed") % kwargs
        super(IBMPowerVMFileTransferFailed, self).__init__(message)


class IBMPowerVMISOAttachFailed(exception.NovaException):

    def __init__(self, message=None, **kwargs):
        if not message:
            message = _("ISO could not be attached to the LPAR")
        super(IBMPowerVMISOAttachFailed, self).__init__(message)


class IBMPowerVMLPARAttributeNotFound(exception.NovaException):
    pass


class IBMPowerVMImageCreationFailed(exception.NovaException):

    def __init__(self, message=None, **kwargs):
        if not message:
            message = _("Image creation failed on PowerVM")
        super(IBMPowerVMImageCreationFailed, self).__init__(message)


class IBMPowerVMLPAROperationTimeout(exception.NovaException):

    def __init__(self, message=None, **kwargs):
        if not message:
            message = _("Operation '%(operation)s' on "
                        "LPAR '%(instance_name)s' timed out") % kwargs
        super(IBMPowerVMLPAROperationTimeout, self).__init__(message)


class PowerVMNoSpaceLeftOnVolumeGroup(exception.NovaException):

    def __init__(self, message=None, **kwargs):
        if not message:
            message = _("No space left on any volume group")
        super(PowerVMNoSpaceLeftOnVolumeGroup, self).__init__(message)


class IBMPowerVMInvalidProcCompat(cmn_ex.IBMPowerVMInsufficientResources):

    def __init__(self, message=None, **kwargs):
        if not message:
            message = _("'%(processor_compatibility)s' is not a valid option"
                        "  for processor compatibility. Valid options are "
                        "'%(valid_values)s' ") % kwargs
        super(IBMPowerVMInvalidProcCompat, self).__init__(message)


class IBMPowerVMOperationNotSupportedException(exception.NovaException):

    def __init__(self, message=None, **kwargs):
        if not message:
            message = _("Operation '%(operation)s' not supported for"
                        " '%(operator)s'") % kwargs
        self.safe = True
        super(IBMPowerVMOperationNotSupportedException, self).__init__(message)


class IBMPowerVMManagementServerOutOfSpace(exception.NovaException):

    def __init__(self, message=None, **kwargs):
        if not message:
            message = _("Insufficient space in '%(path)s'. %(size)s MB is"
                        " needed but only %(free)s MB is available.") % kwargs
        super(IBMPowerVMManagementServerOutOfSpace, self).__init__(message)


class IBMPowerVMInsufficientStagingMemory(exception.NovaException):

    def __init__(self, message=None, **kwargs):
        if not message:
            message = _("Insufficient space in the host staging area "
                        "Operation '%(operation)s' failed. "
                        "%(free)s MB is available.") % kwargs
        super(IBMPowerVMInsufficientStagingMemory, self).__init__(message)


class IBMPowerVMFTPTransferFailed(exception.NovaException):

    def __init__(self, message=None, **kwargs):
        if not message:
            message = _("FTP %(ftp_cmd)s from %(source_path)s to %(dest_path)s"
                        "failed because of the error: "
                        "'%(error_msg)s'") % kwargs
        super(IBMPowerVMFTPTransferFailed, self).__init__(message)


class IBMPowerVMSCPTransferFailed(exception.NovaException):

    def __init__(self, message=None, **kwargs):
        if not message:
            message = _("File transfer from %(source_path)s to %(dest_path)s"
                        "failed because of the error: "
                        "'%(error_msg)s'") % kwargs
        super(IBMPowerVMSCPTransferFailed, self).__init__(message)


class IBMPowerVMInvalidExtraSpec(exception.NovaException):

    def __init__(self, message=None, **kwargs):
        if not message:
            message = _("Invalid attribute name '%(key)s' was passed in as "
                        "part of the flavor extra specs for virtual machine "
                        "'%(instance_name)s'.") % kwargs
        super(IBMPowerVMInvalidExtraSpec, self).__init__(message)
