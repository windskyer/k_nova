#    Copyright 2013 IBM Corp.
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

import argparse

# FIXME(bknudson): need to move commands somewhere else.
from nova.openstack.common import gettextutils
gettextutils.install('nova')

from nova.compute.ibm import configuration_strategy
from nova.compute.ibm import configuration_strategy_common as common
from nova.compute.ibm.sysprep import unattend
from nova.openstack.common import log as logging

from nova.openstack.common.gettextutils import _


LOG = logging.getLogger(__name__)


class ConfigurationStrategy(configuration_strategy.ConfigurationStrategy):
    """Implements configuration strategy for windows sysprep."""

    def get_name(self):
        return 'sysprep'

    def calc_configuration_data(self, instance, injected_files, admin_password,
            cs_data, cs_properties):
        """Calculates the configuration data for Windows sysprep from instance
           info.
        """

        # sysprep requires that a removable device (like cdrom),
        # that contains an unattend.xml.

        unattend_contents = self._calc_unattend_contents(cs_data,
                                                         cs_properties)

        files = {'unattend.xml': unattend_contents}

        present_disk_info = {
                'name': 'disk.sysprep',
                'volume_label': 'sysprep',
                'device_type': 'cdrom',
                'files': files
            }

        config_data = {'present_disk': present_disk_info}
        return config_data

    def _calc_unattend_contents(self, cs_data, cs_properties):
        unattend_raw_text = cs_data['properties']['metadata_template']
        unattend_obj = unattend.parse(unattend_raw_text)

        strategy_props = self._calc_properties(cs_data, cs_properties)

        self._update_unattend(unattend_obj, strategy_props)

        return unattend_obj.calc_raw_text()

    def _update_unattend(self, unattend_obj_in_out, strategy_props):
        for key, value in strategy_props.iteritems():
            unattend_obj_in_out.set(key, value)

    def _calc_properties(self, cs_data, cs_properties):
        property_mapping = common.property_mapping_to_dict(cs_data)

        properties = {}

        for key, value in cs_properties.iteritems():
            if key in property_mapping:
                targets = property_mapping[key]
                for target in targets:
                    properties[target] = value
                continue

            LOG.debug(
                _('Ignoring key %s because it\'s not in property mapping.') %
                key)

        return properties


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--unattend', metavar='FILENAME',
                        help='The unattend.xml file', required=True)
    parser.add_argument('--mapping', metavar='TARGET=SOURCE',
                        help='A property mapping', action='append')
    parser.add_argument('--auth_token', metavar='FILENAME',
                        help='File containing authentication token')
    parser.add_argument('--image_id', metavar='ID', help='Image ID')
    parser.add_argument('--replace', action='store_true',
                        help='Replace existing attribute')
    parser.add_argument('--hostname', metavar='HOSTNAME', default='localhost',
                        help='Glance server hostname, defaults to localhost')
    args = parser.parse_args()

    mappings = common.parse_mappings(args.mapping)

    with open(args.unattend, 'r') as f:
        unattend_contents = f.read()

    cs_data = common.calc_common_metadata('sysprep', unattend_contents,
                                          mappings)

    res = common.set_image_metadata(cs_data, args.image_id, args.auth_token,
                                    args.replace, hostname=args.hostname)

    print('Result: %s %s' % (res.status, res.reason))
