#
# =================================================================
# =================================================================

import ConfigParser
import StringIO
import commands
import copy
import json
from nova import utils
from nova.openstack.common import lockutils
from nova.openstack.common import log as logging
from nova.openstack.common import processutils
from os import SEEK_SET
import os
from oslo.config import cfg
import re
import tempfile

from powervc_nova import _
from powervc_nova.network.powerkvm import agent
from powervc_nova.network.powerkvm.agent.common import exception as exp
from powervc_nova.objects.network import dom_kvm


LOG = logging.getLogger(__name__)

CONF = cfg.CONF
CONF.import_opt('integration_bridge', 'powervc_nova.network.powerkvm.agent')
CONF.import_opt('management_ip', 'powervc_nova.network.powerkvm.agent')
ADAPTER_REGEX = '([a-zA-Z0-9!@#$%^&\*_\-\[\]\{\}?\(\)]+)'

# Neutron only supports vSwitches with a certain number of characters.  Should
# ignore any of longer length
MAX_VSWITCH_CHARS = 8

CONF.register_opts([
    cfg.IntOpt('max_mgmt_adpt_ping_tries', default=15,
               help='Max attempts for ping request to management adapter')
])


class CommandExecutor():

    """
    cached value for lshw data
    """
    lshwCachedData = None

    @staticmethod
    @lockutils.synchronized('powerkvm_ovslshw', 'nova-ovslshw-powerkvm-')
    def send_lshw_command():
        """
        This method is used to send the lshw command. The command
        is sent to return a POSIX output. Call the parser method to
        parse the output and eventually send it back to the caller.
        No filtering is done at this level of the code.

        :return : [{'name': 'eth0', 'speed': '10Gbit/s'},
                {'name': 'eth3', 'speed': '10Gbit/s'},
                {'name': 'virbr1-nic', 'speed': '10Mbit/s'},
                {'name': 'virbr0-nic', 'speed': '10Mbit/s'}]

        """
        lshwout = ""
        try:
            lshwout, stderr = utils.execute('lshw', '-class', 'network',
                                            run_as_root=True)
            if stderr and stderr != "":
                raise exp.IBMPowerKVMCommandExecError(command="lshw",
                                                      exp=stderr)
        except OSError as ex:
            LOG.error(ex)
        except Exception as e:
            LOG.error(e)
            raise
        # TODO: If an stderr is encoutered throw an exception.
        return CommandParser.parse_lshw(lshwout)

    @staticmethod
    def send_bond_info_command():
        """
        This method is used to retrieve the bond information. We don't have a
        direct of determining this and hence a set of commands are sent.

        :return : A parsed list of bond data
        """
        bond_data_dict = {}
        bonds = ""
        stderr = 'Unknown'
        try:
            directory = "/proc/net/bonding"
            file_exists = _check_file_exists(directory)
            if file_exists:
                bonds = _list_directory(directory)
            LOG.debug("The bonds are: %s", bonds)
        except Exception as e:
            bonds = None
            LOG.error(e)
        try:
            if bonds:
                # It means there are bonds.
                for bond in bonds:
                    if bond != "":
                        comm = "/proc/net/bonding/" + bond
                        bondinfo, stderr = utils.execute('cat', comm,
                                                         run_as_root=True)
                        # parse the command output but not here. Call the
                        # parser
                        slave_list = CommandParser.parse_bond_data(bondinfo)
                        bond_data_dict[bond] = slave_list
                        if stderr and stderr != "":
                            raise exp.IBMPowerKVMCommandExecError(cmd="bond",
                                                                  exp=stderr)
                        LOG.debug("Bond info for %s is: %s", bond, bondinfo)
        except OSError as ex:
            LOG.error(ex)
            bond_data_dict = ""
        except Exception as e:
            # LOG and move on
            LOG.error(_("There was a problem in executing cat %(command)s "
                      " The system returned the following error: %(error)s") %
                      dict(command=comm, error=stderr))
            LOG.error(e)
            raise
        return bond_data_dict

    @staticmethod
    def get_lshw_results():
        """
        return cached lshw data

        :return: lshwCachedData
        """
        if CommandExecutor.lshwCachedData is None:
            # If its none, we should run it to gather the data.
            data = CommandExecutor.send_lshw_command()
            CommandExecutor.lshwCachedData = data
        return CommandExecutor.lshwCachedData

    @staticmethod
    def set_lshw_results(value):
        """
        set lshw cached data
        """
        CommandExecutor.lshwCachedData = value

    @staticmethod
    def send_vsctl_command():
        """
        This method is used to send the ovs-vsctl command and get the parsed
        output

        :return: parsed dictionary of the object of the vsctl command
        {'br0': {'br0': {'interface': ['eth0'], 'type': 'internal'},
                '"9"': {'interface': ['"9"']},
                '"br1"': {'interface': ['"br1"', '"arbit"'],
                                   'type': 'internal'}}}
        """
        vsctlout = ""
        try:
            vsctlout, stderr = utils.execute('ovs-vsctl', 'show',
                                             run_as_root=True)
            if stderr and stderr != "":
                raise exp.IBMPowerKVMCommandExecError(cmd="ovs-vsctl",
                                                      exp=stderr)
        except OSError as ex:
            LOG.error(ex)
        except Exception as e:
            LOG.error(e)
            raise
        LOG.debug("ovs-vsctl show output = %s", vsctlout)
        return CommandParser.parse_ovs_vsctl_show(vsctlout)

    @staticmethod
    def send_ifconfig_command():
        """
        This method is used to send the ifconfig -a command.

        :return: parsed list of dictionary of the ifconfig output
                    [{'name': 'eth0', 'state': 'UP',
                     'ipv4': '9.79.214.43', 'ipv6': ''},
                    {'name': 'eth3', 'state': 'UP',
                     'ipv4': '127.0.0.1', 'ipv6': ''},
                    {'name': 'wc0', 'state': 'UP',
                     'ipv4': '9.79.214.43', 'ipv6': ''}]
        """
        ifcfgout = ""
        try:
            ifcfgout, stderr = utils.execute('ifconfig', '-a',
                                             run_as_root=True)
            if stderr and stderr != "":
                raise exp.IBMPowerKVMCommandExecError(cmd="ifconfig",
                                                      exp=stderr)
        except OSError as ex:
            LOG.error(ex)
        except Exception as e:
            LOG.error(e)
            raise
        LOG.debug("ifconfig output = %s", ifcfgout)
        return CommandParser.parse_ifconfig(ifcfgout)

    @staticmethod
    def send_ip_address_command():
        """
        This method is used to send the ifconfig -a command.

        :return: parsed list of dictionary of the ifconfig output
                    [{'name': 'eth0', 'state': 'UP',
                     'ipv4': '9.79.214.43', 'ipv6': ''},
                    {'name': 'eth3', 'state': 'UP',
                     'ipv4': '127.0.0.1', 'ipv6': ''},
                    {'name': 'wc0', 'state': 'UP',
                     'ipv4': '9.79.214.43', 'ipv6': ''}]
        """
        ip_address_out = ""
        try:
            ip_address_out, stderr = utils.execute('ip', 'address',
                                                   run_as_root=True)
            if stderr and stderr != "":
                raise exp.IBMPowerKVMCommandExecError(cmd="ip address",
                                                      exp=stderr)
        except OSError as ex:
            LOG.error(ex)
        except Exception as e:
            LOG.error(e)
            raise
        LOG.debug("ip address command output = %s", ip_address_out)
        return CommandParser.parse_ip_address_command(ip_address_out)

    @staticmethod
    def send_brctl_command():
        """
        This method is used to send the brctl command

        :return: a dict with the brctl output
          {
            'bridges': [
                {
                    'interfaces': [
                        {
                            'name': 'virbr0-nic'
                        },
                        {
                            'name': 'test-nic'
                        },
                        {
                            'name': 'test-nic2'
                        }
                    ],
                    'bridge_name': 'virbr0'
                },
                {
                    'interfaces': [
                        {
                            'name': 'virbr1-nic'
                        }
                    ],
                    'bridge_name': 'virbr1'
                },
                {
                    'interfaces': [],
                    'bridge_name': 'virbr2'
                }
            ]
        }
        """
        try:
            brctlout, stderr = utils.execute('brctl', 'show',
                                             run_as_root=True)
            if stderr and stderr != "":
                raise exp.IBMPowerKVMCommandExecError(cmd="brctl",
                                                      exp=stderr)
        except Exception as e:
            LOG.error(e)
            raise
        LOG.debug("brctl output = %s" % brctlout)
        return CommandParser.parse_brctl(brctlout)

    @staticmethod
    def run_vsctl(args, check_error=False):
        """
        This is a utility method to run ovs-vsctl commands. This could be used
        to do various operations that involve ovs-vsctl. Note we can also pass
        a json format for outputs - so use it per your discretion
        :param args: A list of arguments that can be sent to the ovs-vsctl com
        for execution.
        :param check_error: This should be set to True, if you would like the
        exception to be propagated.
        :return stdout: A standard output after running the command
        """
        full_args = ["ovs-vsctl", "--timeout=2"] + args
        try:
            stdout, stderr = utils.execute(*full_args,
                                           run_as_root=True)
            if stderr and stderr != "":
                raise exp.IBMPowerKVMCommandExecError(cmd="ovs-vsctl",
                                                      exp=stderr)
        except Exception as e:
            LOG.error(_("Unable to execute %(cmd)s. Exception: %(exception)s"),
                      {'cmd': full_args, 'exception': e})
            if check_error:
                raise
        return stdout

    @staticmethod
    def run_ping(target, adapter=None):
        """
        This is a utility method to run ping command. Will run exactly one
        ping to the target and return whether or not it was successful.
        :param target: The host to run the ping against
        :param adapter: Defaults to None (optional).  If specified, the
                        adapter to run the ping through.
        :return success: True if ping worked.  False if it is not routable.
        """
        full_args = "ping"
        if adapter is not None:
            full_args = full_args + " -I " + adapter
        full_args = full_args + " -c 1 -W 1 " + target
        try:
            status, output = commands.getstatusoutput(full_args)
            if status != 0:
                LOG.error(_("Ping failed with return code %(rc)s and stdout: "
                            "%(stdout)s"), {'rc': status, 'stdout': output})
            return status == 0
        except Exception as e:
            # Some other fundamental error occurred
            LOG.error(_("Unable to execute %(cmd)s. Exception: %(exception)s"),
                      {'cmd': full_args, 'exception': e})
            return False

    @staticmethod
    def get_ifcfg(device_name):
        """
        This method is used to get the contents of a networking ifcfg file
        for a specified device.

        :param device_name: The networking device to get the ifcfg for.
        :return: The fully qualified path of the ifcfg file.
        :return: a dict with the key/value pairs from the ifcfg file.
        """
        LOG.debug('searching for ifcfg file for %s' % device_name)
        try:
            fpath, ifcfg_dict = CommandExecutor._find_ifcfg_data(device_name)
            return fpath, ifcfg_dict

        except Exception as e:
            LOG.exception(e)
            raise
        LOG.debug('ifcfg file not found for %s' % device_name)
        return None, None

    @staticmethod
    def send_ifcfg(device_name, device_dict):
        """
        This method is used to update the contents of a networking ifcfg file
        for a specified device.

        :param device_name: The networking device to update the ifcfg for.
        :param device_dict: a dict with the key/value pairs for the ifcfg file.
        :returns fnamepath: The path and name of the file data was written to.
        """
        try:
            fname = CommandExecutor._find_ifcfg_data(device_name)[0]
            if not fname:
                fname = CommandExecutor._generate_ifcfg_name(device_name)
            lines = []
            for key in device_dict.keys():
                lines.append(key + '=' + device_dict[key] + '\n')

            # Write the output as root
            _write_to_file_as_root(fname, lines)

            return fname
        except Exception as e:
            LOG.error(e)
            raise
        return None

    @staticmethod
    def _find_ifcfg_data(device_name):
        """
        Look up the ifcfg file name and contents for a specified device_name.

        :param device_name: A device name to look up such as eth0.
        :return: The fully qualified path of the ifcfg file.
        :return: A dictionary with the parsed file contents.
        """
        ifcfg_dir = '/etc/sysconfig/network-scripts'

        # The device_name is usually in the ifcfg file name, so check for this
        # case first.
        fname = '%s/ifcfg-%s' % (ifcfg_dir, device_name)
        if _check_file_exists(fname):
            stdout = utils.execute('cat', fname, run_as_root=True)[0]
            ifcfg_dict = CommandParser.parse_ifcfg(device_name, stdout)

            # make sure there are no quote around the device name we
            # get from the ifcfg file
            ifcfg_dev_name = \
                re.sub(r'^"|"$', '', ifcfg_dict.get(agent.IFCFG_DEVICE, ''))
            if ifcfg_dev_name == device_name:
                LOG.debug('found ifcfg file in expected location.')
                return fname, ifcfg_dict

        # The ifcfg with the desired device name either does not exist, or
        # contains a different internal device name.  Search all ifcfg files
        # looking for the correct device.
        ifcfg_files = [f for f in _list_directory(ifcfg_dir)
                       if _check_file_exists('%s/%s' % (ifcfg_dir, f))]
        for ifcfg_file in ifcfg_files:
            fname = '%s/%s' % (ifcfg_dir, ifcfg_file)
            stdout = utils.execute('cat', fname, run_as_root=True)[0]
            ifcfg_dict = CommandParser.parse_ifcfg(device_name, stdout)

            # make sure there are no quote around the device name we
            # get from the ifcfg file
            ifcfg_dev_name = \
                re.sub(r'^"|"$', '', ifcfg_dict.get(agent.IFCFG_DEVICE, ''))
            if ifcfg_dev_name == device_name:
                LOG.debug('found ifcfg data in file %s.' % ifcfg_file)
                return fname, ifcfg_dict
        return None, {}

    @staticmethod
    def _generate_ifcfg_name(device_name):
        """
        Generate an ifcfg name for the specified device.  This method addresses
        the following case:

        -> host-ovs API caller wants to update eth0 config.
        -> ifcfg-eth0 exists, but has Device set to eth1.
        -> This method will generate ifcfg-eth0-1, or the next available file
           name.

        :param device_name: A device name to generate a file name for.
        :return: The fully qualified path of the ifcfg file.
        """
        # Check for simple case
        ifcfg_dir = '/etc/sysconfig/network-scripts'
        desired_fname = '%s/ifcfg-%s' % (ifcfg_dir, device_name)
        if not _check_file_exists(desired_fname):
            return desired_fname

        # Begin generating names, find first one that's not in use
        for i in range(0, 100000):
            fname = desired_fname + '-' + str(i)
            if not _check_file_exists(fname):
                return fname

        # This should not happen ?
        return None

    @staticmethod
    def get_all_ifcfg_files_for_logging():
        """
        Get the contents of all ifcfg files.  These contents are generally
        needed to debug KVM networking reconfiguration problems.

        :returns: All files as a combined string.
        """
        ifcfg_dir = '/etc/sysconfig/network-scripts'
        ifcfg_files = [f for f in _list_directory(ifcfg_dir)
                       if _check_file_exists('%s/%s' % (ifcfg_dir, f))]
        return_string = ''
        for ifcfg_file in ifcfg_files:
            fname = '%s/%s' % (ifcfg_dir, ifcfg_file)
            stdout = utils.execute('cat', fname, run_as_root=True)[0]
            ifcfg_dict = CommandParser.parse_ifcfg('arbitrary_string', stdout)
            if ifcfg_dict and len(ifcfg_dict):
                return_string = return_string + '\n' + \
                    (_('Contents of %s: %s') %
                     (ifcfg_file, json.dumps(ifcfg_dict, sort_keys=True,
                                             indent=4)))
        return return_string

    @staticmethod
    @lockutils.synchronized('powerkvm_ovslshw', 'nova-ovslshw-powerkvm-')
    def send_network_restart():
        """
        This method is used to restart the networking on the host.
        """
        try:
            stdout = utils.execute('systemctl', 'restart', 'network.service',
                                   run_as_root=True)[0]
            LOG.debug("service network restart output = %s" % stdout)
        except Exception as e:
            LOG.exception(e)

    @staticmethod
    def restart_neutron_agent():
        """
        This method is used to restart the neutron agent on the host.
        """
        try:
            stdout = utils.execute('systemctl', 'restart',
                                   'neutron-openvswitch-agent.service',
                                   run_as_root=True)[0]
            LOG.debug("systemctl restart neutron-openvswitch-agent "
                      "output = %s" % stdout)
        except Exception as e:
            LOG.exception(e)


