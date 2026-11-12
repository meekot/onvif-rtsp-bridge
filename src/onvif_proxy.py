#!/usr/bin/env python3
import os
import socket
import threading
import uuid
import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from xml.sax.saxutils import escape
import xml.etree.ElementTree as ET
from bridge_config import load_config, stream_uri
from bridge_network import bind_device
from bridge_xml import parse_xml


SOAP_NS = (
    "http://www.w3.org/2003/05/soap-envelope"
)
TDS_NS = "http://www.onvif.org/ver10/device/wsdl"
TRT_NS = "http://www.onvif.org/ver10/media/wsdl"
TT_NS = "http://www.onvif.org/ver10/schema"


def envelope(content):
    return (
        '<s:Envelope xmlns:s="{}" xmlns:tds="{}" '
        'xmlns:trt="{}" xmlns:tt="{}">'
        '<s:Body>{}</s:Body></s:Envelope>'
    ).format(SOAP_NS, TDS_NS, TRT_NS, TT_NS, content)


def handle_soap(body, config, camera):
    CAMERA, VIDEO, CONFIG = camera, camera['video'], config
    LOCAL_IP = camera['ip']
    DAHUA_IP = config['upstream']['host']
    HTTP_PORT, CHANNEL = camera['onvif_port'], camera['channel']
    DEVICE_URL = device_url(camera)
    MEDIA_URL = 'http://{}:{}/onvif/media_service'.format(LOCAL_IP, HTTP_PORT)
    RTSP_URL = stream_uri(config, camera)
    try:
        root = parse_xml(body)
        soap_body = root.find('{%s}Body' % SOAP_NS)
        action = list(soap_body)[0].tag.rsplit('}', 1)[-1]
    except (ET.ParseError, TypeError, IndexError):
        action = ''
    if config['bridge']['log_requests']:
        known_actions = ('GetNetworkInterfaces', 'GetSnapshotUri',
                         'GetVideoEncoderConfigurationOptions', 'GetSystemDateAndTime',
                         'GetDeviceInformation', 'GetCapabilities', 'GetServices',
                         'GetProfiles', 'GetProfile', 'GetStreamUri', 'GetVideoSources')
        print('ONVIF {} ({}): {}'.format(camera['id'], camera['ip'],
              action if action in known_actions else 'unsupported action'), flush=True)
    body = ('<' + action + '>').encode('ascii', errors='ignore')
    if b"GetNetworkInterfaces" in body:
        return envelope(
            "<tds:GetNetworkInterfacesResponse>"
            '<tds:NetworkInterfaces token="' + escape(CAMERA['interface_name']) + '">'
            "<tt:Enabled>true</tt:Enabled>"
            "<tt:Info>"
            "<tt:Name>" + escape(CAMERA['interface_name']) + "</tt:Name>"
            "<tt:HwAddress>" + escape(CAMERA['mac']) + "</tt:HwAddress>"
            "<tt:MTU>1500</tt:MTU>"
            "</tt:Info>"
            "<tt:IPv4>"
            "<tt:Enabled>true</tt:Enabled>"
            "<tt:Config>"
            "<tt:Manual>"
            "<tt:Address>" + LOCAL_IP + "</tt:Address>"
            "<tt:PrefixLength>" + str(CAMERA['prefix_length']) + "</tt:PrefixLength>"
            "</tt:Manual>"
            "<tt:DHCP>false</tt:DHCP>"
            "</tt:Config>"
            "</tt:IPv4>"
            "</tds:NetworkInterfaces>"
            "</tds:GetNetworkInterfacesResponse>"
        )
    if b"GetSnapshotUri" in body:
        snapshot_url = escape(CONFIG['upstream']['snapshot_url'].format(
            host=DAHUA_IP, channel=CHANNEL, subtype=CAMERA['subtype']))

        return envelope(
            "<trt:GetSnapshotUriResponse>"
            "<trt:MediaUri>"
            "<tt:Uri>{}</tt:Uri>"
            "<tt:InvalidAfterConnect>false</tt:InvalidAfterConnect>"
            "<tt:InvalidAfterReboot>false</tt:InvalidAfterReboot>"
            "<tt:Timeout>PT0S</tt:Timeout>"
            "</trt:MediaUri>"
            "</trt:GetSnapshotUriResponse>"
        ).format(snapshot_url)

    if b"GetVideoEncoderConfigurationOptions" in body:
        return envelope(
            "<trt:GetVideoEncoderConfigurationOptionsResponse>"
            "<trt:Options>"
            "<tt:QualityRange>"
            "<tt:Min>1</tt:Min>"
            "<tt:Max>6</tt:Max>"
            "</tt:QualityRange>"
            "<tt:H264>"
            "<tt:ResolutionsAvailable>"
            "<tt:Width>" + str(VIDEO['width']) + "</tt:Width>"
            "<tt:Height>" + str(VIDEO['height']) + "</tt:Height>"
            "</tt:ResolutionsAvailable>"
            "<tt:GovLengthRange>"
            "<tt:Min>1</tt:Min>"
            "<tt:Max>150</tt:Max>"
            "</tt:GovLengthRange>"
            "<tt:FrameRateRange>"
            "<tt:Min>1</tt:Min>"
            "<tt:Max>" + str(VIDEO['fps']) + "</tt:Max>"
            "</tt:FrameRateRange>"
            "<tt:EncodingIntervalRange>"
            "<tt:Min>1</tt:Min>"
            "<tt:Max>1</tt:Max>"
            "</tt:EncodingIntervalRange>"
            "<tt:H264ProfilesSupported>Main</tt:H264ProfilesSupported>"
            "</tt:H264>"
            "</trt:Options>"
            "</trt:GetVideoEncoderConfigurationOptionsResponse>"
        )
    if b"GetSystemDateAndTime" in body:
        now = datetime.datetime.utcnow()
        return envelope(
            "<tds:GetSystemDateAndTimeResponse>"
            "<tds:SystemDateAndTime>"
            "<tt:DateTimeType>NTP</tt:DateTimeType>"
            "<tt:DaylightSavings>false</tt:DaylightSavings>"
            "<tt:UTCDateTime>"
            "<tt:Time>"
            "<tt:Hour>{}</tt:Hour>"
            "<tt:Minute>{}</tt:Minute>"
            "<tt:Second>{}</tt:Second>"
            "</tt:Time>"
            "<tt:Date>"
            "<tt:Year>{}</tt:Year>"
            "<tt:Month>{}</tt:Month>"
            "<tt:Day>{}</tt:Day>"
            "</tt:Date>"
            "</tt:UTCDateTime>"
            "</tds:SystemDateAndTime>"
            "</tds:GetSystemDateAndTimeResponse>"
        ).format(
            now.hour, now.minute, now.second,
            now.year, now.month, now.day
        )
    if b"GetDeviceInformation" in body:
        return envelope(
            "<tds:GetDeviceInformationResponse>"
            "<tds:Manufacturer>" + escape(CAMERA['manufacturer']) + "</tds:Manufacturer>"
            "<tds:Model>" + escape(CAMERA['model']) + "</tds:Model>"
            "<tds:FirmwareVersion>" + escape(CAMERA['firmware']) + "</tds:FirmwareVersion>"
            "<tds:SerialNumber>" + escape(CAMERA['serial_number']) + "</tds:SerialNumber>"
            "<tds:HardwareId>" + escape(CAMERA['hardware_id']) + "</tds:HardwareId>"
            "</tds:GetDeviceInformationResponse>"
        )

    if b"GetCapabilities" in body:
        return envelope(
            "<tds:GetCapabilitiesResponse>"
            "<tds:Capabilities>"
            '<tt:Device><tt:XAddr>{}</tt:XAddr></tt:Device>'
            '<tt:Media><tt:XAddr>{}</tt:XAddr>'
            "<tt:StreamingCapabilities>"
            "<tt:RTPMulticast>false</tt:RTPMulticast>"
            "<tt:RTP_TCP>true</tt:RTP_TCP>"
            "<tt:RTP_RTSP_TCP>true</tt:RTP_RTSP_TCP>"
            "</tt:StreamingCapabilities></tt:Media>"
            "</tds:Capabilities>"
            "</tds:GetCapabilitiesResponse>"
        ).format(DEVICE_URL, MEDIA_URL)

    if b"GetServices" in body:
        return envelope(
            "<tds:GetServicesResponse>"
            '<tds:Service><tds:Namespace>{}</tds:Namespace>'
            "<tds:XAddr>{}</tds:XAddr>"
            "<tds:Version><tt:Major>2</tt:Major>"
            "<tt:Minor>0</tt:Minor></tds:Version>"
            "</tds:Service>"
            '<tds:Service><tds:Namespace>{}</tds:Namespace>'
            "<tds:XAddr>{}</tds:XAddr>"
            "<tds:Version><tt:Major>2</tt:Major>"
            "<tt:Minor>0</tt:Minor></tds:Version>"
            "</tds:Service>"
            "</tds:GetServicesResponse>"
        ).format(TDS_NS, DEVICE_URL, TRT_NS, MEDIA_URL)

    if action in ("GetProfiles", "GetProfile"):
        return envelope(
            ("<trt:GetProfileResponse>" if action == 'GetProfile' else "<trt:GetProfilesResponse>") +
            ('<trt:Profile token="profile1" fixed="true">' if action == 'GetProfile' else '<trt:Profiles token="profile1" fixed="true">') +
            "<tt:Name>" + escape(CAMERA['name']) + "</tt:Name>"
            '<tt:VideoSourceConfiguration token="sourcecfg1">'
            "<tt:Name>Source</tt:Name>"
            "<tt:UseCount>1</tt:UseCount>"
            "<tt:SourceToken>source1</tt:SourceToken>"
            "<tt:Bounds x=\"0\" y=\"0\" width=\"" + str(VIDEO['width']) + "\" height=\"" + str(VIDEO['height']) + "\"/>"
            "</tt:VideoSourceConfiguration>"
            '<tt:VideoEncoderConfiguration token="enc1">'
            "<tt:Name>H264</tt:Name>"
            "<tt:UseCount>1</tt:UseCount>"
            "<tt:Encoding>H264</tt:Encoding>"
            "<tt:Resolution><tt:Width>" + str(VIDEO['width']) + "</tt:Width>"
            "<tt:Height>" + str(VIDEO['height']) + "</tt:Height></tt:Resolution>"
            "<tt:Quality>4</tt:Quality>"
            "<tt:RateControl><tt:FrameRateLimit>" + str(VIDEO['fps']) + "</tt:FrameRateLimit>"
            "<tt:EncodingInterval>1</tt:EncodingInterval>"
            "<tt:BitrateLimit>" + str(VIDEO['bitrate_kbps']) + "</tt:BitrateLimit>"
            "</tt:RateControl>"
            "<tt:H264><tt:GovLength>" + str(VIDEO['gov_length']) + "</tt:GovLength>"
            "<tt:H264Profile>Main</tt:H264Profile></tt:H264>"
            "<tt:Multicast><tt:Address><tt:Type>IPv4</tt:Type>"
            "<tt:IPv4Address>0.0.0.0</tt:IPv4Address>"
            "</tt:Address><tt:Port>0</tt:Port>"
            "<tt:TTL>1</tt:TTL><tt:AutoStart>false</tt:AutoStart>"
            "</tt:Multicast>"
            "<tt:SessionTimeout>PT60S</tt:SessionTimeout>"
            "</tt:VideoEncoderConfiguration>"
            + ('</trt:Profile></trt:GetProfileResponse>' if action == 'GetProfile'
               else '</trt:Profiles></trt:GetProfilesResponse>')
        )

    if b"GetStreamUri" in body:
        return envelope(
            "<trt:GetStreamUriResponse>"
            "<trt:MediaUri>"
            "<tt:Uri>{}</tt:Uri>"
            "<tt:InvalidAfterConnect>false</tt:InvalidAfterConnect>"
            "<tt:InvalidAfterReboot>false</tt:InvalidAfterReboot>"
            "<tt:Timeout>PT60S</tt:Timeout>"
            "</trt:MediaUri>"
            "</trt:GetStreamUriResponse>"
        ).format(escape(RTSP_URL))

    if b"GetVideoSources" in body:
        return envelope(
            "<trt:GetVideoSourcesResponse>"
            '<trt:VideoSources token="source1">'
            "<tt:Framerate>" + str(VIDEO['fps']) + "</tt:Framerate>"
            "<tt:Resolution><tt:Width>" + str(VIDEO['width']) + "</tt:Width>"
            "<tt:Height>" + str(VIDEO['height']) + "</tt:Height></tt:Resolution>"
            "</trt:VideoSources>"
            "</trt:GetVideoSourcesResponse>"
        )

    return envelope(
        '<s:Fault><s:Code><s:Value>s:Sender</s:Value>'
        '</s:Code><s:Reason>'
        '<s:Text xml:lang="en">Action not implemented</s:Text>'
        '</s:Reason></s:Fault>'
    )


