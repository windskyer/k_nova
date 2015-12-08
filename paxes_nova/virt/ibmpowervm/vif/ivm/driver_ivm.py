# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# =================================================================
# =================================================================

"""
    IBM PowerVM VLAN VIF driver - IVM implementation.

    IBMPowerVMVlanVIFDriver is a IBM PowerVM based VIF driver that manages the
    VLAN bridging capability provided by PowerVM. The current implementation
    is based on IVM and should be used with Neutron ML2 plugin.

    This VIF driver requires the VIOS to have at least one Shared Ethernet
    Adapter(SEA) pre-configured with management vlan as the untagged port
    vlan on the pvid adapter without any additional 8021Q VLAN ids on it.
    Any new VLAN id that adds to the SEA will be plugged into a trunk
    virtual ethernet adapter's addl_vlan_id list as 8021Q VLAN.

    IBMPowerVMVlanVIFDRiver has the following tunable in the nova.conf:
    1. ibmpowervm_data_sea_devs, an StrOpt that describes the SEA device
       name that ibmpowervm VIF driver can use for VM deployment. This is
       optional and by default it is empty. By default, the sea that has
       management ip will be used for data vlan deployment. All the SEAs
       that used for VM VLAN deployment can be specified in the list
       delimited by ':', for example:  ibmpowervm_data_sea_devs = ent4:ent6
       When this ibmpowervm_data_sea_devs list is specified, VIF will only
       use the sea devices on the list for VM vlan deployment.

    In order to use this VIF driver, this class (IBMPowerVMVlanVIFDriver) needs
    to be instantiated in the PowerVMOperator's __init__ method. Then
    IBMPowerVMVlanVIFDriver's plug or unplug method will bridge or unbridge
    (respectively) a given VLAN used by a VM.
"""

import time

from nova.openstack.common import log as logging

from paxes_nova.virt.ibmpowervm import ivm
from paxes_nova.virt.ibmpowervm import vif

from paxes_nova.virt.ibmpowervm.vif import driver

from paxes_nova.virt.ibmpowervm.vif.common import exception as excp
from paxes_nova.virt.ibmpowervm.vif.common import ras

from paxes_nova.virt.ibmpowervm.vif.ivm import topo_ivm
from paxes_nova.virt.ibmpowervm.vif.ivm import utils

from paxes_nova.virt.ibmpowervm.ivm import operator
from paxes_nova.virt.ibmpowervm.ivm import common as pvmcom

from oslo.config import cfg

from paxes_nova import _

LOG = logging.getLogger(__name__)

RMDEV_ATTEMPTS = 3

CONF = cfg.CONF


