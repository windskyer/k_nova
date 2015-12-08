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

import json
import re

from nova.openstack.common import importutils
from nova.openstack.common import log as logging
from nova import utils
from oslo.config import cfg

from nova.openstack.common.gettextutils import _


opts = [
    cfg.StrOpt('configuration_strategies',
               help='configuration strategies, format is class-ids separated '
               'by spaces. For example, '
               'nova.compute.ibm.configuration_strategy_ovf.'
               'ConfigurationStrategy'),
]

CONF = cfg.CONF
CONF.register_opts(opts)
CONF.import_opt('dhcp_domain', 'nova.network.manager')

LOG = logging.getLogger(__name__)


class ConfigurationStrategy:
    """Base class for configuration strategies. To implement a custom
    configuration strategy, the developer must subclass this class and override
    the get_name and calc_configuration_data methods. The get_name
    implementation is expected to return the text matching the type of the
    configuration_strategy property added to the glance image being deployed.
    The calc_configuration_data method is expected to use cs_data parameter
    together with cs_properties parameter to replace the metada_template in
    cs_data with openstack and user provided values in cs_properties.
    The metadata_template text is extracted from the configuration_strategy
    property in glance image. See ConfigurationStrategies class below and
    methods in nova.compute.configuration_strategy_common that are handling
    the logic to get configuration_strategy property from glance image as well
    as the population of cs_data and cs_properties parameters.

    The calc_configuration_data method must return a configuration_data
    dictionary which contains one key/value pair with key being string
    'present_disk' and value being a present_disk dictionary. Here is an
    example:

        config_data = {'present_disk': present_disk_info}
        return config_data

    See below for an example of the present_disk dictionary that shows the
    required keys.

        present_disk_info = {
                'name': 'diskname.extension',
                'volume_label': 'my_custom_label',
                'files': files }

    The present_disk_info dictionary can also have an optional key/value pair
    where key is the string 'device_type' and value could be 'cdrom', 'disk',
    'floppy' or any other custom type. The idea here is that it will be
    something the compute drivers will understand when they transport
    configuration_data to the instance.

    Notice 'files' is another dictionary containing a list of key/value pairs
    where key is the file name and value is the text content of the file.
    Here is an example:

        files = {'ovf-env.xml': 'ovf_env_text_here',
                 'kickstart.xml': 'kickstart_text_here',
                 'custom_script.sh': 'script_content_here' }

    In most cases 'files' will be used to store the modified metadata_template
    as a file.

    In addition to implementing the ConfigurationStrategy class, make sure you
    add your fully qualified implementation to the configuration_strategies
    config option in your nova.conf file. This is how you register your unique
    implementation.

    There is another set of classes that may need to be changed to get your
    custom configuration_strategy working depending on your custom transport
    needs. Each independent hypervisor driver implements this transport. We
    currently support creating and transporting ISO file with all the content
    from present_disk in configuration_data. The configuration_data gets passed
    in as a property in the instance dictionary and each driver is in charge
    of creating the ISO and transporting it to the instance being booted. For
    convenience we have created a helper method to deal with ISO creation found
    in
    nova.virt.ibm.config_filesystem_image_utils.build_config_filesystem_image.
    If ISO and media transport is not the desired transport for your custom
    implementation you may need to make changes to either the utility method,
    the drivers themselves or both.
    """

    def get_name(self):
        """Override to return a unique name, matching the 'type' in the image
        metadata configuration strategy.
        """
        return

    def calc_configuration_data(self, instance, injected_files, admin_password,
            cs_data, cs_properties):
        """Generates a configuration data dictionary with information to
        transport to instance during boot.
        :param instance: Instance object as returned by DB layer.
        :param injected_files: dictionary with file names and their content.
        :param admin_password: generated by openstack.
        :param cs_data: configuration_strategy metadata in glance image.
        :param cs_properties: dictionary containing configuration data
        provided by openstack plus server metadata passed on boot
        :returns: dictionary containing data to transport to the running
        instance

        """
        pass