class ONVIFHandler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(10)
        BaseHTTPRequestHandler.setup(self)

    def log_message(self, format, *args):
        pass

    def do_POST(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            if length < 0 or length > 65536:
                raise ValueError()
        except ValueError:
            self.send_error(413)
            return
        body = self.rfile.read(length)
        response = handle_soap(body, self.server.config, self.server.camera).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "application/soap+xml")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def do_GET(self):
        content = b"ONVIF RTSP Bridge is running"
        self.send_response(200)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


def device_url(camera):
    return 'http://{}:{}/onvif/device_service'.format(camera['ip'], camera['onvif_port'])


def discovery_response(camera, relates_to):
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope '
        'xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
        'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
        'xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
        '<s:Header>'
        '<a:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/'
        'ProbeMatches</a:Action>'
        '<a:MessageID>urn:uuid:{}</a:MessageID>'
        '<a:RelatesTo>{}</a:RelatesTo>'
        '<a:To>http://schemas.xmlsoap.org/ws/2004/08/'
        'addressing/role/anonymous</a:To>'
        '</s:Header>'
        '<s:Body><d:ProbeMatches><d:ProbeMatch>'
        '<a:EndpointReference>'
        '<a:Address>urn:uuid:{}</a:Address>'
        '</a:EndpointReference>'
        '<d:Types>dn:NetworkVideoTransmitter</d:Types>'
        '<d:Scopes>onvif://www.onvif.org/type/video_encoder'
        '</d:Scopes>'
        '<d:XAddrs>{}</d:XAddrs>'
        '<d:MetadataVersion>1</d:MetadataVersion>'
        '</d:ProbeMatch></d:ProbeMatches></s:Body>'
        '</s:Envelope>'
    ).format(
        uuid.uuid4(), escape(relates_to), camera['uuid'], device_url(camera)
    )