class CommandParser():
    """
    This class is used to parse various commands in dictionary or list formats
    This class could be made a private class, if we don't want this to be used
    from outside this module.
    """

    @staticmethod
    def parse_ovs_vsctl_show(ovs_vsctl_output):
        """
        Parses OpenVSwitch's ovs-vsctl command output to find network bridges.

        :param ovs_vsctl_output: A string with the raw ovs-vsctl command
        output.
        :return dict_list: {'"br0"':
                            {' "br0"':
                                {'interface': ['"br0"'], 'type': 'internal'}
                            }
                        '}
        """
        if ovs_vsctl_output is None or ovs_vsctl_output == "":
            raise exp.IBMPowerKVMInvalidVSCTLOutput()
        i = 0
        bridge_dict = {}
        # Formulate the bridge dictionary to contain the bridge status for
        # vsctl
        for bridge_chunk in ovs_vsctl_output.split('\n    Bridge '):
            if bridge_chunk.strip() == "":
                continue
            if i == 0:
                i = i + 1
                continue
            j = 0
            #Let'stripe off the escape characters if used
            bridge_name = bridge_chunk.split("\n")[0].strip().strip('""')
            port_dict = {}
            for port_chunk in bridge_chunk.split('\n        Port '):
                # In that sub-chunk we would want to split it using the
                # port.
                if j != 0:
                    split_port_list = port_chunk.split('\n')
                    # get the port name out from the first element of the
                    # list
                    port_name = split_port_list[0].strip().strip('""')
                    interface_list = []
                    interface_dict = {}
                    # Next we get the items off the split port chunk
                    for item in split_port_list:
                        if "            Interface " in item:
                            iface = item.replace('            Interface ',
                                                 '', 1).strip().strip('""')
                            #iface = item.split('  Interface ')[1].strip('""')
                            interface_list.append(iface)
                        if "type" in item:
                            interface_dict['type'] = item.split()[1]
                    interface_dict['interface'] = interface_list
                    port_dict[port_name] = interface_dict
                j = j + 1
            bridge_dict[bridge_name] = port_dict
            i = i + 1
        return bridge_dict

    @staticmethod
    def parse_bond_data(bonddata):
        """
        Parse the bond related data for each bond found in the system.

        :return: slave_list: [{'state': 'up', 'speed': 10000, 'name': 'eth0'},
        {'state': 'up', 'speed': 10000, 'name': 'eth1'}]

        """
        # split the bond data into paragraphs
        if bonddata is None or bonddata == "":
            raise exp.IBMPowerKVMInvalidBondOutput()
        slave_list = []
        # Similar explanation of the regex construct is described in the
        # ifconfig parser.
        reg_slv = 'Slave Interface:[ ]*' + ADAPTER_REGEX
        slave_int_re = re.compile(reg_slv)
        slave_speed_re = re.compile('Speed:[ ]*([a-zA-Z0-9]+)')
        slave_state_re = re.compile('MII Status:[ ]*([a-z]+)')
        for para in bonddata.split("\n\n"):
            if "Slave Interface" in para:
                slave_name = ""
                slave_speed = 0
                match_slave_name = slave_int_re.search(para)
                match_slave_speed = slave_speed_re.search(para)
                match_slave_state = slave_state_re.search(para)
                if match_slave_name:
                    slave_name = match_slave_name.group(1)
                if match_slave_speed:
                    slave_speed = match_slave_speed.group(1)
                if match_slave_state:
                    slave_state = match_slave_state.group(1)
                # We don't want to create objects here.
                if slave_name and slave_state and slave_speed:
                    slave_info_dict = {}
                    slave_info_dict['name'] = slave_name
                    try:
                        slave_info_dict['speed'] = int(slave_speed)
                    except Exception as e:
                        LOG.debug(e)
                        slave_info_dict['speed'] = 0
                    slave_info_dict['state'] = slave_state
                    slave_list.append(slave_info_dict)
        return slave_list

    @staticmethod
    def parse_ip_address_command(ip_address_output):
        """
        This method replaces the ifconfig parser and parses the data out of the
        "ip address" command instead.

        :param ip_address_output: The output from the ip address command.
        :return dict_list: A list of dictionary object:
        """
        if ip_address_output is None or ip_address_output == "":
            raise exp.IBMPowerKVMInvalidIPAddressOutput()
        dict_list = []
        ipv4_value = None
        ipv6_value = None
        regex_dev = '[0-9]+:[ ]+' + ADAPTER_REGEX
        device_re = re.compile(regex_dev)
        ipv4re = re.compile('inet [ ]*([0-9\.]+)')
        ipv6re = re.compile('inet6[ ]*([0-9a-z:]+)')
        interface_name = None

        for line in ip_address_output.split('\n'):
            match_interface = device_re.search(line)
            if match_interface:
                old_interface_name = interface_name
                interface_name = match_interface.group(1)
            match_ipv4 = ipv4re.search(line)
            if match_ipv4:
                ipv4_value = match_ipv4.group(1)
            match_ipv6 = ipv6re.search(line)
            if match_ipv6:
                ipv6_value = match_ipv6.group(1)

            # If we match on interface, then we are wrapped around
            if match_interface and old_interface_name is not None:
                dict_list.append({'name': old_interface_name,
                                  'ipv4': ipv4_value,
                                  'ipv6': ipv6_value})
                ipv4_value = None
                ipv6_value = None

        # last time through assume an add
        dict_list.append({'name': interface_name, 'ipv4': ipv4_value,
                          'ipv6': ipv6_value})

        return dict_list

    @staticmethod
    def parse_ifconfig(ifconfig_output):
        """
        This method is used to parse the ifconfig output.

        :param ifconfig_output: A string containing the inconfig shell output
        :return dict_list: A list of dictionary object:
            {'interface': 'eth0', 'state': 'UP', 'ipv4': '', 'ipv6': ''}
        """
        # TBD: This could be better done with regular expression, so can be mod
        if ifconfig_output is None or ifconfig_output == "":
            raise exp.IBMPowerKVMInvalidIFCONFIGOutput()
        dict_list = []
        # This is how the regular expr can be decoded. Search for a pattern
        # in the string that starts with 'inet addr' followed by 0 or more " "
        # this is denoted by [ ]*. Followed by an evaluation statement that
        # starts with ( and says this could be any number between 0 to 9
        # followed by a . (\. - escape character used) that can repeat for one
        # or more time (denoted by +). The value inside the "()" denotes the
        # expression whose value would be found by the search command.
        ipv4re = re.compile('inet [ ]*([0-9\.]+)')
        # Same as above, only that changes here is a use of albhabets with the
        # integers. So [0-9a-z:]+ means, the value can be any digit combination
        # with letter combination, followed by a : and this can repeat for 1 or
        # more times.
        ipv6re = re.compile('inet6 addr:[ ]*([0-9a-z:]+)')
        # Take the interface chunk from the ifconfig output and compile the
        # ipv4 and ipv6 addresses out of it.
        for interface_chunk in ifconfig_output.split('\n\n'):
            list_int_prop = interface_chunk.split('\n')
            ipv4_value = ""
            ipv6_value = ""
            for line in list_int_prop:
                match_ipv4 = ipv4re.search(line)
                if match_ipv4:
                    ipv4_value = match_ipv4.group(1)
                match_ipv6 = ipv6re.search(line)
                if match_ipv6:
                    ipv6_value = match_ipv6.group(1)
            interface_name = list_int_prop[0].split()[0]
            # We formulate a dictionay that fits into our scheme of things
            # going forward
            if interface_name:
                dict_list.append({'name': interface_name, 'ipv4': ipv4_value,
                                  'ipv6': ipv6_value})
        return dict_list

    @staticmethod
    def parse_lshw(lshwout):
        """
        This method parses the lshw output and only returns a list of those
        devices which are of type 'Ethernet device'.

        :param lshwout: Raw output after running the lshw command
        :return list_lshw:
            [{'speed': '10Gbit/s', 'interface': ' eth2'}]
        """
        if lshwout is None or lshwout == "":
            raise exp.IBMPowerKVMInvalidLSHWOutput()
        # Similar explanation for the regex is provided in the ifconfig parser
        regex = 'logical name:[ ]*' + ADAPTER_REGEX
        ethcardre = re.compile(regex)
        speedre = re.compile('speed=([a-z0-9A-Z/]+)')
        linkre = re.compile('link=([a-z/]+)')
        capacityre = re.compile('capacity:[ ]*([a-z0-9A-Z]+)')
        list_lshw = []
        # Let's devide the output into paragraphs. These are decided by
        # parsing the output based of the network pattern as below.
        for network_chunk in lshwout.split('*-network'):
            # Not all the devices which are part of lshw are physical ethernet
            # cards, hence we filter them out on the basis of the tag below.
            if network_chunk.strip(" ") == "":
                continue
            if "Ethernet" in network_chunk and "pci" in network_chunk:
                speed_mb = None
                capacity_mb = None
                name = ""
                state = ""
                for line in network_chunk.split("\n"):
                    match_eth = ethcardre.search(line)
                    if match_eth:
                        name = match_eth.group(1)

                    # link is the state of the adapter
                    match_link = linkre.search(line)
                    if match_link:
                        state_data = match_link.group(1)
                        if state_data == "yes":
                            state = "Available"
                        else:
                            state = "Unavailable"

                    # Speed is the current negotiated line rate.  Only
                    # available when plugged in.
                    match_speed = speedre.search(line)
                    if match_speed:
                        speed = match_speed.group(1)
                        if "G" in speed:
                            speed_mb = int(speed.split('G')[0]) * 1000
                        elif "M" in speed:
                            speed_mb = speed.split('M')[0]
                        else:
                            speed_mb = None

                    # Capacity is the speed that this port is capable of.
                    # Should only be used when the link is not up (ex. No
                    # Speed).
                    match_capacity = capacityre.search(line)
                    if match_capacity:
                        capacity = match_capacity.group(1)
                        if "G" in capacity:
                            capacity_mb = int(capacity.split('G')[0]) * 1000
                        elif "M" in capacity:
                            capacity_mb = capacity.split('M')[0]
                        else:
                            capacity_mb = None

                # It's not good enough to report a ethernet device without the
                # speed or name. A bridge can also be called "Ethernet
                # Interface which has a eth device added to that. But that
                # is not necessarily what we would want to report here.
                if name:
                    # The overall speed is determined by a mix of the capacity
                    # and speed.  Speed should be preferred.
                    overall_speed = speed_mb
                    if not speed_mb:
                        overall_speed = capacity_mb
                    if not overall_speed:
                        overall_speed = 0

                    list_lshw.append({'name': name.strip(),
                                      'speed': overall_speed,
                                      'state': state})
        return list_lshw

    @staticmethod
    def parse_brctl(brctlout):
        """
        This method parses the brctl output.

        :return bridgeDict:
        {
            'bridges': [
                {
                    'interfaces': [
                        {
                            'name': 'virbr0-nic'
                        },
                        {
                            'name': 'test-nic'
                        },
                        {
                            'name': 'test-nic2'
                        }
                    ],
                    'bridge_name': 'virbr0'
                },
                {
                    'interfaces': [
                        {
                            'name': 'virbr1-nic'
                        }
                    ],
                    'bridge_name': 'virbr1'
                },
                {
                    'interfaces': [],
                    'bridge_name': 'virbr2'
                }
            ]
        }
        """
        if brctlout is None or brctlout == "":
            raise exp.IBMPowerKVMInvalidBRCTLOutput()
        ROOT_NAME = "bridges"
        BRIDGE_NAME = "bridge_name"
        INTERFACES = "interfaces"
        INTERFACE_NAME = "name"

        outputList = brctlout.splitlines()
        bridgeDict = {"bridges": []}
        currBridgeDict = None

        # skip first line since it is just column headings
        for i in range(1, len(outputList)):
            LOG.debug("current brctl entry = %s" % outputList[i])
            splitStr = outputList[i].split()

            # determine if this is an entry for a bridge
            # or an entry containing only an interface for
            # the previously listed bridge entry
            if len(splitStr) > 1:
                if currBridgeDict is not None:
                    bridgeDict[ROOT_NAME].append(currBridgeDict)

                # reinitialize current bridge dictionary
                currBridgeDict = dict()

                # set bridge name, need to preserve until next
                # iteration since next line of output could be
                # another interface for this bridge
                currBridgeDict[BRIDGE_NAME] = splitStr[0]
                if len(splitStr) == 4:
                    currBridgeDict[INTERFACES] = \
                        [{INTERFACE_NAME: splitStr[3]}]
                else:
                    currBridgeDict[INTERFACES] = []

            # need to process interface for bridge found
            # in a previous line of output
            else:
                if(currBridgeDict is not None and
                   currBridgeDict.get(BRIDGE_NAME) is not None):
                    currBridgeDict[INTERFACES].\
                        append({INTERFACE_NAME: splitStr[0]})

        # add final bridge to dictionary
        bridgeDict[ROOT_NAME].append(currBridgeDict)

        LOG.debug("brctl data dictionary = %s" % bridgeDict)

        return bridgeDict

    @staticmethod
    def parse_ifcfg(device_name, ifcfg_contents):
        """
        This method parses the ifcfg file contents.  A dictionary of key/value
        pairs from the ifcfg file is returned.

        :param device_name: The ifcfg device, for example "eth0".
        :param ifcfg_contents: The contents of the ifcfg file.
        :return ifcfg_dict: The ifcfg file parsed into a dictionary.
        """
        # ConfigParser (mostly) handles the properties file format, which is
        # (mostly) what ifcfg files are, so ConfigParser is used here.
        # ConfigParser requires a file, so StringIO is used to convert a string
        # into a file.

        # This code creates a config StringIO that looks something like:
        # [eth0]
        # TYPE=Ethernet
        # DEVICE='eth0'
        # BOOTPROTO='dhcp'
        attribute_list = []
        try:
            config = StringIO.StringIO()
            config.write('[%s]\n' % device_name)
            config.write(ifcfg_contents)
            config.seek(0, SEEK_SET)

            # Use ConfigParser to parse our StringIO-posing-as-a-file
            cp = ConfigParser.ConfigParser()
            cp.optionxform = str
            cp.readfp(config)
            attribute_list = cp.items(device_name)
        except Exception:
            # Many files in network-scripts do not have ifcfg format, this is
            # fine, continue on.
            pass

        # Convert parsed data to a dictionary
        ifcfg_dict = dict(attribute_list)

        # Remove leading/trailing single quotes from the dictionary entries
        ifcfg_dict = dict(map(lambda (k, v): (k, v.strip("'")),
                              ifcfg_dict.iteritems()))

        return ifcfg_dict


