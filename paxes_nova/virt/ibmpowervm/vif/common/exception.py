# vim: tabstop=4 shiftwidth=4 softtabstop=4

# =================================================================
# =================================================================

from nova import exception

from paxes_nova import _


class IBMPowerVMInvalidHostConfig(exception.NovaException):
    msg_fmt = _("Invalid Host configuration: %(attr)s")


class IBMNetworkNotValidForHost(exception.NovaException):
    msg_fmt = _("Invalid Network with id: %(network)s for the Host: %(host)s")


class IBMPowerVMSEANotValidForPlug(exception.NovaException):
    msg_fmt = _("The plug operation cannot proceed on the SEA: %(sea)s"
                " for the Host: %(host)s. The VLAN is on the primary vea of"
                " another SEA on the same host. Please update the network to "
                "map to SEA: %(sea_alt)s and retry.")


class IBMPowerVMSEAVSwitchNotAllowed(exception.NovaException):
    status_code = 500
    msg_fmt = _("The reassignment to Shared Ethernet Adapter %(sea)s failed"
                " because it is not on the same virtual switch as the current"
                " SEA and one or more virtual machines are associated with it."
                " Choose another SEA that either is on the same virtual switch"
                " as the current SEA or remove the virtual machines associated"
                " with the network. Then, retry the operation")


class IBMPowerVMInvalidVEAConfig(exception.NovaException):
    msg_fmt = _("Invalid VEA configuration: %(attr)s")


class IBMPowerVMInvalidMgmtNetworkCfg(exception.NovaException):
    msg_fmt = _("Invalid IBMPowerVM management network configuration: %(msg)s")


class IBMPowerVMFailToAddDataVlan(exception.NovaException):
    msg_fmt = _("PowerVM failed to add data vlan %(data_vlan_id)d ")


class IBMPowerVMInvalidDataVlanID(exception.NovaException):
    msg_fmt = _("Invalid vlan id %(data_vlan_id)d for IBMPowerVM VIF driver")


class IBMPowerVMInvalidVlanConfig(exception.NovaException):
    msg_fmt = _("Invalid vlan configuration")


class IBMPowerVMInvalidVETHConfig(exception.NovaException):
    msg_fmt = _("Invalid VETH device configuration: %(attr)s")


class IBMPowerVMVIDPVIDConflict(exception.NovaException):
    msg_fmt = _("VID conflicts with the virt_adapter's arbitrary PVID")

    def __init__(self, vea):
        """
        Builds a new exception to indicate that a VEA exists with a given VLAN
        as its PVID.

        :param vea: The Virtual Ethernet Adapter that has the VLAN as the PVID
        """
        self.vea = vea


class IBMPowerVMVIDisCtlChannel(exception.NovaException):
    msg_fmt = _("The VLAN requested is utilized on the ctl_channel.")


class IBMPowerVMVEATooManyVlan(exception.NovaException):
    msg_fmt = _("Too many VLANs per VEA")


class IBMPowerVMInvalidSEAConfig(exception.NovaException):
    msg_fmt = _("Invalid SEA configuration")


class IBMPowerVMValidSEANotFound(IBMPowerVMInvalidSEAConfig):
    status_code = 404
    msg_fmt = _("A valid Shared Ethernet Adapter was not found. Ensure that a "
                "valid Shared Ethernet Adapter is configured and try the "
                "operation again.  Please refer to the PowerVC compute logs "
                "for more information.")


class IBMPowerVMInvalidSEASelection(IBMPowerVMInvalidSEAConfig):
    status_code = 500
    msg_fmt = _("VLAN %(vlan)s on Host %(host)s can not be associated with"
                " the Shared Ethernet Adapter %(sea)s  as it is already"
                " associated with another Shared Ethernet Adapter %(psea)s"
                " on either its primary Virtual Ethernet Adapter or on "
                "its Control Channel Adapter")


