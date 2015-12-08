# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# =================================================================
# =================================================================

"""
IBMPowerVM VIF driver utilities
"""

import logging

from paxes_nova.virt.ibmpowervm.vif.common import ras

from paxes_nova import _

# maximum number of VEA allowed per SEA.
SEA_MAX_VEAS = 16
# maximum number of 8021Q tagged VLAN id supported on a single VEA
VEA_MAX_VLANS = 20

LOG = logging.getLogger(__name__)


def is_valid_vlan_id(vlan_id):
    """
    check whether vlan_id is in the range of [1,4094]

    :param vlan_id: vlan id to validate
    :returns: True or False
    """

    return True if vlan_id and (vlan_id > 0 and vlan_id < 4095) else False


def get_veth_list_by_vlan_id_cmd():
    """
    create the IVM command to retrieve the veth device by vlan id

    :return: The command to run against the IVM.
             The results will look like the following:
               1,12,3333,none,1,0
               1,13,4093,"201,207",1,1
             The result order is:
              - lpar_id
              - slot_num
              - port_vlan_id
              - is_trunk
              - ieee_virtual_eth
              - addl_vlan_ids
    """
    return ('lshwres -r virtualio --rsubtype eth --level lpar '
            ' -F lpar_id,slot_num,port_vlan_id,is_trunk,ieee_virtual_eth,'
            'addl_vlan_ids')


def get_lpar_id_list():
    """
    Creates an IVM command that will return a list of all the LPAR identifiers.
    It will include the VIO Server LPAR ID.  It may also have multiple lpars
    returned multiple times, so the results need to be pared down.

    :return: The result order is:
            1
            1
            1
            1
            2
    """
    return ('lshwres -r virtualio --rsubtype eth --level lpar -F lpar_id')


def get_veth_list_for_lpar(lpar_id):
    """
    Creates an IVM command that will return the information needed for a
    VM import operation.

    :param lpar_id: The LPAR Identifier to gather the information for.
    :return: The command to run against the IVM.
             The results will look like the following:
              1,1,06-04D7A,EEE91D8C7003,1,none
              2,1,06-04D7A,EEE91D8C7004,2,"201,207"
              3,1,06-04D7A,EEE91D8C7005,3,none
              4,1,06-04D7A,EEE91D8C7006,4,none

            The result order is:
             - slot_num
             - lpar_id
             - lpar_name
             - mac_addr
             - port_vlan_id
             - addl_vlan_ids
    """
    return ('lshwres -r virtualio --rsubtype eth --level lpar -F '
            'slot_num,lpar_id,lpar_name,mac_addr,port_vlan_id,addl_vlan_ids '
            '--filter "lpar_ids=%(lpar)s"' % {'lpar': lpar_id})


def get_slot_descriptions(lpar_id):
    """
    Returns a command that will show the descriptions of each slots device.

    Example output:
        RAID Controller
        Fibre Channel Serial Bus
        Fibre Channel Serial Bus
        Empty slot
        Empty slot
        Network and Computing en/decryption
        Empty slot
        Ethernet controller
    """
    return ('lshwres -r io --rsubtype slot -F description --filter '
            '"lpar_ids=%(lpar)s"' % {'lpar': lpar_id})


def get_vios_lpar_id_name():
    """
    Generates an IVM command that will return the LPAR ID and Name for the
    VIO Server.

    Output to this command will be:
     - '1 06-04C7A'
    The first space dictates the LPAR id.  The rest is the LPAR name.
    """
    return 'ioscli lslparinfo'


def get_virt_adapters_with_physloc_cmd(lpar_id):
    """
    Get all the available virtual adapters with physloc

    :returns: A VIOS command that can get virtual adapters with
              physloc.
    """

    return ('ioscli lsdev -slots |grep "\-V%s"' % lpar_id)


def create_8021Q_vea_cmd(lpar_id, slotnum, port_vlan_id, addl_vlan_ids):
    """
    Generate IVM command to create 8021Q virtual ethernet adapter.

    :param lpar_id: LPAR id
    :param slotnum: virtual adapter slot number
    :param port_vlan_id: untagged port vlan id
    :param addl_vlan_ids: tagged VLAN id list
    :returns: A HMC command that can create 8021Q veth based on the
              specification from input.
    """
    vids = [] if not addl_vlan_ids else addl_vlan_ids
    cmd = ('chhwres -r virtualio --rsubtype eth -o a -s %(slot)s '
           '--id %(lparid)s -a ieee_virtual_eth=1,'
           'port_vlan_id=%(pvid)s,is_trunk=1,'
           'trunk_priority=1,\\\"addl_vlan_ids=%(addlvids)s\\\"' %
           {'slot': slotnum,
            'lparid': lpar_id,
            'pvid': port_vlan_id,
            'addlvids': ",".join(str(x) for x in vids)})

    return cmd