class DOMObjectConverter():
    """
    The reason for this abstraction is to segregate functionality of what the
    parser does vs what is expected vi the DOM objects. If at any certain point
    we decide to not use the DOM objects, only this layer will get affected
    """

    def get_dom(self, host_name):
        """
        This method is used to get the DOM object representation of th devices
        on the system.
        :param: host_name: The name of the host
        :return: host_ovs_config: The DOM object with the host-ovs config info.
        """
        # Get the physical ports
        # Get the bonds and their children
        # Get the bridges and their children
        # Ignore others
        LOG.debug("Entering get_dom()...")
        lshw = CommandExecutor.get_lshw_results()
        if lshw is not None and lshw == "":
            raise exp.IBMPowerKVMInvalidLSHWOutput()
        ip_info = CommandExecutor.send_ip_address_command()
        if ip_info is not None and ip_info == "":
            raise exp.IBMPowerKVMInvalidIFCONFIGOutput()
        bonds = CommandExecutor.send_bond_info_command()
        bridges = CommandExecutor.send_brctl_command()
        if bridges is not None and bridges == "":
            raise exp.IBMPowerKVMInvalidBRCTLOutput()
        # Lists for the respective elements
        physical_devices = []
        bonded_devices = {}
        bridge_devices = {}

        # A converged list of all DOMs
        all_doms = []

        # First we check for physical ports
        for phy_int in lshw:
            phy_dev_name = phy_int['name']
            state = phy_int['state']
            speed = phy_int['speed']
            eth_obj = dom_kvm.PhysicalPort(phy_dev_name, state)
            eth_obj.speed_mb = speed

            # In some scenarios, speed may not be reported.  Set to 0 in that
            # case
            if eth_obj.speed_mb is None or eth_obj.speed_mb == '':
                eth_obj.speed_mb = 0
            LOG.debug("Formed the dom for the physical device: %s",
                      eth_obj.name)
            physical_devices.append(eth_obj)
            all_doms.append(eth_obj)

        # Next we check for the bonded adapters
        if bonds and bonds != {}:
            for bond_name, slave_list in bonds.items():
                bond_obj = dom_kvm.EthernetBond(bond_name)
                bonded_devices[bond_name] = {'obj': bond_obj,
                                             'slaves': slave_list}
                LOG.debug("Formed the dom for the bond: %s", bond_obj.name)
                all_doms.append(bond_obj)

        # Last, we check for bridges
        for bridge in bridges.get('bridges'):
            bridge_name = bridge['bridge_name']
            bridge_obj = dom_kvm.LinuxBridge(bridge_name)
            bridge_devices[bridge_name] = {'obj': bridge_obj,
                                           'slaves': bridge['interfaces']}
            LOG.debug("Formed the dom for the linuxbridge: %s",
                      bridge_obj.name)
            all_doms.append(bridge_obj)

        # Build a list of unused DOMs that is based off of all the DOMs
        unused_doms = copy.copy(all_doms)

        # Now that we've looped, we start to add the children to the bonds
        # and then the bridges.
        for bond in bonded_devices.values():
            slave_devs = [o['name'] for o in bond['slaves']]
            bond['obj'].port_list = self.get_dom_objects_by_name(all_doms,
                                                                 slave_devs)

        for bridge in bridge_devices.values():
            slave_devs = [o['name'] for o in bridge['slaves']]
            bridge['obj'].port_list = self.get_dom_objects_by_name(all_doms,
                                                                   slave_devs)

        # Now that we've gone through the trouble of adding the children
        # to the bridges/bonds (note because a bond could have a bridge as
        # a child and vice versa), we can remove the children of those elements
        # from the return list.
        for bond in bonded_devices.values():
            for b_port in bond['obj'].port_list:
                if b_port in unused_doms:
                    unused_doms.remove(b_port)
        for bridge in bridge_devices.values():
            for b_port in bridge['obj'].port_list:
                if b_port in unused_doms:
                    unused_doms.remove(b_port)

        # Next up are the OVS Ports.  We need to find those and add them
        # to the list of elements.
        vsctl = CommandExecutor.send_vsctl_command()
        if vsctl is None or vsctl == "":
            raise exp.IBMPowerKVMInvalidVSCTLOutput()
        ovs_doms = []
        for ovs_name in vsctl.keys():
            # TODO change to a conf file setting
            # Ignore the integration bridge...not use managed
            if ovs_name == CONF.integration_bridge:
                # Remove all the children interfaces of br-int
                ovs = vsctl.get(ovs_name)
                for ovs_port_name in ovs.keys():
                    backing_ports = ovs[ovs_port_name]['interface']
                    for linux_port in backing_ports:
                        # All the qvo devices have a linux bridge device which
                        # are detected by the lshw/ip command and hence we
                        # would have to translate that and remove.
                        if "qvo" in linux_port:
                            linux_port = linux_port.replace("qvo", "qbr")
                            linux_dom = self.get_dom_object_by_name(all_doms,
                                                                    linux_port)
                            if linux_dom in unused_doms:
                                LOG.debug("Removing the device: %s",
                                          linux_dom.name)
                                unused_doms.remove(linux_dom)
                        if "tap" in linux_port:
                            tap_dom = self.get_dom_object_by_name(all_doms,
                                                                  linux_port)
                            if tap_dom in unused_doms:
                                LOG.debug("Removing the device: %s",
                                          tap_dom.name)
                                unused_doms.remove(tap_dom)
                continue

            # Build our DOM objects
            ovs = vsctl.get(ovs_name)
            ovs_dom = dom_kvm.OpenVSwitch(ovs_name)
            ovs_doms.append(ovs_dom)
            all_doms.append(ovs_dom)

            # Loop through the ports and link them in
            for ovs_port_name in ovs.keys():
                # If the name matches, just ignore
                if ovs_port_name == ovs_name:
                    continue

                # If the name is phy-<ovsname>, then ignore
                if 'phy-' + ovs_name == ovs_port_name:
                    continue

                # Find the DOM objects
                backing_ports = ovs[ovs_port_name]['interface']
                b_dom_ports = self.get_dom_objects_by_name(all_doms,
                                                           backing_ports)
                # Build the OVS port
                ovs_port = dom_kvm.OVSPort(ovs_port_name)

                ovs_port.port_list = b_dom_ports

                # Remove the DOMs from the unused doms list, as they're now
                # children and no longer independent.
                for dom_port in b_dom_ports:
                    if dom_port in unused_doms:
                        LOG.debug("Removing the device: %s", dom_port.name)
                        unused_doms.remove(dom_port)

                # Add this OVS Port DOM to the OVS
                ovs_dom.ovs_port_list.append(ovs_port)

        # Now we need to splice in the IP information on each of the DOM
        # objects.  We utilize the all_doms list we had originally.
        for ip in ip_info:
            ip_addr = ip['ipv4']
            dev_name = ip['name']
            dom = self.get_dom_object_by_name(all_doms, dev_name)
            if dom is None or ip_addr is None:
                continue
            LOG.debug("Added IP address to the device: %s", dom.name)
            dom.ip_addresses.append(dom_kvm.IPAddress(ip_addr))

        # We now have a list of all the DOMs.  The OVS doms and at this point,
        # all doms are just use unused components
        host_ovs_config = dom_kvm.HostOVSNetworkConfig(host_name)
        host_ovs_config.ovs_list = ovs_doms
        host_ovs_config.unused_component_list = unused_doms

        # We wait until the end of this method to parse out Open vSwitches
        # that are too long in length.  If we did it earlier in the process
        # there is a chance that we would have accidentally shown it as an
        # adapter.
        to_del = []
        for ovs in host_ovs_config.ovs_list:
            if len(ovs.name) > MAX_VSWITCH_CHARS:
                LOG.debug("Removing OVS %s due to too long of a name",
                          ovs.name)
                to_del.append(ovs)
        for del_item in to_del:
            host_ovs_config.ovs_list.remove(del_item)

        LOG.debug("Returning data to the API from get_dom: %s",
                  host_ovs_config.to_dict())
        return host_ovs_config

    def get_dom_objects_by_name(self, all_doms, name_list):
        """
        This method is used to get a list of dom objects by referring them
        through a list of names
        :param all_doms: All the dom objects in the system.
        :param name_list: List of names for which we need the dom objects.
        :return: dom_obj_list: A list of dom objects to be returned.
        """
        dom_obj_list = []
        for dom in all_doms:
            for name in name_list:
                if dom.name == name:
                    dom_obj_list.append(dom)
                    continue
        return dom_obj_list

    def get_dom_object_by_name(self, all_doms, name):
        """
        This is used to retrieve a DOM object for a device referred with a name
        :param all_doms: Dom representation of all the devices.
        :param name: The name of the device for which we need the DOM object.
        :return: A dom object or the given device name
        """
        for dom in all_doms:
            if dom.name == name:
                return dom
        return None

    def find_management_adapter(self):
        """
        This is used to find out which adapter is used for communicating with
        the PowerVC management node.if no management adapter found then host
        cant be reachable from PowerVC node and vice versa
        :returns: Name of adapter used for communication if found, else None
        """
        # Query the Management server ip, if its None then return None
        if CONF.management_ip is None:
            LOG.debug("The management ip for the server is not set")
            return None
        # Query the DOM to find all adapters/vswitches on the system
        host_dom = DOMObjectConverter().get_dom(CONF.host)
        # Combine all the OVS together with all the unused components
        dom_top_level_components = host_dom.ovs_list
        dom_top_level_components.extend(host_dom.unused_component_list)

        ATTEMPT_COUNT = CONF.max_mgmt_adpt_ping_tries

        for i in range(0, ATTEMPT_COUNT):
            # For each element from above list
            #  If the state is up
            #    Check to see if the adapter has an IP Address.
            #    If not, continue to next element
            for component in dom_top_level_components:
                if component.get_state() != 'Unavailable':
                    if len(component.ip_addresses) > 0:
                        # Try to ping that qpid host with this adapter.
                        # using 'ping -I <adapter> -c 1 -W 1 <mgmt address>'
                        # If true returned, then save the adapter
                        # If it fails, continue to next adapter.
                        if CommandExecutor.run_ping(CONF.management_ip,
                                                    component.name):
                            return component.name
        # If we have an adapter that pings, return it. Otherwise return none.
        return None


