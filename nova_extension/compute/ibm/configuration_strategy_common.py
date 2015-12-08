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

"""Common utilities for configuration strategy."""


import httplib
import json
import re


def property_mapping_to_dict(cs_data):
    """Converts the property mapping in config strategy data from an
       array like [ {'source': 'string'}, {'target': 'string'} ] to
       a dict with { 'source-string': 'target-string', ... }.
    """

    property_mapping_arr = cs_data['properties']['mapping']

    property_mapping = {}
    for mapping_obj in property_mapping_arr:
        source = mapping_obj['source']
        target = mapping_obj['target']

        if source in property_mapping:
            property_mapping[source].append(target)
        else:
            property_mapping[source] = [target]

    return property_mapping


class InvalidMapping(Exception):
    pass


def parse_mapping(mapping_str):
    mapping_re = re.compile('^(.*)=(.*)$')
    m = mapping_re.match(mapping_str)
    if not m:
        raise InvalidMapping(mapping_str)
    return (m.group(1), m.group(2))


def parse_mappings(mappings_strings):
    if not mappings_strings:
        return []

    mappings = []

    for mapping_str in mappings_strings:
        (target, source) = parse_mapping(mapping_str)
        mappings.append({'source': source, 'target': target})

    return mappings


def calc_common_metadata(type_str, metadata_template, mappings):
    cs_properties = {'metadata_template': metadata_template,
                     'mapping': mappings}

    cs_metadata = {'type': type_str, 'properties': cs_properties}

    return cs_metadata


def set_image_metadata(config_strategy_data, image_id, auth_token_filename,
                       do_replace, hostname='localhost'):
    config_strategy_json = json.dumps(config_strategy_data)

    port = 9292
    conn = httplib.HTTPConnection(hostname, port)

    resource_path = '/v2/images/%s' % image_id
    with open(auth_token_filename, 'r') as f:
        auth_token = f.read().strip()

    headers = {
            'X-Auth-Token': auth_token,
            'Content-Type': 'application/openstack-images-v2.0-json-patch'
        }

    operation = 'add' if not do_replace else 'replace'

    set_config_strategy_op_data = {}
    set_config_strategy_op_data[operation] = '/configuration_strategy'
    set_config_strategy_op_data['value'] = config_strategy_json

    data = [set_config_strategy_op_data]

    data_str = json.dumps(data)

    conn.request('PATCH', resource_path, body=data_str, headers=headers)
    r1 = conn.getresponse()
    return r1
