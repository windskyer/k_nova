# =================================================================
# =================================================================

"""Wrapper around ElementTree, using either the native implementation or lxml.

This module creates a wrapper around the ElementTree library, picking an
appropriate implementation for the environment.
This module normalizes:
* the exception when parsing, normalized to ParseError
* lxml doesn't support unicode strings with encoding, so lxml converts unicode
  document to ascii.

Reasons to use this:
* not all systems have the lxml library
* Python 2.6's native ElementTree has minimal support for XPATH.

This module uses the following rule to pick the implementation:
* If using Python 2.7, uses the native implementation.
* Otherwise, if lxml is available, uses the lxml implementation.
* Otherwise, uses the native implementation.
  (In this case, XPATH support will be minimal).

References:
* Python 2.7 native:
  http://docs.python.org/2.7/library/xml.etree.elementtree.html
* Python 2.6 native:
  http://docs.python.org/2.6/library/xml.etree.elementtree.html
* lxml: http://lxml.de/

To use this module:

 import etree_wrapper
 etree_wrapper.XML(some_xml_string)

If the XML string passed to XML() is not valid, a ParseError is raised.

"""

import sys


class ParseError(Exception):
    """Raised if the XML string could not be parsed."""
    pass


class _NativeImpl:
    def XML(self, raw_str):
        from xml.etree import ElementTree
        try:
            from xml.etree.ElementTree \
            import ParseError as ImplParseError  # noqa
        except ImportError:
            from xml.parsers.expat import ExpatError as ImplParseError  # noqa
        try:
            return ElementTree.XML(raw_str)
        except ImplParseError as e:
            raise ParseError(e)

    def SubElement(self, parent, tag, attrib={}, **extra):
        from xml.etree import ElementTree
        return ElementTree.SubElement(parent, tag, attrib=attrib, **extra)

    def tostring(self, element):
        from xml.etree import ElementTree
        return ElementTree.tostring(element)

    def register_namespace(self, prefix, namespace):
        from xml.etree import ElementTree
        return ElementTree.register_namespace(prefix, namespace)


class _LxmlImpl:
    def XML(self, raw_str):
        from lxml import etree
        # lxml does not support parsing a unicode string that has an encoding
        # value, so we convert a unicode string to ascii.
        raw_str_ascii = raw_str.encode('ascii', 'replace')
        try:
            return etree.XML(raw_str_ascii)
        except etree.XMLSyntaxError as e:
            raise ParseError(e)

    def SubElement(self, parent, tag, attrib={}, **extra):
        from lxml import etree
        return etree.SubElement(parent, tag, attrib=attrib, **extra)

    def tostring(self, element):
        from lxml import etree
        return etree.tostring(element)

    def register_namespace(self, prefix, namespace):
        """This is not necessary for lxml."""
        pass


def _calc_impl_name(version, have_lxml=None):
    if version < (2, 7):
        if have_lxml:
            return 'lxml'
        return 'native'
    return 'native'


def _create_impl(impl_name):
    if impl_name == 'lxml':
        return _LxmlImpl()
    else:
        return _NativeImpl()


def _check_have_lxml():
    try:
        from lxml import etree
        return hasattr(etree, 'XML')
    except ImportError:
        return False


def _create_impl_for_system():
    version = sys.version_info
    if version < (2, 7):
        have_lxml = _check_have_lxml()
    else:
        have_lxml = None
    impl_name = _calc_impl_name(version, have_lxml=have_lxml)
    return _create_impl(impl_name)


_impl = _create_impl_for_system()


def XML(raw_str):
    """Parse the XML string.

       Raises ParseError if the raw_str could not be parsed.

    """
    return _impl.XML(raw_str)


def SubElement(parent, tag, attrib={}, **extra):
    """See the SubElement() documentation from python xml or lxml."""
    return _impl.SubElement(parent, tag, attrib=attrib, **extra)


def tostring(element):
    """See the tostring() documentation from python xml or lxml."""
    return _impl.tostring(element)


def register_namespace(prefix, namespace):
    return _impl.register_namespace(prefix, namespace)
