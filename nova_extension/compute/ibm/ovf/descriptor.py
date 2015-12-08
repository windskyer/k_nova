# =================================================================
# =================================================================

from nova.compute.ibm import etree_wrapper
from nova.openstack.common import log as logging

from nova.openstack.common.gettextutils import _


LOG = logging.getLogger(__name__)


class InvalidDescriptorDocument(Exception):
    """This exception is thrown if the descriptor provided to parse() is not
    valid.

    """

    pass


def _extract_system_id(doc):
    OVF_NAMESPACE = 'http://schemas.dmtf.org/ovf/envelope/1'
    VIRTUAL_SYSTEM_ELEM_NAME = '{%s}VirtualSystem' % OVF_NAMESPACE
    VIRTUAL_SYSTEM_XPATH = './/%s' % VIRTUAL_SYSTEM_ELEM_NAME
    virtual_system_elem = doc.find(VIRTUAL_SYSTEM_XPATH)

    if virtual_system_elem is None:
        # There is no VirtualSystem element, so no ID.
        return None

    ID_ATTR_NAME = '{%s}id' % OVF_NAMESPACE
    return virtual_system_elem.attrib[ID_ATTR_NAME]


class Descriptor:
    """OVF descriptor file as defined by
    'Open Virtualization Format Specification' 1.1.0, section 6

    The system ID from the descriptor is available in the system_id attribute
    (The system_id can be None if no system ID is provided in the descriptor).

    """

    @staticmethod
    def parse(descriptor_raw_text):
        """Parses the raw descriptor text. It's an XML document."""
        return Descriptor(descriptor_raw_text)

    def __init__(self, descriptor_raw_text):
        """Don't use this, use parse()."""
        try:
            self._doc = etree_wrapper.XML(descriptor_raw_text)
        except etree_wrapper.ParseError:
            raise InvalidDescriptorDocument(_('The descriptor is not valid.'))

        self.system_id = _extract_system_id(self._doc)

    def calc_property_values(self):
        """Returns a dict where the keys are the property values defined
        by this descriptor and the value is the default value or None if
        there is no default value.

        """

        property_values = {}

        product_section_elems = self._doc.findall(
                './/{http://schemas.dmtf.org/ovf/envelope/1}ProductSection')

        for product_section_elem in product_section_elems:
            ps_class = product_section_elem.get(
                    '{http://schemas.dmtf.org/ovf/envelope/1}class')
            ps_instance = product_section_elem.get(
                    '{http://schemas.dmtf.org/ovf/envelope/1}instance')

            property_elems = product_section_elem.findall(
                    './{http://schemas.dmtf.org/ovf/envelope/1}Property')

            for property_elem in property_elems:
                key = property_elem.get(
                        '{http://schemas.dmtf.org/ovf/envelope/1}key')
                value = property_elem.get(
                        '{http://schemas.dmtf.org/ovf/envelope/1}value')
                LOG.debug("ovf property %s.%s=%s" % (ps_class, key, value))

                prop_name = ''
                if ps_class:
                    prop_name = prop_name + ps_class + '.'
                prop_name = prop_name + key
                if ps_instance:
                    prop_name = prop_name + '.' + ps_instance

                property_values[prop_name] = value

        return property_values
