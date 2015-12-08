# =================================================================
# =================================================================

from nova.compute.ibm import etree_wrapper as ElementTree


class OvfEnv:
    """ovf-env.xml file as defined by
    'Open Virtualization Format Specification' 1.1.0, section 11.1
    """

    def __init__(self, virtual_system_id):

        if not virtual_system_id:
            virtual_system_id = ''

        template = '''<?xml version="1.0" ?>
<Environment ovfenv:id="%(id)s"
             xmlns="http://schemas.dmtf.org/ovf/environment/1"
             xmlns:ovf="http://schemas.dmtf.org/ovf/envelope/1"
             xmlns:ovfenv="http://schemas.dmtf.org/ovf/environment/1"
             xmlns:rasd=\
"http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/\
CIM_ResourceAllocationSettingData"
             xmlns:vssd=\
"http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/\
CIM_VirtualSystemSettingData"
             xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
    <PlatformSection>
    <Locale>en</Locale>
  </PlatformSection>
  <PropertySection>
  </PropertySection>
</Environment>
''' % dict(id=virtual_system_id)

        self._ovf_env_xml = ElementTree.XML(template)

        self._pfs_elem = self._ovf_env_xml.find(
            './{http://schemas.dmtf.org/ovf/environment/1}PropertySection')

    def set_property(self, key, value):
        p_elem = ElementTree.SubElement(self._pfs_elem, "Property")
        p_elem.set('{http://schemas.dmtf.org/ovf/environment/1}key', key)
        p_elem.set('{http://schemas.dmtf.org/ovf/environment/1}value',
                   value)

    def set_properties(self, properties):
        for key, value in properties.iteritems():
            self.set_property(key, value)

    def calc_raw(self):
        """Returns raw text for ovf-env.xml."""
        raw_text = ElementTree.tostring(self._ovf_env_xml)
        return raw_text