def get_newly_added_slot_name_cmd(mts, lpar_id, slotnum):
    """
    run cfgdev of the newly configured virtual slot and return the
    VIOS device name for that slot.

    :param mts: PowerVM MTS string for virtual adapter
    :param lpar_id: LPAR id
    :param slotnum: virtual slot number
    :returns: A VIOS command that returns the device name based on physloc
    """
    return ('ioscli cfgdev -dev vio0 && ioscli lsdev -plc '
            '%(mts)s-V%(lparid)s-C%(slot)s-T1 -fmt : '
            ' -field name' %
            {'mts': mts,
             'lparid': lpar_id,
             'slot': slotnum})


def get_rmc_status():
    """
    Return the IVM command to get the RMC status on the system.
    """
    return 'lssyscfg -r lpar -F lpar_id,rmc_state'


def change_sea_virt_adapters_cmd(seaname, pveaname, virt_list):
    """
    Change the SEA device with the updated virt_list.

    :param seaname: SEA devname for chdev
    :param pveaname: pvid_adapter devname
    :param virt_list: virt_adapters list
    :returns: A VIOS command to change the attribute of a given SEA
    """
    virt_list = [] if not virt_list else virt_list

    if len(virt_list) > 0 and not isinstance(virt_list, list):
        raise TypeError(_('change_sea_virt_adapters_cmd(): virt_list'
                          ' is not list.'))

    additional_adapters = ""
    if len(virt_list) > 0:
        additional_adapters = ',' + ','.join(virt_list)

    return ('ioscli chdev -dev %(sea)s -attr '
            'virt_adapters=%(pvea)s%(virtlist)s '
            'pvid_adapter=%(pvea)s' %
            {'sea': seaname,
             'pvea': pveaname,
             'virtlist': additional_adapters})


def remove_virtual_device(devname):
    """
    Generate command to remove a virtual device

    :param devname: Virtual device name to remove
    :returns: Command to remove the virtual device
    """
    return ("ioscli rmdev -dev %s" % devname)


def remove_virtual_slot_cmd(lpar_id, slot_num):
    """
    Generate HMC command to remove virtual slot.

    :param lpar_id: LPAR id
    :param slot_num: virtual adapter slot number
    :returns: A HMC command to remove the virtual slot.
    """

    return ("chhwres -r virtualio --rsubtype eth -o r -s %(slot)s "
            "--id %(lparid)s" %
            {'slot': slot_num, 'lparid': lpar_id})


def create_non_8021Q_vea_cmd(lpar_id, slot_num, port_vlan_id):
    """
    Generate HMC command to create a non-8021Q virtual ethernet adapter

    :param lpar_id: LPAR id
    :param slot_num: virtual adapter slot number
    :param port_vlan_id: untagged port vlan id
    :returns: A HMC command to create a non 8021Q veth adapter based on
              the specification from input.
    """
    return ("chhwres -r virtualio --rsubtype eth -o a -s %(slot)s "
            "--id %(lparid)s -a ieee_virtual_eth=0,port_vlan_id=%(pvid)s,"
            "is_trunk=1,trunk_priority=1" %
            {'slot': slot_num, 'lparid': lpar_id, 'pvid': port_vlan_id})


def get_mgmt_interface_devname_cmd(ip_addr):
    """
    Generate the VIOS command to find VIOS/IVM management interface
    device name. Be aware, ioscli lstcpip -interface will be a slow
    command (about 1 second per configured interface). The alternative
    is to use lstcpip -stored to do AIX config database query only
    which speed things up dramatically since it won't open/close
    the interface.

    :param ip_addr: management interface ip address
    :returns: A VIOS command to find the SEA that has management host ip
              address configured.
    """
    return ('ioscli lstcpip -stored | grep -p -w \"%(ip)s\" | grep -p -w '
            '\"State = up\"' % {'ip': ip_addr})


def get_all_sea_with_physloc_cmd():
    """
    Get all the available SEA configured on VIOS and their physloc

    :returns: A VIOS command to get all the virtual adapters (SEAs and VEAs)
              configured on VIOS and their physloc.  Note, this will even
              return adapters in the "Defined" state.
    """
    return ("ioscli lsdev -virtual -type adapter -fmt : -field name "
            "description physloc|grep '^ent' ")


def parse_physloc_adapter_output(output):
    """
    Parses the physical location command output from an IVM command.

    :param output: The output from an IVM physical loction command
    :returns: The output formatted into a dictionary
    """
    # Output example:
    # ['ent4:Virtual I/O Ethernet Adapter (l-lan):
    #  U9117.MMC.0604C17-V100-C2-T1',
    # 'ent10:Virtual I/O Ethernet Adapter (l-lan):
    # U9117.MMC.0604C17-V100-C10-T1',
    # 'ent13:Virtual I/O Ethernet Adapter (l-lan):
    # U9117.MMC.0604C17-V100-C13-T1',
    # 'ent14:Virtual I/O Ethernet Adapter (l-lan):
    # U9117.MMC.0604C17-V100-C14-T1',
    # 'ent5:Shared Ethernet Adapter: ',
    # 'ent11:Shared Ethernet Adapter: ',
    # 'ent12:VLAN: ']

    if len(output) == 0:
        return False

    # convert output to a dictionary with eth key.
    # Basically, it breaks each line( VEA or SEA) into two part.
    # 1. the devname, which is used as the key for dict virt_eths.
    # 2. The content of each dict element will be a list with
    #    [<description>, <physloc>].
    virt_eths = {}
    for item in output:
        virt_eths[item.split(':')[0]] = item.split(':')[1:]
    return virt_eths


