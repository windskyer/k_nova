#
# =================================================================
# =================================================================


class IPAddress(object):
    """
    An IP Address stores the level 3 information about a logical ethernet port.
    The data can be ipv4 or ipv6.
    """
    def __init__(self, address):
        self.address = address

    def __eq__(self, other):
        # Verify passed object is the same type
        if type(self) is not type(other):
            return False

        return self.__dict__ == other.__dict__

    def to_dict(self):
        """
        Get a dict with this object's data.  Example contents:
        {
            "ip-address":
                {
                    "address": "9.10.11.12"
                }
            }
        }

        :returns: A dictionary.
        """
        return {'ip-address': {'address': self.address}}

    def from_dict(self, data_dict):
        """
        Populate this object with data from a dictionary.  See to_dict method
        doc for expected format.

        :param data_dict: A dictionary with the data to use.
        """
        self.address = data_dict['ip-address']['address']


class LogicalEthernetPort(object):
    """
    A Logical Ethernet Port is effectively a port that has two elements.
    Something sitting on top of it, and some way to send traffic through it.

    If Python had proper interfaces, this class would be an interface.
    """
    def __init__(self, name, state=None):
        self.name = name
        self.state = state
        self.ip_addresses = []

    def __eq__(self, other):
        # Verify passed object is the same type
        if type(self) is not type(other):
            return False

        return self.__dict__ == other.__dict__


class PhysicalPort(LogicalEthernetPort):
    """
    Represents a physical ethernet port on the system that a wire is connected
    to.  Typically, logical devices (bridges, bonds, vSwitches) are put on top
    of these ports.  However, they can also have IP Addresses put directly on
    top of them, and if they do then the primary operating system can connect
    through it.

    To determine all of the physical ports on the system we will be utilizing
    the lshw command.
    """
    def __init__(self, name, state):
        super(PhysicalPort, self).__init__(name, state)
        self.speed_mb = 100
        self.ip_addresses = []

    def get_state(self):
        return self.state

    def get_speed_mb(self):
        return self.speed_mb

    def to_dict(self):
        """
        Get a dict with this object's data.  Example contents:
        {
            "physical-port":
                {
                    "name": "eth0",
                    "speed_mb": 100,
                    "state": "Available",
                    "ip_addresses": []
                }
            }
        }

        :returns: A dictionary.
        """
        ip_address_list = []
        for ip_address in self.ip_addresses:
            ip_address_list.append(ip_address.to_dict())
        return {'physical-port': {'name': self.name,
                                  'speed_mb': self.get_speed_mb(),
                                  'state': self.get_state(),
                                  'ip_addresses': ip_address_list}}

    def from_dict(self, data_dict):
        """
        Populate this object with data from a dictionary.  See to_dict method
        doc for expected format.

        :param data_dict: A dictionary with the data to use.
        """
        self.name = data_dict['physical-port']['name']
        self.speed_mb = data_dict['physical-port']['speed_mb']
        self.state = data_dict['physical-port']['state']
        if 'ip_addresses' in data_dict['physical-port']:
            for ip_dict in data_dict['physical-port']['ip_addresses']:
                ip_obj = IPAddress(None)
                ip_obj.from_dict(ip_dict)
                self.ip_addresses.append(ip_obj)