class ConfigurationStrategies:
    """Registry of known configuration strategies."""
    def __init__(self):
        LOG.debug(_("Initializing configuration strategies..."))

        self._strategies = {}

        from nova.compute.ibm import configuration_strategy_ovf
        from nova.compute.ibm import configuration_strategy_sysprep

        config_strategy_objs = [
                configuration_strategy_ovf.ConfigurationStrategy(),
                configuration_strategy_sysprep.ConfigurationStrategy(),
            ]

        class_names_str = CONF.configuration_strategies
        if class_names_str:
            class_names = re.split('\s+', class_names_str)

            for class_name in class_names:
                obj = importutils.import_object(class_name)
                config_strategy_objs.append(obj)

        for config_strategy_obj in config_strategy_objs:
            name = config_strategy_obj.get_name()
            LOG.debug('Strategy %s is %s' % (name, config_strategy_obj))
            self._strategies[name] = config_strategy_obj

    def calc_configuration_data(self, instance, image_meta, network_info,
                                injected_files, admin_password):
        cs_data = self._extract_strategy(image_meta)

        if not cs_data:
            LOG.debug(_('No configuration strategy data in image metadata,'
                        ' skipping.'))
            return

        cs_type = cs_data.get('type')
        if not cs_type:
            LOG.warning(_("Config strategy data doesn't specify a type,"
                          " skipping"))
            return

        cs_obj = self._strategies.get(cs_type)
        if not cs_obj:
            LOG.warning(_("Config strategy type doesn't match a known type,"
                          " ignoring. The type is '%s'") % cs_type)
            return

        cs_properties = self._build_property_map(instance, network_info,
                                                 admin_password)

        config_data = cs_obj.calc_configuration_data(instance, injected_files,
                    admin_password, cs_data, cs_properties)

        return config_data

    @staticmethod
    def _extract_strategy(image_meta):
        if not image_meta:
            return

        image_properties = image_meta.get('properties')

        if not image_properties:
            return

        configuration_strategy_str = (
            image_properties.get('configuration_strategy'))

        if not configuration_strategy_str:
            LOG.debug(_('No configuration strategy in image metadata.'))
            return

        LOG.debug('configuration_strategy_str=%s' % configuration_strategy_str)

        configuration_strategy = json.loads(configuration_strategy_str)
        return configuration_strategy

    @staticmethod
    def _calc_dns_list_str(dns_list):
        return ' '.join([d['address'] for d in dns_list])

    @staticmethod
    def _build_property_map(instance, network_info, admin_password):
        all_property_values = {}

        # These properties we calculate from OpenStack environment.

        all_property_values['server.admin_password'] = admin_password

        all_property_values['server.hostname'] = instance['hostname']
        all_property_values['server.domainname'] = CONF.dhcp_domain

        if len(network_info):
            subnets = network_info[0]['network']['subnets']
            if subnets:
                dns = subnets[0]['dns']
                if dns:
                    all_property_values['server.dns-client.pri_dns'] = (
                            dns[0]['address'])

                    if len(dns) > 1:
                        sec_dns_ip = dns[1]['address']
                    else:
                        # If there's only 1 DNS, default sec_dns to pri_dns
                        sec_dns_ip = dns[0]['address']

                    all_property_values['server.dns-client.sec_dns'] = (
                            sec_dns_ip)

                    all_property_values['server.dns-client.dns_list'] = (
                            ConfigurationStrategies._calc_dns_list_str(dns))

        for nw_i, nw_all in enumerate(network_info):
            nw_i = nw_i + 1
            nw_data = nw_all['network']

            mac = nw_all['address']
            all_property_values['server.network.%d.mac' % nw_i] = (
                    mac)
            all_property_values['server.network.%d.mac_alt' % nw_i] = (
                    mac.replace(':', '-'))

            slot_id = int(mac[-2:], 16)
            all_property_values['server.network.%d.slotnumber' % nw_i] = (
                    str(slot_id))

            subnets = nw_data['subnets']
            if not subnets:
                # There's no subnet defined, so indicate use dhcp for this
                # network.
                addr_type = 'v4'
                all_property_values[
                    'server.network.%d.%s.use_dhcp' % (nw_i, addr_type)] = (
                    'true')
                continue
            # Iterating through subnets to get ipv4 as well as ipv6 addresses
            for subnet in subnets:
                ips = subnet['ips']

                if len(ips) == 0:
                    continue

                # Getting first IP of each subnet
                ip = ips[0]
                gateway = subnet['gateway']['address']
                # FIXME(blk-u): This bit is copied from nova.network.model.
                if ip['version'] == 4:
                    addr_type = 'v4'
                    netmask = str(subnet.as_netaddr().netmask)
                else:
                    addr_type = 'v6'
                    netmask = str(subnet.as_netaddr().prefixlen)

                all_property_values[
                    'server.network.%d.%s.address' % (nw_i, addr_type)] = (
                    ip['address'])
                all_property_values[
                    'server.network.%d.%s.netmask' % (nw_i, addr_type)] = (
                    netmask)
                all_property_values[
                    'server.network.%d.%s.cidr' % (nw_i, addr_type)] = (
                    '%s/%s' % (ip['address'], subnet.as_netaddr().prefixlen))
                all_property_values[
                    'server.network.%d.%s.gateway' % (nw_i, addr_type)] = (
                    gateway)
                all_property_values[
                    'server.network.%d.%s.use_dhcp' % (nw_i, addr_type)] = (
                    'false')

        # These properties are from the metadata passed in on the boot.
        md = utils.instance_meta(instance)
        for i in md:
            key = 'server.metadata.' + i
            LOG.debug('from metadata, setting key %s to %s' %
                      (key, md[i]))
            all_property_values[key] = md[i]

        return all_property_values
