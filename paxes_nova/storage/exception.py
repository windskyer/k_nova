# vim: tabstop=4 shiftwidth=4 softtabstop=4

# =================================================================
# =================================================================

from nova import exception
from paxes_nova import _


class IBMPowerVCSCGNotFound(exception.NovaException):
    msg_fmt = _("The storage connectivity group with ID '%(scg_id)s' was "
                "not found. The storage connectivity group associated with "
                "this request may have been removed. Ensure that the ID "
                "specified is returned by the storage-connectivity-groups "
                "REST API and try this request again.")


class IBMPowerVCFCPortNotFound(exception.NovaException):
    msg_fmt = _("The Fibre Channel port with ID %(fc_id)s was not found. "
                "The Fibre Channel port might have been removed from the "
                "managed system. Ensure that the IDs specified are returned "
                "by the host-storage-topologies REST API and try this "
                "request again.")


class IBMPowerVCSCGNotInFlavor(exception.NovaException):
    msg_fmt = _("The flavor with ID %(flavor_id)s does not have an "
                "associated storage connectivity group. This may be a normal "
                "condition if the storage connectivity group can be "
                "determined in another way. Only custom-built flavors can "
                "specify a storage connectivity group.")


class IBMPowerVCProviderError(exception.NovaException):
    msg_fmt = _("This error occurred when retrieving storage providers: "
                "%(response)s  Ensure that the Cinder service is running "
                "and try the request again.")


class IBMPowerVCStorageError(exception.NovaException):
    msg_fmt = _("A problem was encountered during storage processing: "
                "%(error)s.")


class IBMPowerVCViosNotFound(exception.NovaException):
    msg_fmt = _("The Virtual I/O Server with ID %(v_id)s was not found. "
                "The Virtual I/O Server partition might have been "
                "removed from the managed host. Ensure that the IDs "
                "specified are returned by the host-storage-topologies REST "
                "API and try this request again.")


class IBMPowerVCSCGRefByInstance(exception.NovaException):
    msg_fmt = _("The storage connectivity group '%(scg_name)s' cannot be "
                "deleted because it is referenced by at least one virtual "
                "server. The server might have been deployed using the "
                "storage connectivity group or it might have been "
                "automatically assigned to the group after being added. The "
                "instances that reference this storage connectivity group "
                "are: %(instance_names)s.")


class IBMPowerVCSCGNotDetermined(exception.NovaException):
    msg_fmt = _("A storage connectivity group could not be determined "
                "for the operation. Enable a default storage connectivity "
                "group for NPIV or shared storage pool connectivity, or "
                "create a custom storage connectivity group with the desired "
                "connectivity type that includes the Virtual I/O Servers "
                "that are used by the virtual machine in the operation, "
                "then try the request again. Refer to the following detailed "
                "messages for more information: %(messages)s")


class IBMPowerVCStorageMigError(exception.NovaException):
    msg_fmt = _("The virtual machine that you want to migrate uses a Fibre "
                "Channel (FC) port with WWPN '%(wwpn)s', but there is no "
                "corresponding storage mapping on destination host "
                "'%(dest_host_name)s'. Complete the following steps and then "
                "try the migration again: 1. Ensure that the backing storage "
                "providers are in running state. 2. Ensure that the Fibre "
                "Channel ports are assigned to registered fabrics for all of "
                "your hosts. 3. Ensure that the storage "
                "connectivity group used for migration, '%(scg_name)s', "
                "contains a corresponding set of Virtual I/O Servers on the "
                "source and destination hosts, and that there are Fibre "
                "Channel ports on the destination Virtual I/O Servers with "
                "the following attributes: The ports are configured to allow "
                "attachments, the ports are in OK status, the port tags on "
                "the ports match the port tag on the storage connectivity "
                "group (if a tag exists on the storage connectivity group), "
                "and there are ports for both fabrics in a dual fabric "
                "environment.")


class IBMPowerVCConnectivityError(exception.NovaException):
    msg_fmt = _("No Virtual I/O Servers within deploy hypervisor host "
                "'%(host_name)s' are available for storage connections "
                "that use storage connectivity group '%(scg_name)s'. The "
                "associated message is '%(sub_case)s' Ensure "
                "the following conditions are satisfied: "
                "1. The Virtual I/O Servers are running and RMC is active on "
                "them. 2. The hosts are in a good Operating state. 3. The "
                "HMC connections are active (OK). 4. "
                "For NPIV connectivity, check that physical Fibre Channel "
                "ports are available, cabled, and owned by running Virtual "
                "I/O Servers. 5. The Fibre Channel Port "
                "Configuration settings show that the ports allow attachment, "
                "and if the storage connectivity group has a port tag set, "
                "ensure that the Fibre Channel port tag matches it. 6. "
                "There are one or more virtual slots available for connecting "
                "to disks via vFC or vSCSI adapters. 7. There are one or "
                "more applicable storage providers in a Running state.")


class IBMPowerVCStorageMigError2(exception.NovaException):
    msg_fmt = _("The virtual machine that you want to migrate uses one or "
                "more virtual SCSI connections to shared storage pool "
                "volumes, but there is not a corresponding set of virtual "
                "SCSI connections available on the destination host "
                "'%(dest_host_name)s'. Ensure the shared storage pool "
                "provider is in running state. Ensure that the storage "
                "connectivity group '%(scg_name)s' used for migration "
                "contains a set of %(needed)d Virtual I/O Servers on the "
                "destination host. Ensure those I/O servers are running and "
                "have active RMC connections. Provision any new Virtual I/O "
                "Servers needed, make any needed state changes, increase "
                "the maximum virtual I/O slots if needed, then try the "
                "migration request again.")


class IBMPowerVCGetProviderError(exception.NovaException):
    msg_fmt = _("The storage providers associated with the storage "
                "connectivity group '%(scg_name)s' could not be determined. "
                "Ensure that the Cinder service is running and that you are "
                "authorized, then try the request again.")


class IBMPowerVCNoProviders(exception.NovaException):
    msg_fmt = _("The storage connectivity group '%(scg_name)s' does not "
                "have access to any active storage providers. "
                "Ensure that a storage provider is registered, that it is "
                "not in Error state and the storage connectivity group's "
                "properties allow connectivity to one or more providers, "
                "then try the request again.")
