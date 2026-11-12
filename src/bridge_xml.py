"""Bounded XML parsing without document types or entity declarations."""
import xml.etree.ElementTree as ET

MAX_XML_BYTES = 65536


def parse_xml(data):
    if not isinstance(data, bytes) or len(data) > MAX_XML_BYTES:
        raise ET.ParseError('Invalid XML input size or type')
    # Removing zero bytes catches ASCII declaration keywords in UTF-16/32 too,
    # before the old Expat parser can process any internal entity definitions.
    declarations = data.replace(b'\0', b'').upper()
    if b'<!DOCTYPE' in declarations or b'<!ENTITY' in declarations:
        raise ET.ParseError('DTD and entity declarations are not supported')
    return ET.fromstring(data)