class EthernetBond(LogicalEthernetPort):
    """
    The Ethernet Bond is a low level Linux networking element that can be used
    to bond multiple physical adapters together.  Physical Ports can be added
    to a bond.  Options can be added to them (often distribution specific) to
    define the bonding attributes - such as how to balance the traffic across
    the bonds.

    The bond takes multiple ports and presents them as a single logical entity
    that can send traffic.  It is nice because if one port goes down, the other
    is there to enable traffic.  It not only provides redundancy, it also
    provides additional speed.

    The bonds can be determined by listing the files in /proc/net/bonding/.
    The main element that we wish to find in here is the Physical Ports within
    it.
    """
    def __init__(self, name):
        super(EthernetBond, self).__init__(name)
        self.port_list = []

    def get_state(self):
        return derive_state(self.port_list)

    def get_speed_mb(self):
        return total_speed(self.port_list)

    def to_dict(self):
        """
        Get a dict with this object's data.  Example contents:
        {
            "ethernet-bond":
                {
                    "name": "bond0",
                    "state": "Available",
                    "speed": "1000",
                    "ports": [],
                    "ip_addresses": []
                }
            }
        }

        :returns: A dictionary.
        """
        ports = []
        for port in self.port_list:
            ports.append(port.to_dict())
        ip_address_list = []
        for ip_address in self.ip_addresses:
            ip_address_list.append(ip_address.to_dict())
        return {'ethernet-bond': {'name': self.name,
                                  'state': self.get_state(),
                                  'speed_mb': self.get_speed_mb(),
                                  'ports': ports,
                                  'ip_addresses': ip_address_list}}

    def from_dict(self, data_dict):
        """
        Populate this object with data from a dictionary.  See to_dict method
        doc for expected format.

        :param data_dict: A dictionary with the data to use.
        """
        self.name = data_dict['ethernet-bond']['name']
        self.state = data_dict['ethernet-bond']['state']
        self.port_list = []
        if 'ports' in data_dict['ethernet-bond']:
            for port_dict in data_dict['ethernet-bond']['ports']:
                port_obj = PhysicalPort(None, None)
                port_obj.from_dict(port_dict)
                self.port_list.append(port_obj)
        if 'ip_addresses' in data_dict['ethernet-bond']:
            for ip_dict in data_dict['ethernet-bond']['ip_addresses']:
                ip_obj = IPAddress(None)
                ip_obj.from_dict(ip_dict)
                self.ip_addresses.append(ip_obj)


class LinuxBridge(LogicalEthernetPort):
    """
    A standard linux bridge is just an Ethernet Hub.  It has elements sitting
    on top of it, and devices within it.  What the bridge does is broadcast to
    all consumers the packets that come into it.

    Bridges are incredibly popular in networking today because they are
    lightweight, but since they broadcast every packet it does become expensive
    once several VMs are running on that bridge (as the packet gets sent to all
    hosts).

    We will not support bridges as a connection device.  However, we need to
    understand which devices are backing a Bridge.  This is where the brctl
    command comes into use.  The output of this should show us which Physical
    Ports (well, really Logical Ethernet Ports) are backing the bridge.  Those
    are NOT candidates to add to the vSwitches.
    """
    def __init__(self, name):
        super(LinuxBridge, self).__init__(name)
        self.port_list = []

    def get_state(self):
        return derive_state(self.port_list)

    def get_speed_mb(self):
        return slowest_speed(self.port_list)

    def to_dict(self):
        """
        Get a dict with this object's data.  Example contents:
        {
            "linux-bridge":
                {
                    "name": "br0",
                    "state": "Available",
                    "speed_mb": "100",
                    "ports": [],
                    "ip_addresses": []
                }
            }
        }

        :returns: A dictionary.
        """
        ports = []
        for port in self.port_list:
            ports.append(port.to_dict())
        ip_address_list = []
        for ip_address in self.ip_addresses:
            ip_address_list.append(ip_address.to_dict())
        return {'linux-bridge': {'name': self.name,
                                 'state': self.get_state(),
                                 'speed_mb': self.get_speed_mb(),
                                 'ports': ports,
                                 'ip_addresses': ip_address_list}}

    def from_dict(self, data_dict):
        """
        Populate this object with data from a dictionary.  See to_dict method
        doc for expected format.

        :param data_dict: A dictionary with the data to use.
        """
        self.name = data_dict['linux-bridge']['name']
        self.port_list = []
        if 'ports' in data_dict['linux-bridge']:
            for port_dict in data_dict['linux-bridge']['ports']:
                port_obj = _inspect_dictionary_type(port_dict)
                port_obj.from_dict(port_dict)
                self.port_list.append(port_obj)
        if 'ip_addresses' in data_dict['linux-bridge']:
            for ip_dict in data_dict['linux-bridge']['ip_addresses']:
                ip_obj = IPAddress(None)
                ip_obj.from_dict(ip_dict)
                self.ip_addresses.append(ip_obj)


