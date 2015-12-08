#
# =================================================================
# =================================================================

from nova.openstack.common import log as logging
from powervc_nova.network.powerkvm.agent import commandlet
from powervc_nova.network.powerkvm.agent.common import warning
from powervc_nova.network.powerkvm.agent.common import micro_op
from oslo.config import cfg
from powervc_nova.network.powerkvm.agent.ovs_validation_mixin \
    import OVSValidator
from powervc_nova.network.powerkvm import agent
from nova import utils

import re
import os
from xml.etree import cElementTree

CONF = cfg.CONF
CONF.import_opt('integration_bridge', 'powervc_nova.network.powerkvm.agent')

LOG = logging.getLogger(__name__)


class BeginningVswitchValidator(micro_op.MicroOperation,
                                OVSValidator):
    """
    Add a port to an OpenVSwitch.
    """

    def __init__(self, ovs_name, desired_dom):
        """
        Constructor.

        :param ovs_name: The name of the Open vSwitch that is having a port
                         added.
        :param desired_dom: The desired dom.  We want this micro op to
            validate against thedesired dom so it must be passed in
            via the constructor.
        """
        self.commandex = commandlet.CommandExecutor()
        self.commandparse = commandlet.CommandParser()
        self.ovs_name = ovs_name
        self.current_dom = desired_dom
        self.ifcfg_cache = {}
        self.ifcfg_files = []
        self.libvirt_cache = {}
        self.libvirt_files = []

    def validate(self, current_dom):
        """
        Overrides parent method.

        Validate

        NOTE: Unlike other validates, this checks the desired
        dom, not the system's current dom.  The current_dom
        parm passed in here is ignored; the dom passed in
        during the micro op create is used to set the
        ovs_validation_mixin current_dom to be the desired
        dom.

        """
        try:
            warning_list = []

            self.validate_ovs_ports_appear_once(self.ovs_name)

            self.validate_ovs_port_appears_on_one_ovs(self.ovs_name)

            self.validate_one_ip_address(self.ovs_name)

            # check adapter ip addresses for all ports
            adapter_list_1 = ""
            adapter_list_2 = ""
            for adapter in current_dom.get_all_ports():
                LOG.debug('Processing adapter %s' % adapter.name)
                if len(adapter.ip_addresses) > 0:
                    ifcfg_dict = self._has_ifcfg_data(adapter.name)
                    LOG.debug('ifcfg data for %s: %s'
                              % (adapter.name, str(ifcfg_dict)))
                    if ifcfg_dict == {}:
                        LOG.debug("checking for libvirt data for ip "
                                  "configuration")
                        libvirt_dir = '/var/lib/libvirt/network'
                        if not self._has_libvirt_network_data(libvirt_dir,
                                                              adapter.name):
                            adapter_list_1 += "%s, " % adapter.name
                    elif agent.IFCFG_BOOTPROTO in ifcfg_dict and \
                            ifcfg_dict[agent.IFCFG_BOOTPROTO].upper() == \
                            "DHCP":
                        adapter_list_2 += "%s, " % adapter.name

            if adapter_list_1 != "":
                warning_list.append(
                    warning.OVSAdapterHasTempIPAddressWarning(
                        adapter_name=adapter_list_1[:-2]))

            if adapter_list_2 != "":
                warning_list.append(
                    warning.OVSAdapterDHCPWarning(
                        adapter_name=adapter_list_2[:-2]))

        except Exception as exc:
            LOG.error(exc)
            raise
        return current_dom, warning_list

    def execute(self):
        """
        Overrides parent method.

        This micro op is for validation only
        """
        pass

    def undo(self):
        """
        Overrides parent method.

        This micro op is for validation only
        """
        pass

    def _has_ifcfg_data(self, device_name):
        """
        Determine if ifcfg data exists for device.

        :param device_name: A device name to look up such as eth0.
        :return: ifcfg_dict if data exists, {} otherwise.
        """

        ifcfg_dir = '/etc/sysconfig/network-scripts'

        # The device_name is usually in the ifcfg file name, so check for this
        # case first.
        ifcfg_dict = {}
        fname = '%s/ifcfg-%s' % (ifcfg_dir, device_name)
        LOG.debug('Initial attempt to process %s using ifcfg file %s'
                  % (device_name, fname))
        if fname in self.ifcfg_cache:
            LOG.debug("Using cached ifcfg data")
            ifcfg_dict = self.ifcfg_cache[fname]

        elif commandlet._check_file_exists(fname):
            if fname not in self.ifcfg_cache:
                LOG.debug("Retrieving ifcfg data from system")
                stdout = utils.execute('cat', fname, run_as_root=True)[0]
                ifcfg_dict = self.commandparse.parse_ifcfg(device_name, stdout)
                self.ifcfg_cache[fname] = ifcfg_dict

        # make sure there are no quote around the device name we
        # get from the ifcfg file
        ifcfg_dev_name = \
            re.sub(r'^"|"$',
                   '',
                   ifcfg_dict.get(agent.IFCFG_DEVICE, ''))
        if ifcfg_dev_name == device_name and self._has_ip_config(ifcfg_dict):
            LOG.debug('found ifcfg file in expected location.')
            return ifcfg_dict

        # The ifcfg with the desired device name either does not exist, or
        # contains a different internal device name.  Search all ifcfg files
        # looking for the correct device.

        if len(self.ifcfg_files) == 0:
            self.ifcfg_files = [f for f in commandlet.
                                _list_directory(ifcfg_dir)
                                if commandlet._check_file_exists(
                                '%s/%s' % (ifcfg_dir, f))]
        for ifcfg_file in self.ifcfg_files:
            fname = '%s/%s' % (ifcfg_dir, ifcfg_file)
            LOG.debug('General attempt to process %s using ifcfg file %s'
                      % (device_name, fname))
            if os.access(fname, os.X_OK):
                LOG.debug("%s is executable, will not be processed for ifcfg "
                          "data" % fname)
                self.ifcfg_files.remove(ifcfg_file)
                continue
            if fname not in self.ifcfg_cache:
                LOG.debug("For %s retrieving ifcfg file %s from filesystem"
                          % (device_name, fname))
                stdout = utils.execute('cat', fname, run_as_root=True)[0]
                ifcfg_dict = self.commandparse.parse_ifcfg(device_name, stdout)
                self.ifcfg_cache[fname] = ifcfg_dict

            else:
                LOG.debug("Using cached ifcfg data for file %s" % fname)

            # make sure there are no quote around the device name we
            # get from the ifcfg file
            ifcfg_dev_name = \
                re.sub(r'^"|"$',
                       '',
                       self.ifcfg_cache[fname].get(agent.IFCFG_DEVICE, ''))

            LOG.debug("check for match between %s and %s and "
                      "ip data in %s"
                      % (ifcfg_dev_name, device_name, str(ifcfg_dict)))
            if ifcfg_dev_name == device_name and \
                    self._has_ip_config(self.ifcfg_cache[fname]):
                LOG.debug('found ifcfg data in file %s for device %s.'
                          % (fname, device_name))
                return self.ifcfg_cache[fname]

        return {}

    def _has_ip_config(self, device_dict):
        """
        Check to see if the specified dictionary has IP config.

        :param device_dict: The dictionary to inspect.
        :returns: True if ip config is present, False otherwise.
        """
        LOG.debug('ifcfg data: %s' % str(device_dict))

        keys_that_indicate_ip_config = [agent.IFCFG_IPADDR,
                                        agent.IFCFG_IPV6ADDR,
                                        agent.IFCFG_DHCP_HOSTNAME,
                                        agent.IFCFG_DHCPV6C,
                                        agent.IFCFG_DHCPV6C_OPTIONS,
                                        agent.IFCFG_DHCP_HOSTNAME,
                                        ]
        for key in keys_that_indicate_ip_config:
            if key in device_dict and device_dict[key]:
                LOG.debug('ifcfg ip data match on key %s' % device_dict[key])
                return True
        return False

    def _has_libvirt_network_data(self, directory, device_name):
        def _parse_libvirt_file(libvirt_filename):
            try:
                xml_data = None
                if libvirt_filename not in self.libvirt_cache:
                    stdout = utils.execute('cat',
                                           libvirt_filename,
                                           run_as_root=True)[0]
                    xml_data = cElementTree.fromstring(stdout)
                    self.libvirt_cache[libvirt_filename] = xml_data

                else:
                    xml_data = self.libvirt_cache[libvirt_filename]

                return xml_data
            except Exception:
                '''
                file cannot be parsed as xml, so ignore it though
                log debug message
                '''
                LOG.debug("file %s cannot be parsed as xml, ignoring" %
                          libvirt_filename)

            return None

        def _scan_for_bridge(libvirt_xml, device_name):
            net_list = libvirt_xml.findall('network')
            for network in net_list:
                bridge_list = network.findall('bridge')
                ip = network.find('ip')
                for bridge in bridge_list:
                    name = bridge.get('name')
                    LOG.debug('device name = %s, ip = %s'
                              % (device_name, str(ip)))
                    if name == device_name and ip:
                        return True

            return False

        libvirt_dir = directory

        if len(self.libvirt_files) == 0:
            self.libvirt_files = [f for f in commandlet.
                                  _list_directory(libvirt_dir,
                                                  run_as_root=True)
                                  if commandlet._check_file_exists(
                                      '%s/%s' % (libvirt_dir, f))]

        LOG.debug("libvirt network files = %s" % self.libvirt_files)
        for libvirt_file in self.libvirt_files:
            fname = '%s/%s' % (libvirt_dir, libvirt_file)

            xml_elem = _parse_libvirt_file(fname)
            if not xml_elem:
                continue

            if _scan_for_bridge(xml_elem, device_name):
                return True

        return False
