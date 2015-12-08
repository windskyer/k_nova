# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
# Copyright (c) 2010 Citrix Systems, Inc.

from nova.openstack.common import log as logging
from nova.virt import netutils as base_netutils
from oslo.config import cfg
from paxes_nova import logcall
from nova.network import model

CONF = cfg.CONF

injection_ops = [
    cfg.StrOpt('no_v4subnet_method_inject', default='dhcp',
               help='The network configuration method to inject in the VMs '
                    'network template if the VIF does not have a IPv4 subnet.'
                    'Valid values are (None, dhcp)'),

    cfg.StrOpt('no_v6subnet_method_inject', default=None,
               help='The network configuration method to inject in the VMs '
                    'network template if the VIF does not have a IPv6 subnet.'
                    'Valid values are (None, dhcp, auto)'),
]

CONF.register_opts(injection_ops)
CONF.import_opt('use_ipv6', 'nova.netconf')
CONF.import_opt('injected_network_template', 'nova.virt.disk.api')
LOG = logging.getLogger(__name__)


@logcall
def get_injected_network_template(network_info, use_ipv6=CONF.use_ipv6,
                                  template=CONF.injected_network_template):
    """Returns a rendered network template for the given network_info.

    :param network_info:
        :py:meth:`~nova.network.manager.NetworkManager.get_instance_nw_info`
    :param use_ipv6: If False, do not return IPv6 template information
        even if an IPv6 subnet is present in network_info.
    :param template: Path to the interfaces template file.
    """
    if not (network_info and template):
        return

    nets = []
    ifc_num = -1

    for vif in network_info:
        if not vif['network']:
            continue

        network = vif['network']
        if not network.get_meta('injected'):
            continue

        subnet_v4 = None
        subnet_v6 = None
        methodv4 = None
        methodv6 = None

        if vif['network']['subnets']:
            subnet_v4 = base_netutils._get_first_network(network, 4)
            subnet_v6 = base_netutils._get_first_network(network, 6)
        # NOTE(bnemec): The template only supports a single subnet per
        # interface and I'm not sure how/if that can be fixed, so this
        # code only takes the first subnet of the appropriate type.

        ifc_num += 1

        address = None
        netmask = None
        gateway = ''
        broadcast = None
        dns = None
        if subnet_v4:
            if subnet_v4.get_meta('dhcp_server') is not None:
                methodv4 = 'dhcp'
            elif subnet_v4['ips']:
                methodv4 = 'static'
                ip = subnet_v4['ips'][0]
                address = ip['address']
                netmask = model.get_netmask(ip, subnet_v4)
                if subnet_v4['gateway']:
                    gateway = subnet_v4['gateway']['address']
                broadcast = str(subnet_v4.as_netaddr().broadcast)
                dns = ' '.join([i['address'] for i in subnet_v4['dns']])
        else:
            # The VIF doesn't have a v4 subnet.  Check CONF values.
            if CONF.no_v4subnet_method_inject and \
                    CONF.no_v4subnet_method_inject.lower() == 'dhcp':
                methodv4 = 'dhcp'

        address_v6 = None
        gateway_v6 = ''
        netmask_v6 = None
        if subnet_v6:
            if subnet_v6.get_meta('dhcp_server') is not None:
                methodv6 = 'dhcp'
            elif subnet_v6['ips']:
                methodv6 = 'static'
                ip_v6 = subnet_v6['ips'][0]
                address_v6 = ip_v6['address']
                netmask_v6 = model.get_netmask(ip_v6, subnet_v6)
                if subnet_v6['gateway']:
                    gateway_v6 = subnet_v6['gateway']['address']

        elif use_ipv6:
            # request to use ipv6 but no subnet defined.
            # Check the CONF value for the method to use.
            if CONF.no_v6subnet_method_inject:
                if CONF.no_v6subnet_method_inject.lower() == 'dhcp':
                    methodv6 = 'dhcp'
                elif CONF.no_v6subnet_method_inject.lower() == 'auto':
                    methodv6 = 'auto'

        net_info = {'name': 'eth%d' % ifc_num,
                    'methodv4': methodv4,
                    'methodv6': methodv6,
                    'address': address,
                    'netmask': netmask,
                    'gateway': gateway,
                    'broadcast': broadcast,
                    'dns': dns,
                    'address_v6': address_v6,
                    'gateway_v6': gateway_v6,
                    'netmask_v6': netmask_v6,
                    }
        nets.append(net_info)

    if not nets:
        return

    return base_netutils.build_template(template, nets, use_ipv6)


def monkey_patch_netutils_get_injected_network_template():
    LOG.debug('Monkey patching nova.virt.netutils'
              '.get_injected_network_template(...)')

    base_netutils.get_injected_network_template = get_injected_network_template
