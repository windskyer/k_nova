# =================================================================
# =================================================================

import re

from nova.compute.ibm import etree_wrapper as ElementTree
from nova import exception
from nova.openstack.common import log as logging

from nova.openstack.common.gettextutils import _


LOG = logging.getLogger(__name__)


class InvalidXml(exception.NovaException):
    """Raised when the document passed in to parse() is not valid."""
    message = _('The unattend XML in the configuration metadata for the image'
                ' is not valid.')


class NoSuchElement(exception.NovaException):
    """Raised when the XPATH passed to set() doesn't match any element in the
       doc.

    """
    message = _("The path provided doesn't match any element in the unattend"
                " template. The path is '%(path)s.'")


def parse(raw_text):
    """Parse XML text to an Unattend.

       Raises InvalidXml if the document is not valid XML.

    """
    return Unattend(raw_text)


class Unattend:
    _split_path_attr_re = re.compile('^(.*?)(@([a-zA-Z_:][-a-zA-Z0-9_:.]*))?$')

    def __init__(self, raw_text):
        """Don't use this, use parse() instead!"""
        ElementTree.register_namespace('',
                        'urn:schemas-microsoft-com:unattend')
        ElementTree.register_namespace('xsi',
                        'http://www.w3.org/2001/XMLSchema-instance')
        ElementTree.register_namespace('wcm',
                        'http://schemas.microsoft.com/WMIConfig/2002/State')
        try:
            self._doc = ElementTree.XML(raw_text)
        except ElementTree.ParseError:
            LOG.debug('Unattend was passed an invalid XML document: %s',
                      raw_text)
            raise InvalidXml

    def set(self, path_attr, new_value):
        """Sets the element text or attribute value in the document.

           path_attr is like path[@attr-name], where path is an XPATH, and
           attr-name is an attribute name.

           If the @attr-name is present, then the attribute for the element
           is set to new_value.

           If the @attr-name is not present, the text for the element is set
           to new_value.

           If no element is found, raises NoSuchElement.

        """

        (path, attr_name) = self._split_path_attr(path_attr)

        elem = self._doc.find(path)

        if elem is None:
            LOG.debug('No element found for XPATH passed to set : %s',
                      path_attr)
            raise NoSuchElement(path=path)

        if attr_name:
            # Attribute given, so setting attribute value.
            elem.set(attr_name, new_value)
        else:
            # No attribute given, so setting element text.
            elem.text = new_value

    def get_doc(self):
        """Returns the parsed doc."""
        return self._doc

    def calc_raw_text(self):
        """Returns the unattend.xml as text."""
        return ElementTree.tostring(self._doc)

    @staticmethod
    def _split_path_attr(path_attr):
        """Splits a path[@attr-name] string into the path and attr-name.
           Returns (path, attr-name), if no @attr-name, attr-name is None.

        """

        m = Unattend._split_path_attr_re.match(path_attr)

        (path, attr_name) = m.group(1, 3)
        return (path, attr_name)
