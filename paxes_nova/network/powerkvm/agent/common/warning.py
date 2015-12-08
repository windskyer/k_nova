#
# =================================================================
# =================================================================

from nova import exception
from powervc_nova import _


class OVSWarning(exception.NovaException):
    msg_fmt = _("An issue was detected during validation.")
    name = "generic.warning"


class OVSPortModificationNetworkOutageWarning(OVSWarning):
    msg_fmt = _("Adding, deleting, or updating a port from Open vSwitch "
                "%(ovs)s may cause a loss of network connectivity for virtual "
                "machines that are using this port.")
    name = "port.modification.network.outage.warning"


class OVSLastPortRemovalWarning(OVSWarning):
    msg_fmt = _("This operation will remove the last "
                "port from the Open vSwitch %(ovs)s.  No external traffic "
                "will be available on this virtual switch after the "
                "port is removed.")
    name = "last.port.removal.warning"


class OVSPortModificationVMActionWarning(OVSWarning):
    msg_fmt = _("Do not run any operations on the virtual machines "
                "on this Host while the ports are being modified. "
                "If you try to run another operation, such as deploy, "
                "the operations might fail.")
    name = "port.modification.vm.action.failure.warning"


class OVSMultipleVirtualPortsWarning(OVSWarning):
    msg_fmt = _("The virtual switch %(vswitch_name)s has multiple virtual "
                "ports configured on it. This configuration will bridge "
                "independent physical networks together, which is an "
                "uncommon configuration. You can instead bond adapters "
                "together in a single virtual port.")
    name = "multiple.virtual.port.warning"


class OVSMovingPortsFromBridgeToOVSWarning(OVSWarning):
    msg_fmt = _("Bridges cannot be added directly to the virtual switches. "
                "Proceeding with this operation will remove the components "
                "from the bridge %(bridge_name)s and add them to the "
                "virtual switch. The following components will be moved:  "
                "%(ports)s")
    name = "moving.ports.from.bridge.warning"


class OVSNoPortsOnBridgeWarning(OVSWarning):
    msg_fmt = _("Bridges cannot be added directly to the virtual switches. "
                "Only the components of the bridge can be moved to the "
                "virtual switch. The bridge %(bridge_name)s cannot be added "
                "to the virtual switch because it has no components "
                "associated with it, therefore there is no way  "
                "to associate the bridge with the virtual switch.  "
                "This portion of the request will be ignored.")
    name = "no.ports.on.bridge.warning"


class OVSAdapterHasTempIPAddressWarning(OVSWarning):
    msg_fmt = _("Adapter(s) %(adapter_name)s have a temporary IP address "
                "assigned.  This operation will restart the network "
                "service and remove this address from the adapter "
                "configuration.  Before continuing, it is recommended that "
                "you save the configuration in the appropriate ifcfg file "
                "in the  /etc/sysconfig/network-scripts/ directory.")
    name = 'adapter.temp.ipaddress.warning'


class OVSAdapterDHCPWarning(OVSWarning):
    msg_fmt = _("Adapter(s) %(adapter_name)s are configured for DHCP.  This "
                "operation will restart the network service, "
                "which could cause a new IP address to be assigned.")
    name = 'adapter.dhcp.warning'


class OVSMovingDHCPAddressWarning(OVSWarning):
    msg_fmt = _("The IP address on %(adapter_name)s is being moved "
                "to %(target_dev)s.  The IP address was obtained by "
                "DHCP.  This operation will restart the network "
                "service, which might cause a new IP address to be "
                "assigned to the target device.  The target device "
                "will have a unique MAC address as well.")
    name = 'adapter.move.ipaddress.dhcp.warning'
