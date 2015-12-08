# vim: tabstop=4 shiftwidth=4 softtabstop=4

# =================================================================
# =================================================================

# NOTE: notify message MUST follow these rules:
#
#       - Messages must be wrappered with _() for translation
#
#       - Replacement variables must be wrappered with brackets
#
#       - Replacement variables must be from the following list:'
#                        {instance_id}
#                        {instance_name}
#                        {host_name}
#                        {source_host_name}
#                        {target_host_name}
#                        {error}

from paxes_nova import _

PAUSE_SUCCESS = (_("Pause of virtual machine {instance_name} on host "
                   "{host_name} was successful."))

PAUSE_ERROR = (_("Pause of virtual machine {instance_name} on host "
                 "{host_name} failed with exception: {error}"))

SUSPEND_SUCCESS = (_("Suspend of virtual machine {instance_name} on host "
                   "{host_name} was successful."))

SUSPEND_ERROR = (_("Suspend of virtual machine {instance_name} on host "
                 "{host_name} failed with exception: {error}"))

RESUME_SUCCESS = (_("Resume of virtual machine {instance_name} on host "
                    "{host_name} was successful."))

RESUME_ERROR = (_("Resume of virtual machine {instance_name} on host "
                  "{host_name} failed with exception: {error}"))

DEPLOY_SUCCESS = (_("Deploy of virtual machine {instance_name} on host "
                    "{host_name} was successful."))

DEPLOY_ERROR = (_("Deploy of virtual machine {instance_name} on host "
                  "{host_name} failed with exception: {error}"))

START_SUCCESS = (_("Start of virtual machine {instance_name} on host "
                   "{host_name} was successful."))

START_ERROR = (_("Start of virtual machine {instance_name} on host "
                 "{host_name} failed with exception: {error}"))

STOP_SUCCESS = (_("Stop of virtual machine {instance_name} on host "
                  "{host_name} was successful."))

STOP_ERROR = (_("Stop of virtual machine {instance_name} on host "
                "{host_name} failed with exception: {error}"))

RESTART_SUCCESS = (_("Restart of virtual machine {instance_name} on host "
                     "{host_name} was successful."))

RESTART_ERROR = (_("Restart of virtual machine {instance_name} on host "
                   "{host_name} failed with exception: {error}"))

LPM_SUCCESS = (_("Migration of virtual machine {instance_name} from host "
                 "{source_host_name} to host {target_host_name} was "
                 "successful."))

LPM_ERROR = (_("Migration of virtual machine {instance_name} to host "
               "{target_host_name} failed with exception: {error}"))

LPM_ERROR_DEST = (_("Migration of virtual machine {instance_name} to host "
                    "{host_name} failed with exception: {error}"))

DELETE_ERROR = (_("Delete of virtual machine {instance_name} on host "
                  "{host_name} failed with exception: {error}"))

DELETE_SUCCESS = (_("Delete of virtual machine {instance_name} on host "
                    "{host_name} was successful. "))
RESIZE_ERROR = (_("Resize of virtual machine {instance_name} on host "
                  "{host_name} failed with exception: {error}"))

RESIZE_SUCCESS = (_("Resize of virtual machine {instance_name} on host "
                    "{host_name} was successful."))

CAPTURE_SUCCESS = (_("Capture of virtual machine {instance_name} on host "
                     "{host_name} was successful"))

CAPTURE_ERROR = (_("Capture of virtual machine {instance_name} on host "
                   "{host_name} failed with exception: {error}"))

ATTACH_SUCCESS = (_("Volume {volume_id} was successfully attached to "
                    "virtual machine {instance_name}."))

ATTACH_ERROR = (_("Volume {volume_id} could not be attached to "
                  "virtual machine {instance_name}. Error message: {error}"))

DETACH_SUCCESS = (_("Volume {volume_id} was successfully detached from "
                    "virtual machine {instance_name}."))

DETACH_ERROR = (_("Volume {volume_id} could not be detached from "
                  "virtual machine {instance_name}. Error message: {error}"))
