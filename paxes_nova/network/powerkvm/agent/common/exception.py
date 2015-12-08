#
# =================================================================
# =================================================================

from nova import exception

from powervc_nova import _


class IBMPowerKVMInvalidLSHWOutput(exception.NovaException):
    msg_fmt = _("Invalid 'lshw -class network' output. Please run this command"
                " on the system and verify that the output is correct.")


class IBMPowerKVMInvalidVSCTLOutput(exception.NovaException):
    msg_fmt = _("Invalid 'ovs-vsctl show' network output. Please run this "
                "command on the system and verify that the output is correct.")


class IBMPowerKVMInvalidBRCTLOutput(exception.NovaException):
    msg_fmt = _("Invalid 'brctl show' network output. Please run this command "
                "on the system and verify that the output is correct.")


class IBMPowerKVMInvalidBondOutput(exception.NovaException):
    msg_fmt = _("Invalid output while obtaining the bond information. Please"
                " check the system for valid bonds.")


class IBMPowerKVMInvalidIFCONFIGOutput(exception.NovaException):
    msg_fmt = _("Invalid 'ifconfig -a' output. Please run this command"
                " on the system and verify that the output is correct.")


class IBMPowerKVMInvalidIPAddressOutput(exception.NovaException):
    msg_fmt = _("Invalid 'ip address' output. Please run this command"
                " on the system and verify that the output is correct.")


class IBMPowerKVMCommandExecError(exception.NovaException):
    msg_fmt = _("Error executing the command: %(cmd)s. The system has"
                " returned the following error messsage: %(exp)s")


class IBMPowerKVMEmptyInterfaceList(exception.NovaException):
    msg_fmt = _("No valid interface found on the system. Use lshw -class "
                " network to check the hardware information on the system.")


class IBMPowerKVMNMControlled(exception.NovaException):
    msg_fmt = _("Reconfiguration of NM_CONTROLLED adapters is not supported."
                "  Edit the /etc/sysconfig/network-scripts/ifcfg-X file to "
                "remove the NM_CONTROLLED attribute.")


class IBMPowerKVMTargetIpConfig(exception.NovaException):
    msg_fmt = _("An error was found when attempting to move IP configuration "
                "from %(src)s to %(tgt)s. "
                "The device %(tgt)s already has IP configuration "
                "that will be overwritten. Refer to"
                " /etc/sysconfig/network-scripts/ to see the configuration "
                "and make changes to avoid overwriting IP configuration.")


class IBMPowerKVMSourceTargetOVS(exception.NovaException):
    msg_fmt = _("An error was found when attempting to move IP configuration "
                "from %(src)s to %(tgt)s. "
                "Both the target and source devices are of type OVSBridge."
                " This is not supported at the moment.")


class IBMPowerKVMUnsupportedRequest(exception.NovaException):
    msg_fmt = _("The source or the target should be an Open vSwitch, IP"
                " movement is not supported across other type of devices.")


class IBMPowerKVMSourceDevFileDoesNotExit(exception.NovaException):
    msg_fmt = _("The source device does not have a proper network script file"
                " on the system (typically named ifcfg-%(eth)s). The file"
                " either does not exist, was deleted or the source device "
                "cannot be found by the kernel.")


class QpidPingFailure(exception.NovaException):
    msg_fmt = _("Unable to ping the Qpid Server after %(timeout)s seconds.")


class IBMPowerKVMIPAddressDelete(exception.NovaException):
    msg_fmt = _("There was an attempt to remove all ports on virtual switch "
                " %(ovs_name)s  in a single operation. "
                "To move the IP Address to the appropriate adapter, "
                "first remove all virtual ports that are not associated"
                " with the IP address configured on the virtual switch. "
                "Then perform a second remove operation.")


class IPAddressAdd(exception.NovaException):
    msg_fmt = _("Unable to move the %(dev)s onto the Virtual Switch "
                "%(ovs_name)s. The device you are trying to move and "
                "the virtual switch have an IP address assigned. To "
                "resolve this, remove the IP address from either the "
                "device or the virtual switch and try again.")


class IPAddressAddMultiPort(exception.NovaException):
    msg_fmt = _("Unable to move %(dev)s onto Virtual Switch %(ovs_name)s. "
                "There are multiple ports being added to the virtual switch "
                "with IP addresses assigned. Manually remove the IP "
                "addresses from the ports and retain only the one IP "
                "address which you want to add to the virtual switch, and "
                "try again.")


class IBMPowerKVMOVSValidationError(exception.NovaException):
    """"
    This class will be overridden by the exception definitions
    for specific validation errors.
    """


class IBMPowerKVMOVSPortExistsError(IBMPowerKVMOVSValidationError):
    msg_fmt = _("The Open vSwitch port %(port_name)s already exists "
                "on virtual switch %(switch_name)s.")


class IBMPowerKVMNoOVSPortError(IBMPowerKVMOVSValidationError):
    msg_fmt = _("The Open vSwitch port %(port_name)s does not exist "
                "on virtual switch %(switch_name)s.")


class IBMPowerKVMOVSDoesNotExistError(IBMPowerKVMOVSValidationError):
    msg_fmt = _("The Open vSwitch %(switch_name)s does not exist.")


class IBMPowerKVMOVSParentPortExistsError(IBMPowerKVMOVSValidationError):
    msg_fmt = _("The adapter %(adapter_name)s you are trying to add or update "
                "is already in use on device %(parent_name)s.")


class IBMPowerKVMOVSAdapterNotAvailableError(IBMPowerKVMOVSValidationError):
    msg_fmt = _("The device %(adapter_name)s does not exist.")


class IBMPowerKVMOVSIntBridgeSpecifiedError(IBMPowerKVMOVSValidationError):
    msg_fmt = _("The integration bridge, %(bridge_name)s, cannot be "
                "specified for this operation.")


class IBMPowerKVMOVSNoAdapterSpecified(IBMPowerKVMOVSValidationError):
    msg_fmt = _("No adapters were specified for Open vSwitch port "
                "%(port_name)s")


class IBMPowerKVMOVSNoValidDomSpecified(IBMPowerKVMOVSValidationError):
    msg_fmt = _("Either no DOM was specified or the DOM is invalid.")


class IBMPowerKVMUnknownKey(IBMPowerKVMOVSValidationError):
    msg_fmt = _("The request body key %(key)s is invalid.")


class IBMPowerKVMOneHostAtTime(IBMPowerKVMOVSValidationError):
    msg_fmt = _("Specify only one host in the request body.")


class IBMPowerKVMPortNameMismatch(IBMPowerKVMOVSValidationError):
    msg_fmt = _("The Open vSwitch port %(ovs_port_name)s must use the same "
                "name as the component %(port_name)s.")


class IBMPowerKVMOVSAdaptersAppearMultipleTimes(IBMPowerKVMOVSValidationError):
    msg_fmt = _("The following adapters appear more than once in the "
                "components list for %(ovs_port_name)s: %(adapter_names)s")


class IBMPowerKVMOVSPortsAppearMultipleTimes(IBMPowerKVMOVSValidationError):
    msg_fmt = _("The following ports appear more than once in the ports "
                "list for %(ovs_name)s: %(port_names)s")


class IBMPowerKVMOVSPortInMultipleOVS(IBMPowerKVMOVSValidationError):
    msg_fmt = _("One or more ports that are listed for the virtual switch "
                "%(ovs_name)s are shared by other virtual switches. The "
                "following list contains all the shared ports and the "
                "virtual switches that are associated with them: "
                "%(port_list)s")