class OVSPort(LogicalEthernetPort):
    """
    The OVS Port represents a 'port' (like a port on a switch).  However, it
    may consist of many OVS Interface's.  Users can create a 'Link Aggregation
    Port' on an OpenVSwitch, and in that scenario is consists of multiple OVS
    Interfaces.

    An OVS Port that is set up for Link Aggregation provides the same benefits
    as a Bond.  However, since it is defined in OpenVSwitch, we have a
    consistent format distribution to distribution.
    """
    def __init__(self, name):
        super(OVSPort, self).__init__(name)
        self.port_list = []

    def get_state(self):
        return derive_state(self.port_list)

    def get_speed_mb(self):
        return total_speed(self.port_list)

    def to_dict(self):
        """
        Get a dict with this object's data.  Example contents:
        {
            "ovs-port":
                {
                    "name": "eth0",
                    "state": "Available",
                    "speed_mb": "100",
                    "ports": []
                }
            }
        }

        :returns: A dictionary.
        """
        ports = []
        for port in self.port_list:
            ports.append(port.to_dict())
        return {'ovs-port': {'name': self.name,
                             'state': self.get_state(),
                             'speed_mb': self.get_speed_mb(),
                             'ports': ports}}

    def from_dict(self, data_dict):
        """
        Populate this object with data from a dictionary.  See to_dict method
        doc for expected format.

        :param data_dict: A dictionary with the data to use.
        """
        self.name = data_dict['ovs-port']['name']
        self.port_list = []
        if 'ovs-port' in data_dict and 'ports' in data_dict['ovs-port']:
            for logical_port_dict in data_dict['ovs-port']['ports']:
                port_obj = _inspect_dictionary_type(logical_port_dict)
                port_obj.from_dict(logical_port_dict)
                self.port_list.append(port_obj)


class OpenVSwitch(object):
    """
    The OpenVSwitch (sometimes referred to as a bridge from the ovs-vsctl
    command) is an internal virtual switch.  It presents itself as a 'Bridge'
    to the Linux operating system.  Every port within it is essentially a
    system level port.
    """
    def __init__(self, name):
        self.name = name
        self.ovs_port_list = []  # Type of OVSPort
        self.ip_addresses = []

    def get_state(self):
        return derive_state(self.ovs_port_list)

    def get_speed_mb(self):
        return slowest_speed(self.ovs_port_list)

    def get_port(self, port_name):
        """
        Returns the port (if one exists) based off the name

        :param port_name: The name of the port
        :return: The OVS Port if it exists.  None otherwise
        """
        for port in self.ovs_port_list:
            if port.name == port_name:
                return port
        return None

    def to_dict(self):
        """
        Get a dict with this object's data.  Example contents:
        {
            "ovs":
                {
                    "name": "eth0",
                    "state": "Available",
                    "speed_mb": "1000",
                    "ports": []
                }
            }
        }

        :returns: A dictionary.
        """
        port_list = []
        for ovs_port in self.ovs_port_list:
            port_list.append(ovs_port.to_dict())
        ip_address_list = []
        for ip_address in self.ip_addresses:
            ip_address_list.append(ip_address.to_dict())
        return {'ovs': {'name': self.name,
                        'state': self.get_state(),
                        'speed_mb': self.get_speed_mb(),
                        'ports': port_list,
                        'ip_addresses': ip_address_list}}

    def from_dict(self, data_dict):
        """
        Populate this object with data from a dictionary.  See to_dict method
        doc for expected format.

        :param data_dict: A dictionary with the data to use.
        """
        self.name = data_dict['ovs']['name']
        self.ovs_port_list = []
        if 'ovs' in data_dict and 'ports' in data_dict['ovs']:
            for ovs_port_dict in data_dict['ovs']['ports']:
                port_obj = OVSPort(None)
                port_obj.from_dict(ovs_port_dict)
                self.ovs_port_list.append(port_obj)
        if 'ovs' in data_dict and 'ip_addresses' in data_dict['ovs']:
            for ip_dict in data_dict['ovs']['ip_addresses']:
                ip_obj = IPAddress(None)
                ip_obj.from_dict(ip_dict)
                self.ip_addresses.append(ip_obj)


