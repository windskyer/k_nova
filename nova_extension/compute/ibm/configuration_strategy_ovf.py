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
import re

# FIXME(bknudson): need to move commands somewhere else.
from nova.openstack.common import gettextutils
gettextutils.install('nova')

from nova.compute.ibm import configuration_strategy
from nova.compute.ibm import configuration_strategy_common as common
from nova.compute.ibm.ovf import ovf
from nova.openstack.common import log as logging

from nova.openstack.common.gettextutils import _

LOG = logging.getLogger(__name__)


class ConfigurationStrategy(configuration_strategy.ConfigurationStrategy):
    """Implements configuration strategy for OVF."""

    def get_name(self):
        return 'ovf'

    def calc_configuration_data(self, instance, injected_files, admin_password,
            cs_data, cs_properties):
        """Calculates the configuration data for OVF from instance info."""

        # OVF requires a disk that contains an ovf-env.xml.

        descriptor_raw_text = cs_data['properties']['metadata_template']

        # Get all OVF property to value mappings and compressed targets given
        # instance data, instance metadata, and mapping from image metadata
        properties, compressed_targets = self._calc_properties(cs_data,
                                                         cs_properties)

        ovf_env_text = ovf.calc_ovf_env_text(descriptor_raw_text, properties,
                                        compressed_targets=compressed_targets)

        files = {'ovf-env.xml': ovf_env_text}

        present_disk_info = {
                'name': 'disk.ovf',
                'volume_label': 'ovf-env',
                'files': files
            }

        config_data = {'present_disk': present_disk_info}
        return config_data

    def _expand_multi_mapping(self, property_mapping, cs_properties):
        compressed_keys = []
        compressed_targets = set()
        expanded_keys = []
        expanded_values = []
        invalid_mapping_found = False
        out_property_mapping = property_mapping.copy()

        for key, value in property_mapping.iteritems():
            # Skip single-mapping entry
            if '*' not in key:
                continue

            # Making sure there is only one * in key
            if key.count('*') > 1:
                err_msg = _('mapping source %s must have only one '
                            'wildcard *.') % key
                LOG.error(err_msg)
                invalid_mapping_found = True
                continue

            invalid_target = False
            for target in value:
                # Making sure target has at least one wildcard *
                if '*' not in target:
                    err_msg = _('Mapping target %s must '
                                'have at least one *.') % target
                    LOG.error(err_msg)
                    invalid_target = True
                    invalid_mapping_found = True

            if invalid_target:
                continue

            # Add to compressed keys
            compressed_keys.append(key)

            # Expanding multi-mapping keys
            key = key.replace('.', '\.')
            key = key.replace('*', '\d+')
            match_list = re.findall(key, str(cs_properties))

            if not match_list:
                LOG.warn(_('Could not find expanded keys for compressed'
                           ' key %s') % compressed_keys[-1])

            # Expanding the values
            for exp_key in match_list:
                key = key.replace('\d+', '(\d+)')
                match = re.match(key, exp_key)
                exp_digits = match.group(1)

                exp_value = []
                for target in value:
                    compressed_targets.add(target)
                    exp_target = target.replace('*', exp_digits)
                    exp_value.append(exp_target)

                # Add to expanded values
                expanded_values.append(exp_value)

            # Add to expanded keys
            expanded_keys = expanded_keys + match_list

        if invalid_mapping_found:
            raise common.InvalidMapping(_('One or more mapping items are '
                                          'invalid. Check log file for '
                                          'detailed error messages.'))

        # Removing compressed keys
        for key in compressed_keys:
            out_property_mapping.pop(key)

        # Adding expanded key/value
        for x in range(0, len(expanded_keys)):
            out_property_mapping[expanded_keys[x]] = expanded_values[x]

        return out_property_mapping, expanded_keys, compressed_targets

    def _calc_properties(self, cs_data, cs_properties):
        property_mapping = common.property_mapping_to_dict(cs_data)

        result = self._expand_multi_mapping(property_mapping, cs_properties)
        property_mapping, exp_keys, compressed_targets = result
        properties = {}

        for key, value in cs_properties.iteritems():
            if key in property_mapping:
                targets = property_mapping.pop(key)
                expanded = key in exp_keys
                prop_info = dict(value=value, required=True, expanded=expanded)
                for target in targets:
                    properties[target] = prop_info

                continue

            # key is not in property mapping.
            if key.startswith('server.metadata.'):
                LOG.warn(_("Didn't find meta key %s in mapping, adding it") %
                         key)
                new_key = key.replace('server.metadata.', '', 1)
                properties[new_key] = dict(value=value, required=False,
                                           expanded=False)
                continue

            LOG.debug(
                _("Ignoring key %s because it's not in property mapping.") %
                key)

        if property_mapping:
            LOG.warn(_('Some property mapping items were not used because '
                       'source key was neither a known OpenStack property nor'
                       ' passed as server metadata. These are the unmapped '
                       'keys: %s') % property_mapping.keys())

        return properties, compressed_targets


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--ovf', metavar='FILENAME',
                        help='The OVF descriptor file', required=True)
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

    with open(args.ovf, 'r') as f:
        ovf_descriptor = f.read()

    cs_data = common.calc_common_metadata('ovf', ovf_descriptor, mappings)

    res = common.set_image_metadata(cs_data, args.image_id, args.auth_token,
                                    args.replace, hostname=args.hostname)

    print('Result: %s %s' % (res.status, res.reason))
