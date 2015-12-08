# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# =================================================================
# =================================================================

import logging

from paxes_nova.db.network import models as model

from paxes_nova.virt.ibmpowervm import ivm
from paxes_nova.virt.ibmpowervm.ivm import exception as pvmexcp

from paxes_nova.virt.ibmpowervm.vif import topo

from paxes_nova.virt.ibmpowervm.vif.common import exception as excp
from paxes_nova.virt.ibmpowervm.vif.common import ras

from paxes_nova.virt.ibmpowervm.vif.ivm import utils

from paxes_nova.virt.ibmpowervm.ivm import operator as ivm_oper
from paxes_nova.virt.ibmpowervm.ivm import common as pvmcom

from oslo.config import cfg

LOG = logging.getLogger(__name__)

CONF = cfg.CONF


class IBMPowerVMNetworkTopoIVM(topo.IBMPowerVMNetworkTopo):

    """
    IBM PowerVM Shared Ethernet Adapter Topology implementation for IVM.
    """

    def __init__(self, host_name, operator=None):
        """
        IBMPowerVMNetworkTopoIVM constructor

        :param host_name: Host_name for this compute process's node
        :param operator: PowerVMOperator to interface with PowerVM host
        """
        if operator:
            self._pvmops = operator
        else:
            msg = (ras.vif_get_msg
                   ('info', 'CONNECTION_IVM_TOPO'))
            ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG, msg)
            pvm_conn = pvmcom.Connection(CONF.powervm_mgr,
                                         CONF.powervm_mgr_user,
                                         CONF.powervm_mgr_passwd,
                                         CONF.powervm_mgr_port)
            self._pvmops = ivm_oper.IVMOperator(pvm_conn)

        topo.IBMPowerVMNetworkTopo.__init__(self, host_name=host_name,
                                            operator=self._pvmops)

    def _get_vea_for_slot(self, vios, devname, vea_slot_num, sea_pvid,
                          dom_factory=model.No_DB_DOM_Factory()):
        """
        Connects to IVM and gets a DOM VEA and SEA object based on a slot
        number.

        :param vios: A VioServer object to fetch adapter info from
        :param devname: The name of the adapter device
        :param vea_slot_num: The slot number of the VEA
        :param sea_pvid: This is needed to protect against a bug in IVM
        :param dom_factory: Factory used to create the DOM objects, optional.
        :returns vea: A VirtualEthernetAdapter object associated with the
                      passed in vea_slot_num.  If nothing is found (ie, the vea
                      is in "Defined" status), None will be returned.
        """
        # Start by running the command on the IVM endpoint
        # Output example(IVM):
        # 1,2,1,4000,"20,21,40"
        # if addl_vlan_ids is empty, IVM shows 'None'
        cmd = utils.get_veth_slot_info_cmd(vios.lpar_id, vea_slot_num)
        raw_output = self._pvmops.run_vios_command(cmd)

        # Since the IVM command to get virt_eths (what vea_slot_num was
        # calculated from) also includes veths in "Defined" state, this
        # command could actually return nothing.  Look for that...
        if raw_output:
            output = raw_output[0].split(',')
        else:
            return None

        sea_is_trunk = (output[0] == '1')
        sea_trunk_pri = int(output[1])
        sea_ieee_eth = (output[2] == '1')
        sea_port_vlan_id = int(output[3])
        sea_vswitch = 'ETHERNET0'  # IVM doesn't have vswitch field.

        # delete first 4 elements from output list
        output[0:4] = []
        addlvlans = output  # the rest are addl_vlan_ids or none

        # Fix up the addl vlans, remove the double quote characters
        if len(addlvlans) == 1 and addlvlans[0] == 'none':
            addlvlans = []
        else:
            addlvlans = map(lambda x: int(x.replace('\"', '')), addlvlans)

        # Create and return the DOM VEA
        return dom_factory.create_vea(name=devname,
                                      vio_server=vios,
                                      slot=vea_slot_num,
                                      pvid=sea_port_vlan_id,
                                      is_trunk=sea_is_trunk,
                                      trunk_pri=sea_trunk_pri,
                                      state='Available',
                                      ieee_eth=sea_ieee_eth,
                                      vswitch_name=sea_vswitch,
                                      addl_vlan_ids=addlvlans)

    def _get_all_vios(self, dom_factory=model.No_DB_DOM_Factory()):
        """
        Overridden method from
        powervc_nova.virt.ibmpowervm.vif.topo.IBMPowerVMNetworkTopo

        IVM systems only have a single VIOS by definition, so this returns a
        list with a single element.

        :param dom_factory: Factory used to create the DOM objects, optional.
        :returns vioses: A list of VioServer objects
        """
        ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG, "Enter")

        # Find the LPAR name and ID
        cmd = utils.get_vios_lpar_id_name()
        output = self._pvmops.run_vios_command(cmd)
        # Split the LPAR name and ID apart
        output_array = output[0].split()
        vio_lpar_id = output_array.pop(0)
        vio_lpar_name = ' '.join(output_array)
        msg = (ras.vif_get_msg('info', 'LPAR_DETAILS') %
               {'lpar_id': vio_lpar_id, 'lpar_name': vio_lpar_name})
        ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG, msg)

        # Get the RMC status
        cmd = utils.get_rmc_status()
        output = self._pvmops.run_vios_command(cmd)
        rmc_state = utils.parse_rmc_status(output, vio_lpar_id)

        # Use the factory to build the VIOS DOM object
        # NOTE: By virtue of the fact that we're running VIOS commands over
        #       SSH, the VIOS state is 'running'... so just hard code it below.
        return [dom_factory.create_vios(lpar_name=vio_lpar_name,
                                        lpar_id=vio_lpar_id,
                                        host_name=self.host_name,
                                        state='running',
                                        rmc_state=rmc_state)]

    def _get_sea_from_ivm(self, vios, sea_devname, virt_eths,
                          dom_factory=model.No_DB_DOM_Factory()):
        """
        Connect to an IVM endpoint and retrieve all of the configuration for a
        SEA and the VEAs associated with it.

        :param vios: The VioServer DOM object that represents this IVM
        :param sea_devname: The device name for the SEA to query
        :param virt_eths: Dictionary of physical adapter info from IVM
        :param dom_factory: Factory used to create the DOM objects, optional.
        :returns SharedEthernetAdapter: An SEA DOM object
        """
        # Got the SEA logical device name on VIOS.
        # Need to find out its pvid_adapter and virt_adapters and pvid
        cmd = utils.get_sea_attribute_cmd(sea_devname)

        output = self._pvmops.run_vios_command(cmd)

        # Output example:
        # ['value', '', '100', 'ent4', 'ent4,ent13,ent14',
        #  'ent24            Available     Shared Ethernet Adapter']
        if (len(output) == 0 or output[0] != 'value'):
            ras.trace(LOG, __name__, ras.TRACE_ERROR,
                      ras.vif_get_msg('error', 'VIOS_CMDFAIL') %
                      {'cmd': cmd})
            return None

        # Get rid of 'value','' from the list
        output[0:2] = []

        # Find out pvid adapter and all the virt_adapters for
        # the give SEA and create SeaDevices object based
        # on the information retrieved from HMC/IVM.
        sea_pvid = int(output[0].strip())

        sea_pvid_adpt = output[1].strip()
        sea_virt_adpts = output[2].strip().split(',')
        sea_state = output[3].split()[1]

        # virt_adpts has at least pvid_adpt
        if sea_pvid_adpt not in sea_virt_adpts:
            msg = ras.vif_get_msg('error', 'SEA_INVALIDSTATE')
            ras.function_tracepoint(LOG, __name__, ras.TRACE_ERROR,
                                    msg)
            return None

        # Get the full virt_adapters list without the pvid_adapter
        sea_virt_adpts.remove(sea_pvid_adpt)

        # Get the slot number for this adapter
        vea_slot_num = utils.parse_slot_num(virt_eths, sea_pvid_adpt)

        # Create the VEA and SEA dom objects
        vea_dev = self._get_vea_for_slot(vios,
                                         sea_pvid_adpt,
                                         vea_slot_num,
                                         sea_pvid,
                                         dom_factory)
        sea_dev = dom_factory.create_sea(name=sea_devname,
                                         vio_server=vios,
                                         slot=vea_slot_num,
                                         state=sea_state,
                                         primary_vea=vea_dev,
                                         control_channel=None,
                                         additional_veas=None)

        ras.trace(LOG, __name__, ras.TRACE_DEBUG,
                  ('SEA %(devname)s is discovered' %
                   {'devname': sea_devname}))

        # Find all the virt_adapters if there are any in sea_virt_adpts
        # list
        for devname in sea_virt_adpts:

            # Get the slot number for this adapter
            vea_slot_num = utils.parse_slot_num(virt_eths, devname)

            vea_dev = self._get_vea_for_slot(vios,
                                             devname,
                                             vea_slot_num,
                                             sea_pvid,
                                             dom_factory)

            try:
                sea_dev.add_vea_to_sea(vea_dev)
            except excp.IBMPowerVMInvalidSEAConfig:
                msg = (ras.vif_get_msg('error', 'SEA_FAILTOADDVEA') %
                       {'veaname': vea_dev.name,
                        'seaname': sea_dev.name})
                ras.trace(LOG, __name__, ras.TRACE_EXCEPTION, msg)
                return None
        # End of for loop to discovery all the virtual adapters
        # for the SEA. It is the end of the inner for loop
        return sea_dev

    def _find_orphaned_veas(self, vios, virt_eths, veas_attached_to_seas,
                            dom_factory=model.No_DB_DOM_Factory()):
        """
        Connect to the IVM endpoint and find any VEAs that are not associated
        with an SEA, and thus are "orphans."

        Results are not returned from this method, but are set into this
        objects member attributes.

        :param vios: The VioServer DOM object that represents this IVM
        :param virt_eths: Dictionary of physical adapter info from IVM
        :param veas_attached_to_seas: List of device names of VEAs that are
                                      attached to SEAs, and thus are not
                                      orphans
        :param dom_factory: Factory used to create the DOM objects, optional.
        :returns List: A list of VirtualEthernetAdapter DOM objects
        """
        # Loop back through the list of virtual devices.  We need to find
        # the VEAs that are not attached to any SEA.  They may have VLANs
        # attached to them that we do not wish to override
        orphan_veas = []
        for vea in virt_eths:
            if not (virt_eths[vea][0].strip().startswith(
                    'Virtual I/O Ethernet Adapter')):
                # this is not a VEA device.
                continue

            # Check to see if this VEA is part of the existing SEA list
            if vea in veas_attached_to_seas:
                continue

            # Get the slot number for this adapter
            vea_slot_num = utils.parse_slot_num(virt_eths, vea)

            # As an 'orphan' VEA (one not backed by a SEA), we need to get
            # the VLANs off of it and add it to a list of elements we do
            # not provision

            vea_dev = self._get_vea_for_slot(vios,
                                             vea,
                                             vea_slot_num,
                                             None,  # No SEA to check
                                             dom_factory)
            # The VEA in vea_slot_num may have been unavailable (ie, "Defined"
            # status), which returns None.  Don't include those.
            if vea_dev:
                orphan_veas.append(vea_dev)

        return orphan_veas

    def _populate_adapters_into_vios(self, vios,
                                     dom_factory=model.No_DB_DOM_Factory()):
        """
        Overridden method from
        powervc_nova.virt.ibmpowervm.vif.topo.IBMPowerVMNetworkTopo

        This method returns all the adapters, both SEA and VEA, for the VIOS
        this IVM runs on.  Each VEA will be attached to it's owning SEA, if it
        has one.

        :param vios: VioServer to fetch adapters from
        :param dom_factory: Factory used to create the DOM objects, optional.
        :returns boolean: True if at least one adapter was found, False
                          otherwise.
        """
        ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG, "Enter")

        try:
            # If the VIOS RMC state is down, we can't use adapters on it.
            if vios.rmc_state.lower() != 'active':
                ras.trace(LOG, __name__, ras.TRACE_INFO,
                          _('RMC state is not active on VIOS lpar %s') %
                          vios.lpar_name)
                return False

            # Find all of the adapters on the VIOS
            cmd = utils.get_all_sea_with_physloc_cmd()
            output = self._pvmops.run_vios_command(cmd)
            virt_eths = utils.parse_physloc_adapter_output(output)

            # Find all of the SEAs on this IVM
            seas_found = {}
            for vea in virt_eths:
                if virt_eths[vea][0].strip() != 'Shared Ethernet Adapter':
                    # this is not a SEA device.
                    continue
                seas_found[vea] = virt_eths[vea]

            # Go through the SEAs and get their current configuration from IVM
            veas_attached_to_seas = []
            for sea_devname in seas_found:

                # Connect to the IVM endpoint and collect the SEA info
                sea_dev = self._get_sea_from_ivm(vios, sea_devname, virt_eths,
                                                 dom_factory)

                # Keep track of which VEAs are owned by an SEA so we can later
                # detect orphan VEAs
                veas_attached_to_seas.append(sea_dev.primary_vea.name)
                for vea in sea_dev.additional_veas:
                    veas_attached_to_seas.append(vea.name)

            # Any VEA not on an SEA is considered an orphan
            self._find_orphaned_veas(vios, virt_eths, veas_attached_to_seas,
                                     dom_factory)

            # Return True if adapters were found
            if len(seas_found.values()) > 0:
                ras.trace(LOG, __name__, ras.TRACE_DEBUG,
                          'SEA discover finished successfully.')
                return True

            ras.trace(LOG, __name__, ras.TRACE_ERROR,
                      ras.vif_get_msg('error', 'VIOS_NOSEA'))
            raise excp.IBMPowerVMValidSEANotFound()

        except pvmexcp.IBMPowerVMCommandFailed:
            ras.trace(LOG, __name__, ras.TRACE_EXCEPTION,
                      ras.vif_get_msg('error', 'VIOS_CMDFAIL') % {'cmd': cmd})
            return False
        except Exception as e:
            ras.trace(LOG, __name__, ras.TRACE_EXCEPTION,
                      ras.vif_get_msg('error', 'VIOS_UNKNOWN') + " (" +
                      _('%s') % e + ")")
            return False