class HostOVSNetworkConfig(object):
    """
    The HostOVSNetworkConfig contains all of the configuration about a KVM
    host, including the OpenVSwitches and the networking components that are
    not currently used.
    """
    def __init__(self, host_name):
        self.host_name = host_name
        self.ovs_list = []
        self.unused_component_list = []
        self.error_list = []

    def contains_unused_port(self, port_obj):
        """
        Checks to see if the specific port object is in this host's unused
        component list.

        :param port_obj: A LogicalEthernetAdapter object to check for.
        :returns: True if the object is in the unused component list, False
                  otherwise.
        """
        for unused_component in self.unused_component_list:
            if unused_component == port_obj:
                return True
        return False

    def contains_unused_port_by_name(self, port_obj_name):
        """
        Checks to see if the specific port object is in this host's unused
        component list.  Validation is done based off port name

        :param port_obj_name: A LogicalEthernetAdapter name to check for.
        :returns: True if the object is in the unused component list, False
                  otherwise.
        """
        for unused_component in self.unused_component_list:
            if unused_component.name == port_obj_name:
                return True
        return False

    def port_in_unused_component(self, port_obj_name):
        """
        Checks to see if the specific port object is in this host's unused
        component list.  Validation is done based off port name

        :param port_obj_name: A LogicalEthernetAdapter name to check for.
        :returns: True if the object is in the unused component list, False
                  otherwise.
        """
        for unused_component in self.unused_component_list:
            if unused_component.name == port_obj_name:
                return False
            port_exists = self._recurse_contains_port_by_name(port_obj_name,
                                                              unused_component)
            if port_exists:
                return True
        return False

    def recurse_unused_port_by_name(self, port_obj_name):
        """
        Checks to see if the specific port object is in this host's unused
        component list as a top level device or as a port in a top level
        device. If it returns False, we can conclude that the device is not
        a part of the unused device list.

        :param port_obj_name: A LogicalEthernetAdapter name to check for.
        :returns: True if the object is in the unused component list, False
                  otherwise.
        """
        for unused_component in self.unused_component_list:
            port_exists = self._recurse_contains_port_by_name(port_obj_name,
                                                              unused_component)
            if port_exists:
                return True
        return False

    def contains_vswitch_port(self, ovs_port_obj):
        """
        Checks to see if the specific OVSPort object is in any of this host's
        vswitches.

        :param ovs_port_obj: An OVSPort object to check for
        :return: The vSwitch DOM object that contains the port, or None
        """
        for ovs in self.ovs_list:
            for ovs_port in ovs.ovs_port_list:
                if ovs_port == ovs_port_obj:
                    return ovs
        return None

    def contains_vswitch_port_by_name(self, ovs_port_name):
        """
        Checks to see if the specific OVSPort object is in any of this host's
        vswitches.  Validation is done based off port name.

        :param ovs_port_name: An OVS Port name to check for
        :return: The vSwitch DOM object that contains the port, or None
        """
        for ovs in self.ovs_list:
            for ovs_port in ovs.ovs_port_list:
                if ovs_port.name == ovs_port_name:
                    return ovs
        return None

    def contains_port(self, port_obj):
        """
        Checks to see if the specific LogicalEthernetPort object is in any of
        this host's vswitches or unused components.

        :param port_obj: A LogicalEthernetPort object to check for.
        :return: True if the port_obj is on this host, False otherwise.
        """
        # Check in-use components
        for ovs in self.ovs_list:
            for ovs_port in ovs.ovs_port_list:
                for port in ovs_port.port_list:
                    if self._recurse_contains_port(port_obj, port):
                        return True

        # Check unused components
        return self.contains_unused_port(port_obj)

    def contains_port_by_name(self, port_name):
        """
        Checks to see if the named LogicalEthernetPort object is in any of
        this host's vswitches or unused components.

        :param port_name: A LogicalEthernetPort name to check for.
        :return: True if the port_name is on this host, False otherwise.
        """
        # Check in-use components
        for ovs in self.ovs_list:
            for ovs_port in ovs.ovs_port_list:
                for port in ovs_port.port_list:
                    if self._recurse_contains_port_by_name(port_name,
                                                           port):
                        return True

        # Check unused components
        return self.recurse_unused_port_by_name(port_name)

    def _recurse_contains_port(self, port_to_locate, port_obj_to_search):
        """
        Recursively search port_obj_to_search and look for port_obj_to_locate.

        :param port_obj_to_locate: A LogicalEthernetPort object to check for.
        :param port_obj_to_search: A LogicalEthernetPort object to inspect.
        :return: True if port_obj_to_locate is found, False otherwise.
        """
        if port_to_locate == port_obj_to_search:
            return True

        if hasattr(port_obj_to_search, 'port_list'):
            for port in port_obj_to_search.port_list:
                if self._recurse_contains_port(port_to_locate, port):
                    return True
        return False

    def _recurse_contains_port_by_name(self, port_to_locate,
                                       port_obj_to_search):
        """
        Recursively search port_obj_to_search and look for port_obj_to_locate.

        :param port_obj_to_locate: A LogicalEthernetPort object to check for.
        :param port_obj_to_search: A LogicalEthernetPort object to inspect.
        :return: True if port_obj_to_locate is found, False otherwise.
        """
        if port_to_locate == port_obj_to_search.name:
            return True

        if hasattr(port_obj_to_search, 'port_list'):
            for port in port_obj_to_search.port_list:
                if self._recurse_contains_port_by_name(port_to_locate, port):
                    return True
        return False

    def find_port_or_ovs(self, port_or_ovs_name):
        """
        Searches this host's vswitch names and all LogicalEthernetPorts that
        are used and unused for the specified name.  The OVSPort objects are
        NOT searched.

        :param port_or_ovs_name: A dom_kvm name to check find.
        :return dom_obj: The object that matches the name, or none.
        """
        # Check in-use components
        for ovs in self.ovs_list:
            for ovs_port in ovs.ovs_port_list:
                for port in ovs_port.port_list:
                    port_obj = self._recurse_find_port(port_or_ovs_name, port)
                    if port_obj:
                        return port_obj

        # Check unused components
        for unused_component in self.unused_component_list:
            if unused_component.name == port_or_ovs_name:
                return unused_component

        # Check vswitches
        for ovs in self.ovs_list:
            if ovs.name == port_or_ovs_name:
                return ovs

    def _recurse_find_port(self, name_to_locate, port_obj_to_search):
        """
        Recursively search port_obj_to_search and look for name_to_locate.

        :param name_to_locate: A string name to search for.
        :param port_obj_to_search: A LogicalEthernetPort object to inspect.
        :return: Object if name_to_locate is found, otherwise None.
        """
        if name_to_locate == port_obj_to_search.name:
            return port_obj_to_search

        if hasattr(port_obj_to_search, 'port_list'):
            for port in port_obj_to_search.port_list:
                child_port = self._recurse_find_port(name_to_locate, port)
                if child_port:
                    return child_port
        return None

    def get_all_ports(self):
        """
        Produces a list of all ports that are not OVSPorts.  In other words,
        all physical ports, ethernet bonds and linux bridges from this host
        will be returned.  It will NOT return components of bonds or
        linux bridges.

        :return: A list of port objects.
        """
        port_list = []
        for ovs in self.ovs_list:
            for ovs_port in ovs.ovs_port_list:
                for port in ovs_port.port_list:
                    port_list.append(port)
        for port in self.unused_component_list:
            port_list.append(port)
        return port_list

    def find_ovs_obj(self, ovs_name):
        """
        Find the OpenVSwitch object that matches the specified name.

        :param ovs_name: The name of the OpenVSwitch object to find.
        :return: An OpenVSwitch object.
        """
        for ovs in self.ovs_list:
            if ovs.name == ovs_name:
                return ovs
        return None

    def find_parent_name(self, obj_name):
        """
        Find the parent name of the passed object.

        :param obj_name: The name of an object to search for a parent of.
        :return: The parent name if found, or None.
        """
        for ovs in self.ovs_list:
            for ovs_port in ovs.ovs_port_list:
                if obj_name == ovs_port.name:
                    return ovs.name
                for port in ovs_port.port_list:
                    if obj_name == port.name:
                        return ovs_port.name
                    if self._recurse_find_port(obj_name, port):
                        return port.name
        for unused_component in self.unused_component_list:
            if self._recurse_find_port(obj_name, unused_component):
                return unused_component.name
        return None

    def to_dict(self):
        """
        Get a dict with this object's data.  Example contents:
        {
            "host-ovs-network-config":
                {
                    "host_name": "host1",
                    "vswitches": [],
                    "unused_components": [],
                    "errors": []
                }
            }
        }

        :returns: A dictionary.
        """
        ovs_dict_list = []
        for ovs_obj in self.ovs_list:
            ovs_dict_list.append(ovs_obj.to_dict())
        comp_list = []
        for comp_obj in self.unused_component_list:
            comp_list.append(comp_obj.to_dict())
        error_dict_list = []
        for error_obj in self.error_list:
            error_dict_list.append(error_obj.to_dict())
        honc = 'host-ovs-network-config'
        return {honc: {'host_name': self.host_name,
                       'vswitches': ovs_dict_list,
                       'unused_components': comp_list,
                       'errors': error_dict_list}}

    def from_dict(self, data_dict):
        """
        Populate this object with data from a dictionary.  See to_dict method
        doc for expected format.

        :param data_dict: A dictionary with the data to use.
        """
        honc = 'host-ovs-network-config'
        self.host_name = data_dict[honc]['host_name']
        self.ovs_list = []
        self.error_list = []
        self.unused_component_list = []
        if honc in data_dict and 'vswitches' in data_dict[honc]:
            for ovs_dict in data_dict[honc]['vswitches']:
                ovs_obj = OpenVSwitch(None)
                ovs_obj.from_dict(ovs_dict)
                self.ovs_list.append(ovs_obj)
        if honc in data_dict and 'errors' in data_dict[honc]:
            for error_dict in data_dict[honc]['errors']:
                error_obj = Error(None)
                error_obj.from_dict(error_dict)
                self.error_list.append(error_obj)
        if honc in data_dict and 'unused_components' in data_dict[honc]:
            for comp_dict in data_dict[honc]['unused_components']:
                comp_obj = _inspect_dictionary_type(comp_dict)
                comp_obj.from_dict(comp_dict)
                self.unused_component_list.append(comp_obj)


