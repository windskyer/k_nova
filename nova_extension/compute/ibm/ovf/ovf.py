# =================================================================
# =================================================================

import re

from nova.compute.ibm.ovf import descriptor
from nova.compute.ibm.ovf import ovf_env
from nova.openstack.common import log as logging

from nova.openstack.common.gettextutils import _


LOG = logging.getLogger(__name__)


class InvalidPropertyName(Exception):
    """This exception is thrown if a property name is not valid because
       it's not defined in the descriptor.
    """
    pass


def _clean_ovf_props_matching_compresed_key(env_properties, compressed_targets,
                                            map_properties):
    env_props_text = str(env_properties)
    for target in compressed_targets:
        # . is a regular expression reserved char, escaping for accuracy
        target = target.replace('.', '\.')
        # this will also take care of product sections with no instance
        target = target.replace('\.*', '\.*\d*')
        target = target.replace('\.*\.', '\.\d+\.')
        match_list = re.findall(target, env_props_text)

        for prop in match_list:
            if prop not in map_properties:
                env_properties.pop(prop)


def _calc_all_properties(descriptor_obj, properties, compressed_targets=None):
    # calculate properties, which includes both
    # 1) all properties from the OVF descriptor, with either the specified
    #    default value or an empty string if no default is provided.
    # 2) the overridden/new values in properties

    all_properties = {}

    descriptor_properties = descriptor_obj.calc_property_values()

    # Initialize property values from descriptor.  Properties without a
    # default value are set to an empty string.  This matches the
    # behavior of existing tools, and allows additional processing to
    # occur based on the existence of properties.
    for key, value in descriptor_properties.iteritems():
        if value is None:
            value = ''
        all_properties[key] = value

    if compressed_targets:
        _clean_ovf_props_matching_compresed_key(all_properties,
                                                compressed_targets,
                                                properties)

    # update properties from descriptor with new properties,
    # overrides default value if there is one.
    for key, prop_info in properties.iteritems():
        is_expanded = prop_info.get('expanded', False)
        if key not in descriptor_properties and not is_expanded:
            if prop_info['required']:
                raise InvalidPropertyName(_("Invalid property '%s' in OVF"
                    " properties. The key is not defined in the descriptor.") %
                    key)

            # The property is not required.
            LOG.debug(
                _("Ignoring optional OVF property '%(key)s' because it's not "
                  "defined in the OVF descriptor") % dict(key=key))
            continue

        all_properties[key] = prop_info['value']

    return all_properties


def calc_ovf_env(descriptor_obj, properties, compressed_targets=None):
    '''Calculates the ovf-env file given the descriptor and the properties to
       override.

    :param descriptor_obj: OVF Environment descriptor object
    :param properties: OVF Environment properties
    :param compressed_targets: Compressed OVF mapping targets
    '''

    all_properties = _calc_all_properties(descriptor_obj, properties,
                                    compressed_targets=compressed_targets)

    ovf_env_obj = ovf_env.OvfEnv(descriptor_obj.system_id)
    ovf_env_obj.set_properties(all_properties)

    return ovf_env_obj


def calc_ovf_env_text(descriptor_raw_text, properties,
                      compressed_targets=None):
    '''Calculates the contents of the ovf-env.xml file given the
       raw descriptor text (XML .ovf file) and the properties to override.

    :param descriptor_raw_text: OVF Environment xml text
    :param properties: OVF Environment properties
    :param compressed_targets: Compressed OVF mapping targets
    '''

    descriptor_obj = descriptor.Descriptor.parse(descriptor_raw_text)
    ovf_env_obj = calc_ovf_env(descriptor_obj, properties,
                               compressed_targets=compressed_targets)
    ovf_env_contents = ovf_env_obj.calc_raw()
    return ovf_env_contents