class IBMPowerVMInvalidSEASelectionOnOrphanVEA(IBMPowerVMInvalidSEAConfig):
    status_code = 500
    msg_fmt = _("VLAN %(vlan)s on Host %(host)s can not be associated with "
                "the Shared Ethernet Adapter %(sea)s.  The VLAN has already "
                "been deployed on the VIOS, but is not backed by a Shared "
                "Ethernet Adapter.  Please remove the VLAN from the VIOS, "
                "or associate it with the correct Shared Ethernet Adapter "
                "and try again.")


class IBMPowerVMNonPrimarySEASelection(IBMPowerVMInvalidSEAConfig):
    status_code = 500
    msg_fmt = _('Device %(devname)s is not a valid selection for the Network.'
                '  Please choose a that is currently available')


class IBMPowerVMRMCDown(exception.NovaException):
    msg_fmt = _('The new VLAN was not provisioned on host %(host)s. '
                'RMC connections are inactive on the following '
                'Virtual I/O Server LPARs: %(lpar_id)s. Ensure that all RMC '
                'connections are active and retry the operation.')


class IBMPowerVMRMCBusy(exception.NovaException):
    msg_fmt = _('The new VLAN was not provisioned on host %(host)s because '
                'RMC communication timed out on the Virtual I/O Servers with '
                'the following LPAR ids: %(lpar_id)s. Ensure that all RMC '
                'connections are active, then retry the operation.')


class IBMPowerVMTaggedVLAN1NotAllowed(IBMPowerVMInvalidSEAConfig):
    status_code = 500
    msg_fmt = _('Vlan 1 cannot be configured as a tagged vlan.  If you wish '
                'to use VLAN 1, it must be configured as the port vlan id of '
                'a Shared Ethernet Adapter on host %(host)s.')


class IBMPowerVMRMCDownNoSEA(exception.NovaException):
    msg_fmt = _('Host %(host)s was not added because no Shared Ethernet '
                'Adapters were discovered for it. The likely cause of this '
                'issue is an inactive RMC connection to the Virtual I/O '
                'Server partition. Ensure that the host has a VIOS with at '
                'least one Shared Ethernet Adapter with an active RMC '
                'connection, and try the operation again.')


class IBMPowerVMAllRMCDown(exception.NovaException):
    msg_fmt = _('Host %(host)s was not added because there were no active RMC '
                'connections to any of its Virtual I/O Servers. Ensure the '
                'host has at least one VIOS with an active RMC connection and '
                'try the operation again.')


class IBMPowerVMMaximumVEAsPerSEA(exception.NovaException):
    msg_fmt = _("The PowerVC limit of 15 VEAs per SEA has been reached.")


class IBMPowerVMOutOfAvailableVIDPool(exception.NovaException):
    msg_fmt = _("The available VLAN ID pool is exhausted")


class IBMPowerVMVlanNotAvailable(exception.NovaException):
    msg_fmt = _("Unable to find VLAN ID in available pool")


class IBMPowerVMNetworkAssociationPutException(exception.NovaException):
    status_code = 500
    msg_fmt = _("Unable to add/update Network Mapping for %(hostid)s due"
                " to unspecified error.  Please reference the server logs"
                " for more information")


class IBMPowerVMNetworkInUseException(exception.NovaException):
    status_code = 500
    msg_fmt = _("Unable to set the network to Do Not Use because there are"
                " Virtual Machines on %(hostid)s that are utilizing the "
                " Network. Please relocate those Virtual Machines off of "
                " the system before updating the network to Do Not Use.")


class IVMPowerVMDomParseError(exception.NovaException):
    msg_fmt = _("Unable to parse the Network Configuration of the host.")


class IBMPowerVMNetworkNotValidForHost(exception.NovaException):
    msg_fmt = _("The specified network(s) has been configured to not be used"
                " on %(host)s.  Please specify a different network, or"
                " modify the configuration of this network for this host.")


class IBMPowerVMIncorrectLoadGroup(exception.NovaException):
    msg_fmt = _("The system is unable to determine the appropriate Shared"
                " Ethernet Adapter (SEA) to utilize for VLAN deployment."
                " This might occur when the primary SEA is configured for"
                " failover but is out of sync with the designated failover"
                " SEA. Ensure that failover is properly configured to pair to"
                " the primary SEA and that the failover SEA is accessible,"
                " then retry the operation.")