class Error(object):
    """
    The Error object stores data about errors that occur, for example errors
    while communicating with a host.
    """
    def __init__(self, message):
        self.message = message

    def to_dict(self):
        """
        Get a dict with this object's data.  Example contents:
        {
            "error":
                {
                    "message": "An error has occurred."
                }
            }
        }

        :returns: A dictionary.
        """
        return {'error': {'message': self.message}}

    def from_dict(self, data_dict):
        """
        Populate this object with data from a dictionary.  See to_dict method
        doc for expected format.

        :param data_dict: A dictionary with the data to use.
        """
        self.message = data_dict['error']['message']


def _inspect_dictionary_type(data_dict):
    """
    Checks the first key of a dictionary and converts it to a class.
    The dictionary {"ovs": {...}} will return an OpenVSwitch object.
    The dictionary {"linux-bridge": {...}} will return an LinuxBridge object.

    :param data_dict: The data dictionary to inspect.
    :returns: An object of type determined by the first dictionary key.
    """
    key = data_dict.keys()[0]

    if key == 'physical-port':
        return PhysicalPort(None, None)
    elif key == 'ethernet-bond':
        return EthernetBond(None)
    elif key == 'linux-bridge':
        return LinuxBridge(None)
    elif key == 'ovs-port':
        return OVSPort(None)
    elif key == 'ovs':
        return OpenVSwitch(None)

    raise Exception('Unknown type: %s' % key)