def discovery_server(config, ready):
    sockets = []
    try:
        group = '239.255.255.250'
        for camera in config['cameras']:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sockets.append((sock, camera))
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            bind_device(sock, config, camera)
            # Wildcard receives multicast; device binding isolates each macvlan.
            sock.bind(('', 3702))
            membership = socket.inet_aton(group) + socket.inet_aton(camera['ip'])
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(camera['ip']))
        ready.set()
        import select
        by_socket = dict(sockets)
        while True:
            readable, _, _ = select.select(list(by_socket), [], [], 30)
            for sock in readable:
                data, address = sock.recvfrom(65535)
                try:
                    root = parse_xml(data)
                    probe = root.find('.//{http://schemas.xmlsoap.org/ws/2005/04/discovery}Probe')
                    if probe is None:
                        continue
                    message_id = root.find('.//{http://schemas.xmlsoap.org/ws/2004/08/addressing}MessageID')
                    relates_to = message_id.text if message_id is not None and message_id.text else ''
                except ET.ParseError:
                    continue
                response = discovery_response(by_socket[sock], relates_to)
                # A connected temporary socket fixes the source IP in existing mode too.
                reply = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                try:
                    bind_device(reply, config, by_socket[sock])
                    reply.bind((by_socket[sock]['ip'], 0))
                    reply.sendto(response.encode('utf-8'), address)
                finally:
                    reply.close()
    except Exception:
        print('WS-Discovery failed; check interfaces, capabilities and UDP 3702.', flush=True)
        os._exit(1)
    finally:
        for sock, camera in sockets:
            sock.close()