class IBMPowerVMVlanVIFDriver(driver.IBMPowerVMBaseVIFDriver):

    """
    IBM PowerVM Vlan VIF driver, IVM implementation
    """

    def __init__(self, host_name, pvm_op=None):
        """
        IBMPowerVMVlanVIFDriver's constructor.

        :param host_name: The name of the host machine for nova, not to be
                          confused with the networking concept of hostname.
        :param pvm_op: It is the PowerVMOperator._operator initialized
                       by __init__() of PowerVMOperator.
        """
        # IVMOperator for IVM ssh command execution..
        if pvm_op:
            self._pvmops = pvm_op
        else:
            pvm_conn = pvmcom.Connection(CONF.powervm_mgr,
                                         CONF.powervm_mgr_user,
                                         CONF.powervm_mgr_passwd,
                                         CONF.powervm_mgr_port)
            msg = (ras.vif_get_msg
                   ('info', 'CONNECTION_IVM'))
            ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG, msg)
            self._pvmops = operator.IVMOperator(pvm_conn)

        # Centralized topology service for IVM
        self._topo = topo_ivm.IBMPowerVMNetworkTopoIVM(host_name, self._pvmops)

        # Call parent constructor
        super(IBMPowerVMVlanVIFDriver, self).__init__()

    def _find_veths_by_vlan(self, vlan_id):
        """
        Will run the command to list the virtual eth's on the system and return
        all of the lines that match this vlan. This may include the veth
        that bridges the vlan and the client veth that using the vlan.
        need to check both veth pvid and addl_vlan_ids.

        :param vlan_id: The additional VLAN id to search for
        :return: Will return a comma delimited string.  The output may look
                 like the following:
                    ['1,13,4093,1,1,"201,207"',
                     '1,19,4000,1,1,"300,301,302"']
                 The order is...
                   0 - lpar_id
                   1 - slot_num
                   2 - port_vlan_id
                   3 - is_trunk
                   4 - ieee_virtual_eth
                   5..n - addl_vlan_ids - Notice the quotes around this!
        """
        cmds = utils.get_veth_list_by_vlan_id_cmd()
        output = self._pvmops.run_vios_command(cmds)
        msg = (ras.vif_get_msg('info', 'LIST_VETH') % {'cmd': output})
        ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG, msg)
        results = []
        for output_line in output:
            spots = output_line.split(',')

            # check port_vlan_id first
            if int(spots[2]) == vlan_id:
                results.append(output_line)
                msg = (ras.vif_get_msg('info', 'PVID_FOUND') %
                       {'pvid': output_line})
                ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG, msg)
                # no need to check addl_vlan_ids then , continue
                continue
            # Based on the first four...and the fact that we only need to check
            # additional vlans, pop off 0 through 4 and then check to see if
            # this VLAN is in the list
            spots[0:5] = []

            # Loop through all the additional VLANs (which may need to be
            # trimmed) and if one matches, add it to results
            for addl_vlan_str in spots:
                if (addl_vlan_str == 'none'):
                    continue

                addl_vlan_int = vif.parse_to_int(addl_vlan_str)
                if addl_vlan_int == vlan_id:
                    msg = (ras.vif_get_msg('info', 'VLANID_IN_ADDL') %
                           {'vlanid': vlan_id})
                    ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG,
                                            msg)
                    results.append(output_line)
                    break

        return results

    def _find_vlan_users(self, vios, vlanid, vswitch):
        """
        This method will search for the given vlanid on the system and return
        two things.  First, the pvid of the SEA on the VIOS containing the
        vlanid.  Second, the number of users of that vlanid total (ie, client
        LPARs in addition to VIOS).

        :param vios: Not needed for IvM.  Only here because superclass has it.
        :param vlanid: VLANID to search for
        :param vswitch: Not needed for IVM since there's only one vswitch.
        :returns pvid: port vlanid of the SEA on the VIOS containing the
                       vlanid.  Will be 0 if not found.
        :returns num_users: Total number of adapters found with this vlanid.
        """
        pvid = 0

        # Get the VEA slot that bridges this vlanid
        # output looks like the following:
        # lparid, slot,port_vlan_id, is_trunk,
        # ieee_virtual_eth,addl_vlan_ids
        # [1,13,4093,1,1,"201,207"],
        output = self._find_veths_by_vlan(vlanid)
        num_users = len(output)
        msg = (ras.vif_get_msg('info', 'VLAN_USERS') %
               {'vlanid': vlanid, 'num': num_users})
        ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG, msg)
        if output:
            # Find the veth that represents the bridging veth
            for veth in output:
                # There can only be one is_trunk adapter (ie, adapter on the
                # VIOS)
                elements = veth.split(',')
                if int(elements[3]):
                    pvid = int(elements[2])

        return pvid, num_users

    def _create_vea_on_vios(self, vios, sea, slotnum, port_vlan_id,
                            addl_vlan_ids):
        """
        This method will create the 802.1Q VirtualEthernetAdapter on the
        VIOS and return the device name of the newly created adapter.  A
        IBMPowerVMFailToAddDataVlan should be raised if unable to create the
        new VEA.

        :param vios: VioServer DOM object representing VIOS this is being
                     created on.
        :param sea: SEA that owns the VEA.  Not needed on IVM as the VEA is
                    attached to the SEA in a separate step.
        :param slotnum: Virtual slot number to create the new VEA in.
        :param port_vlan_id: pvid to set on the new VEA
        :param addl_vlan_ids: Additional vlan ids to set on the new VEA
        :returns vea_devname: Device name of the newly created VEA
        :returns slot_number: Always returns None
        """
        ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG, "Enter")

        try:
            cmd = utils.create_8021Q_vea_cmd(lpar_id=vios.lpar_id,
                                             slotnum=slotnum,
                                             port_vlan_id=port_vlan_id,
                                             addl_vlan_ids=addl_vlan_ids)
            self._pvmops.run_vios_command(cmd)

            # find out the new slot's devname. Even though DR slot add
            # operation will call cfgmgr on the VIOS, just invoke cfgdev
            # again on vio0 to make sure the device is discovered by VIOS.
            cmd = utils.get_newly_added_slot_name_cmd(lpar_id=vios.lpar_id,
                                                      mts=self._get_mts(),
                                                      slotnum=slotnum)

            vea_devname = self._pvmops.run_vios_command(cmd)[0]
            msg = (ras.vif_get_msg('info', 'VEA_DEV_NAME') %
                   {'vea_dev': vea_devname, 'cmd': cmd})
        except Exception as e:  # catch the hmc/vios command exception
            ras.trace(LOG, __name__, ras.TRACE_EXCEPTION,
                      ras.msg('error', 'VETH_NOTCFG') % {'slotnum': slotnum} +
                      "(" + (_('%s') % e) + ")")
            raise

        if not vea_devname:
            # failed to find the newly created veth slot on VIOS. clean it up.
            ras.trace(LOG, __name__, ras.TRACE_ERROR,
                      ras.msg('error', 'VETH_NOTCFG') % {'slotnum': slotnum})
            try:
                cmds = utils.remove_virtual_slot_cmd(lpar_id=vios.lpar_id,
                                                     slot_num=slotnum)
                self._pvmops.run_vios_command(cmds)

                ras.trace(LOG, __name__, ras.TRACE_DEBUG,
                          'Clean up: slot %(slotnum)d has been removed' %
                          {'slotnum': slotnum})

            except Exception:
                ras.trace(LOG, __name__, ras.TRACE_EXCEPTION,
                          ras.msg('error', 'VETH_FAILTOREMOVE') %
                          {'slotnum': slotnum} + "(" + _('%s') % e + ")")

            raise excp.IBMPowerVMFailToAddDataVlan(data_vlan_id=addl_vlan_ids)

        # If we got here, we succeeded!
        return vea_devname, None

    def _delete_vea_on_vios(self, vios, vea_dev, sea_dev, virt_adpts_list):
        """
        This method will remove the given VEA from the given SEA on the
        system and delete it.

        :param vios: VioServer DOM object representing VIOS this is being
                     removed from.
        :param vea_dev: VirtualEthernetAdapter DOM object
        :param sea_dev: SharedEthernetAdapter DOM object
        :param virt_adpts_list: List of VEA devnames that should remain on the
                                SEA
        """
        # Run the first two commands in a loop in case the rmdev fails.
        # Running the first command repeatedly isn't a problem because
        # it's setting the SEA to what we want, and that doesn't change.
        # If this fails, it'll be on the rmdev.
        for x in range(RMDEV_ATTEMPTS):
            try:
                cmds = \
                    (utils.change_sea_virt_adapters_cmd(seaname=sea_dev.name,
                                                        pveaname=sea_dev.
                                                        get_primary_vea().name,
                                                        virt_list=
                                                        virt_adpts_list)
                        + ' && ' +
                        utils.remove_virtual_device(vea_dev.name)
                        + ' && ' +
                        utils.remove_virtual_slot_cmd(lpar_id=vea_dev.
                                                      vio_server.lpar_id,
                                                      slot_num=vea_dev.slot))
                self._pvmops.run_vios_command(cmds)

                # If we got here, all is well
                break
            except Exception as e:
                ras.trace(LOG, __name__, ras.TRACE_ERROR,
                          ras.msg('error', 'VETH_FAILTOREMOVE') %
                          {'slotnum': vea_dev.slot} + '(' + _('%s') % e + ')')

                # Sleep before we try again
                if x < (RMDEV_ATTEMPTS - 1):
                    time.sleep(1)
                else:
                    # This was our last attempt.  Re-raise the exception.
                    raise

    def _supports_dynamic_update(self, vios=None):
        """
        IVM does not support dynamically updating VLAN ids on VEAs.

        :param vios: VioServer DOM object representing VIOS running against
        :returns: False always
        """
        return False

    def _update_sea(self, sea_dev, virt_adpts_list):
        """
        This method will update the passed in SEA to include all virtual
        adapters passed in.  NOTE: This does not touch the SEA's primary VEA.

        :param sea_dev: SharedEthernetAdapter DOM object representing SEA to
                        be updated on the system.
        :param virt_adpts_list: List of virtual adapters (VEAs) to be attached
                                to the SEA when done.
        """
        cmds = utils.change_sea_virt_adapters_cmd(seaname=sea_dev.name,
                                                  pveaname=sea_dev.
                                                  get_primary_vea().name,
                                                  virt_list=virt_adpts_list)

        self._pvmops.run_vios_command(cmds)

    def _find_first_avail_slot(self, vios):
        """
        This method will return the first available virtual slot on the VIOS.
        To be used when creating a VEA.

        :param vios: VioServer DOM object representing VIOS we're working with.
        :returns slotnum: Virtual slot number of the first available slot
        """
        ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG, "Enter")

        # find out all the available virtual adapters and their physloc
        cmds = utils.get_virt_adapters_with_physloc_cmd(vios.lpar_id)
        physloc_list = self._pvmops.run_vios_command(cmds)
        msg = (ras.vif_get_msg('info', 'PHYSC_LOC_LIST') %
               {'output': physloc_list})
        ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG, msg)

        # Output example:
        #['U9117.MMC.0604C17-V100-C2-T1',
        # .....]
        cmd = utils.get_curr_max_virtual_slots_cmd(vios.lpar_id)
        maxslots = int(self._pvmops.run_vios_command(cmd)[0])
        msg = (ras.vif_get_msg('info', 'MAX_SLOTS') %
               {'output': maxslots})
        ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG, msg)
        if not physloc_list:
            return
        used_slots = []

        #Need to delte column descriptions that command returns
        physloc_list.reverse()
        physloc_list.pop()

        #loop through all slots used
        for slot in physloc_list:
            if len(slot.strip()) == 0:
                continue
            # get the first column from output , adapter details
            slot = slot.split(' ')[0]
            snum = int(slot.split('-')[2].lstrip('C'))
            if snum < maxslots:
                used_slots.append(snum)
        used_slots.sort()
        # vslot 0-9 are reserved on IVM.
        for snum in range(10, maxslots):
            if snum not in used_slots:
                break
        if snum == (maxslots - 1):  # The - 1 is because 0 is a valid slot
            return
        else:
            msg = (ras.vif_get_msg('info', 'SLOT_NUM') %
                   {'snum': snum})
            ras.function_tracepoint(LOG, __name__, ras.TRACE_DEBUG, msg)
            return snum

    def _get_mts(self):
        """
        Connect to IVM and get the machine type/model/serial based on adapter
        information.

        :returns mts: String in the form of type.model.serial
        """
        if(not hasattr(self, 'powervm_mts') or
           len(self.powervm_mts.strip()) == 0):
            cmd = utils.get_all_sea_with_physloc_cmd()
            output = self._pvmops.run_vios_command(cmd)
            virt_eths = utils.parse_physloc_adapter_output(output)
            self.powervm_mts = utils.get_mts(virt_eths)
        return self.powervm_mts