def _write_to_file_as_root(output_file, lines):
    """
    Writes a file to the file system as root.  This method is NOT efficient.
    It was created to work around rootwrap issues.

    :param output_file: The file to write to
    :param lines: A list of strings.  Each is a new line within the file.
    """
    try:
        # Write the new contents to a temp file
        f = tempfile.NamedTemporaryFile(delete=False)
        for line in lines:
            f.write(line)
        f.flush()
        f.close()

        # Change the permissions to root / root
        utils.execute('chown', 'root', f.name, run_as_root=True)
        utils.execute('chgrp', 'root', f.name, run_as_root=True)

        # Move the file to the correct location
        utils.execute('mv', '-f', f.name, output_file, run_as_root=True)
    except Exception as e:
        LOG.error(e)
        raise


def _check_file_exists(full_path):
    """
    Will check to see if a file exists.

    :param full_path: The path to the file.
    :returns: True if it does, false otherwise
    """
    try:
        # If the file does not exist, a non-zero exit code will be returned
        utils.execute('ls', full_path, run_as_root=True, check_exit_code=[0])
        return True
    except Exception:
        return False
    return False


def _list_directory(full_path, run_as_root=False):
    """
    Lists out the full file contents of a directory.

    :param full_path: The path to the directory
    :returns: List of paths.
    """
    if not _check_file_exists(full_path):
        return []
    files = []
    try:
        stdout, stderr = utils.execute('ls', '-m',
                                       full_path,
                                       run_as_root=run_as_root)
        if stderr and stderr != '':
            raise exp.IBMPowerKVMCommandExecError(cmd='ls -m', exp=stderr)
        for line in stdout.splitlines():
            for elem in line.split(','):
                if len(elem.strip()) != 0:
                    files.append(elem.strip())
    except Exception as e:
        LOG.exception(e)
        raise
    return files


def remove_file_as_root(file_name_and_path):
    """
    Removes a file from the file system as root.  This method is NOT efficient.
    It was created to work around rootwrap issues.

    :param fname: The file to remove.
    """
    try:
        # If the file does not exist, a non-zero exit code will be returned
        utils.execute('rm', '-f',
                      file_name_and_path,
                      run_as_root=True,
                      check_exit_code=[0])
    except Exception as e:
        LOG.exception(e)
        raise