def parse_rmc_status(output, lpar_id):
    """
    Parses the output from the get_rmc_status() command to return the RMC
    state for the given lpar_id.

    :param output: Output from IVM command from get_rmc_status()
    :param lpar_id: LPAR ID to find the RMC status of.
    :returns status: RMC status for the given LPAR.  'inactive' if lpar_id
                     not found.
    """
    # Output example:
    # 1,active
    # 2,none
    # 3,none
    # 4,none
    # 5,none

    # Be sure we got output to parse
    if not output:
        return 'inactive'

    # Inspect each line
    for item in output:
        # If lpar_id matches this line's lpar_id, return that status
        if item.split(',')[0] == str(lpar_id):
            return item.split(',')[1]

    # If we got here, we didn't find the lpar_id asked for.
    return 'inactive'


def get_mts(virt_eths):
    """
    Parse the machine type/model/serial from the IVM output.

    :param virt_eths: A dictionary with adapter data from IVM
    :returns: A string in the form "type.model.serial"
    """
    for key in virt_eths.keys():
        if 'Virtual I/O Ethernet Adapter (l-lan)' in virt_eths[key][0]:
            return virt_eths[key][1].split('-')[0]
    return None


def parse_slot_num(virt_eths, adapter_name):
    """
    Parse the adapter slot data from the IVM output.

    :param virt_eths: A dictionary with adapter data from IVM
    :param adapter_name: The name of a SEA or VEA
    :returns: An integer of the slot the adapter occupies
    """
    try:
        physloc = virt_eths[adapter_name][1].strip()
    except KeyError:
        msg = (ras.vif_get_msg('error', 'VIOS_SEAINVALIDLOCS') %
               {'devname': adapter_name})
        ras.function_tracepoint(LOG, __name__, ras.TRACE_ERROR,
                                msg)
        return None

    return int(physloc.split('-')[2].lstrip('C'))


def get_sea_attribute_cmd(seaname):
    """
    Get pvid, pvid_adapter, and virt_adapters from the configured SEA device.
    Also get the state of the SEA.

    :param seaname: sea device name
    :returns: A VIOS command to get the sea adapter's attributes.
    """
    return ("ioscli lsdev -dev %(sea)s -attr pvid,pvid_adapter,virt_adapters;"
            "ioscli lsdev -type sea | grep %(sea)s" %
            {'sea': seaname})


def get_veth_slot_info_cmd(lpar_id, slotnum):
    """
    get virtual ethernet slot information
    For IVM, vswitch field is not supported.

    :param lpar_id: LPAR id
    :param slotnum: veth slot number
    :returns: A HMC command to get the virtual ethernet adapter information.
    """
    return ("lshwres -r virtualio --rsubtype eth --level "
            "lpar --filter lpar_ids=%(lparid)s,slots=%(slot)s "
            "-F is_trunk,trunk_priority,ieee_virtual_eth,"
            "port_vlan_id,addl_vlan_ids" %
            {'lparid': lpar_id, 'slot': slotnum})


def get_curr_max_virtual_slots_cmd(lparid=1):
    """
    Get the current max virtual slots limit for the LPAR

    :param lparid: LPAR ID. For IVM this parameter is not needed
    :returns: A HMC command to get the maximum number of virtual slots
              configurable on the VIOS.
    """
    return ("lshwres -r virtualio --rsubtype slot --level lpar "
            "--filter lpar_ids=%(lparid)s -F curr_max_virtual_slots" %
            {'lparid': lparid})


def get_cur_active_vids_cmd(lpar_id=0):
    """
    Returns a command to list all the PVIDs and 8021Q vids currently on
    the LPAR.  Command output will be a list of VLANs, with each
    new line being a new VLAN. There may be a blank line at the end of the
    command, which can be ignored

    :param lparid: The LPAR ID to request against
    :returns: A HMC command that returns a list of virtual ethernet adapter
              that currently has the given VLAN id bridged.
    """
    if lpar_id == 0:
        #return all the active vids on the system
        return ("lshwres -r virtualio --rsubtype eth --level lpar "
                "-F port_vlan_id,addl_vlan_ids")
    else:
        # Only return the active vids for the LPAR.
        return ("lshwres -r virtualio --rsubtype eth --level lpar --filter "
                "lpar_ids=%(lparid)s -F port_vlan_id,addl_vlan_ids" %
                {'lparid': lpar_id})