class CameraHTTPServer(HTTPServer):
    def __init__(self, config, camera):
        self.config, self.camera = config, camera
        HTTPServer.__init__(self, (camera['ip'], camera['onvif_port']), ONVIFHandler,
                            bind_and_activate=False)
        try:
            bind_device(self.socket, config, camera)
            self.server_bind()
            self.server_activate()
        except Exception:
            self.server_close()
            raise


def serve_http(server):
    try:
        server.serve_forever()
    except Exception:
        print('ONVIF listener failed.', flush=True)
        os._exit(1)


def main():
    config = load_config()
    servers = []
    try:
        for camera in config['cameras']:
            servers.append(CameraHTTPServer(config, camera))
        ready = threading.Event()
        discovery = threading.Thread(target=discovery_server, args=(config, ready))
        discovery.daemon = True
        discovery.start()
        if not ready.wait(15):
            raise RuntimeError('Discovery startup timed out')
        for server in servers:
            thread = threading.Thread(target=serve_http, args=(server,))
            thread.daemon = True
            thread.start()
        print('ONVIF ready: {} camera(s).'.format(len(servers)), flush=True)
        while True:
            threading.Event().wait(60)
    except KeyboardInterrupt:
        pass
    finally:
        for server in servers:
            server.server_close()


if __name__ == '__main__':
    main()