def derive_state(state_list):
    """
    Compute a derived state an object.  This will be based off the states of
    the objects based in.

    :param state_list: List of objects that have a get_state() method.
    :returns: State as a string.
    """
    # If there are no ports passed in, we assume it is an internal element,
    # and is therefore available.
    if state_list is None or len(state_list) == 0:
        return "Available"

    # Otherwise derive off the list
    states = [x.get_state().lower() for x in state_list]
    if('available' in states and
       ('unavailable' in states or 'degraded' in states)):
        return 'Degraded'
    elif 'available' in states:
        return 'Available'
    elif 'degraded' in states:
        return 'Degraded'
    else:
        return 'Unavailable'


def slowest_speed(speed_list):
    """
    Compute the derived speed by finding the slowest object in the speed_list.

    :param speed_list: A list of objects with a speed_mb attribute.
    :returns: Speed as an integer.
    """
    speed_mb = 0
    if speed_list:
        for obj in speed_list:
            if not speed_mb:
                speed_mb = obj.get_speed_mb()
            elif obj.get_speed_mb() < speed_mb:
                speed_mb = obj.get_speed_mb()
    return speed_mb


def total_speed(speed_list):
    """
    Compute the sum of all speeds in the speed_list.

    :param speed_list: A list of objects that have a speed_mb attribute.
    :returns: Speed as an integer.
    """
    speed_mb = 0
    if speed_list:
        for port in speed_list:
            if port.get_speed_mb():
                speed_mb += port.get_speed_mb()
    return speed_mb
