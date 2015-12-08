#
# =================================================================
# =================================================================


# Keys for ifcfg file format.  Originally taken from here:
# https://access.redhat.com/site/documentation/en-US/
#     Red_Hat_Enterprise_Linux/6/html/Deployment_Guide/
#     s1-networkscripts-interfaces.html

from oslo.config import cfg
CONF = cfg.CONF

integ_bridge_opts = [cfg.StrOpt('integration_bridge', default='br-int',
                                help='Name of the integration bridge')]
CONF.register_opts(integ_bridge_opts)
mgmt_host_opts = [cfg.StrOpt('management_ip', default=None,
                             help='Management Server ip')]
CONF.register_opts(mgmt_host_opts)

IFCFG_TYPE = 'TYPE'
IFCFG_DEVICE = 'DEVICE'
IFCFG_DHCP_HOSTNAME = 'DHCP_HOSTNAME'
IFCFG_BOND_IFACES = 'BOND_IFACES'
IFCFG_BONDING_OPTS = 'BONDING_OPTS'
IFCFG_BOOTPROTO = 'BOOTPROTO'
IFCFG_BROADCAST = 'BROADCAST'
IFCFG_DEV_TYPE = 'DEVICETYPE'
IFCFG_DHCPV6C = 'DHCPV6C'
IFCFG_DHCPV6C_OPTIONS = 'DHCPV6C_OPTIONS'
IFCFG_DNS1 = 'DNS1'
IFCFG_DNS2 = 'DNS2'
IFCFG_ETHTOOL_OPTIONS = 'ETHTOOL_OPTIONS'
IFCFG_GATEWAY = 'GATEWAY'
IFCFG_HOTPLUG = 'HOTPLUG'
IFCFG_HWADDR = 'HWADDR'
IFCFG_IPADDR = 'IPADDR'
IFCFG_IPV6ADDR = 'IPV6ADDR'
IFCFG_IPV6ADDR_SECONDARIES = 'IPV6ADDR_SECONDARIES'
IFCFG_IPV6INIT = 'IPV6INIT'
IFCFG_IPV6_AUTOCONF = 'IPV6_AUTOCONF'
IFCFG_IPV6_MTU = 'IPV6_MTU'
IFCFG_IPV6_PRIVACY = 'IPV6_PRIVACY'
IFCFG_LINKDELAY = 'LINKDELAY'
IFCFG_MACADDR = 'MACADDR'
IFCFG_MASTER = 'MASTER'
IFCFG_MTU = 'MTU'
IFCFG_NETMASK = 'NETMASK'
IFCFG_NETWORK = 'NETWORK'
IFCFG_NM_CONTROLLED = 'NM_CONTROLLED'
IFCFG_ONBOOT = 'ONBOOT'
IFCFG_OVS_BRIDGE = 'OVS_BRIDGE'
IFCFG_OVS_OPTIONS = 'OVS_OPTIONS'
IFCFG_PEERDNS = 'PEERDNS'
IFCFG_REMOTE_IPADDR = 'REMOTE_IPADDR'
IFCFG_SLAVE = 'SLAVE'
IFCFG_SRCADDR = 'SRCADDR'
IFCFG_STARTMODE = 'STARTMODE'
IFCFG_USERCONTROL = 'USERCONTROL'
IFCFG_BRIDGE = 'BRIDGE'

ERRORS_KEY = 'errors'
WARNINGS_KEY = 'warnings'
