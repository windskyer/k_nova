# vim: tabstop=4 shiftwidth=4 softtabstop=4

# =================================================================
# =================================================================

from nova import exception

from paxes_nova import _


class IBMPowerVMConflictWarning(exception.NovaException):
    status_code = 409


class IBMPowerVMNetworkConflict(IBMPowerVMConflictWarning):
    msg_fmt = _('Warning: Other networks on host %(hostid)s also use VLAN '
                '%(vlanid)s on virtual switch %(vswitch)s. Therefore, if you '
                'use adapter %(sea)s for this network, the following networks '
                'will change to use the same adapter: %(networklist)s')


class IBMPowerVMSeaDefaultConflict(IBMPowerVMConflictWarning):
    msg_fmt = _(" Warning: VMs are in use for this network, changing adapter"
                " may result in network down time. ")


class IBMPowerVMSeaChangeConflict(IBMPowerVMConflictWarning):
    msg_fmt = _(" Warning: VLAN %(vlanid)s is already deployed to SEA %(sea)s"
                " on VIOS LPAR %(vios)s on the host %(host)s.  Changing the"
                " adapter will migrate that VLAN to the new adapter.")
