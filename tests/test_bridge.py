"""Offline regression checks; no camera or third-party packages required."""
import copy
import json
import os
import socket
import sys
import threading
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch, mock_open

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))
sys.path.insert(0, os.path.join(ROOT, 'deploy'))
import add_channels
import bridge_config
import bridge_network
import onvif_proxy
import rtsp_proxy
from bridge_xml import parse_xml, MAX_XML_BYTES


def fixture():
    with open(os.path.join(ROOT, 'config', 'config.example.json')) as handle:
        return json.load(handle)


def soap(action):
    return ('<s:Envelope xmlns:s="{}"><s:Body><m:{} xmlns:m="{}"/>'
            '</s:Body></s:Envelope>').format(onvif_proxy.SOAP_NS, action,
                                           onvif_proxy.TRT_NS).encode('utf-8')


class ConfigurationTests(unittest.TestCase):
    def test_request_logging_is_disabled_by_default(self):
        config = bridge_config.normalize(fixture())
        self.assertFalse(config['bridge']['log_requests'])
        raw = fixture()
        raw['bridge']['log_requests'] = 'true'
        with self.assertRaises(ValueError):
            bridge_config.normalize(raw)

    def test_xml_rejects_dtd_and_entities_in_all_unicode_encodings(self):
        entity = '<!DOCTYPE x [<!ENTITY name "expanded">]><x>&name;</x>'
        for encoding in ('utf-8', 'utf-16', 'utf-16-le', 'utf-16-be',
                         'utf-32', 'utf-32-le', 'utf-32-be'):
            with self.assertRaises(ET.ParseError):
                parse_xml(entity.encode(encoding))
        with self.assertRaises(ET.ParseError):
            parse_xml(b'<!ENTITY name "expanded">')
        with self.assertRaises(ET.ParseError):
            parse_xml(b' ' * (MAX_XML_BYTES + 1))
        config = bridge_config.normalize(fixture())
        response = onvif_proxy.handle_soap(entity.encode('utf-8'), config, config['cameras'][0])
        self.assertIn('<s:Fault>', response)

    def test_xml_accepts_normal_soap_in_utf8_and_utf16(self):
        for encoding in ('utf-8', 'utf-16'):
            document = '<root><value>camera &amp; channel</value></root>'.encode(encoding)
            self.assertEqual('camera & channel', parse_xml(document).find('value').text)

    def test_add_channels_preserves_camera_and_credentials(self):
        raw = fixture()
        raw['cameras'] = raw['cameras'][:1]
        raw['cameras'][0].update(uuid='c573591c-846e-4dc5-b50a-e8923b79d15e',
                                 mac='02:00:00:00:00:01', serial_number='original-camera')
        raw['upstream']['username'] = 'fixture-user'
        raw['upstream']['password'] = 'fixture-password'
        previous = copy.deepcopy(raw)
        result = add_channels.extend(raw, '192.0.2.102', 2, 8)
        self.assertEqual(previous, raw)
        self.assertEqual(previous['cameras'][0], result['cameras'][0])
        self.assertEqual(previous['upstream'], result['upstream'])
        normalized = bridge_config.normalize(result)
        self.assertEqual(9, len(normalized['cameras']))
        self.assertEqual('192.0.2.109', normalized['cameras'][-1]['ip'])
        self.assertEqual(9, len(set(c['uuid'] for c in normalized['cameras'])))
        with self.assertRaises(ValueError):
            add_channels.extend(normalized, '192.0.2.102', 2, 8)

    def test_stable_unique_identities_and_no_channel_limit(self):
        raw = fixture()
        raw['cameras'] = [{'id': 'cam-{}'.format(i), 'ip': '10.10.{}.{}'.format(i // 250, i % 250 + 1),
                           'channel': i + 1} for i in range(100)]
        first = bridge_config.normalize(copy.deepcopy(raw))
        second = bridge_config.normalize(copy.deepcopy(raw))
        self.assertEqual(first, second)
        for key in ('ip', 'uuid', 'mac', 'interface_name', 'serial_number'):
            self.assertEqual(100, len(set(c[key] for c in first['cameras'])))

    def test_duplicate_and_invalid_values_rejected(self):
        for key in ('id', 'ip'):
            raw = fixture()
            raw['cameras'][1][key] = raw['cameras'][0][key]
            with self.assertRaises(ValueError):
                bridge_config.normalize(raw)
        raw = fixture()
        raw['cameras'][0]['ip'] = '192.0.2.255'
        with self.assertRaises(ValueError):
            bridge_config.normalize(raw)

    def test_all_soap_responses_and_camera_isolation(self):
        raw = fixture()
        raw['upstream']['username'] = 'user@example'
        raw['upstream']['password'] = 'fake<&:@ password'
        raw['cameras'][0]['name'] = 'Entrance <A> & yard'
        raw['cameras'][1]['video'] = {'width': 1280, 'height': 720, 'fps': 15}
        config = bridge_config.normalize(raw)
        actions = ['GetNetworkInterfaces', 'GetSnapshotUri', 'GetVideoEncoderConfigurationOptions',
                   'GetSystemDateAndTime', 'GetDeviceInformation', 'GetCapabilities', 'GetServices',
                   'GetProfiles', 'GetProfile', 'GetStreamUri', 'GetVideoSources', 'Unsupported']
        for camera in config['cameras']:
            for action in actions:
                xml = onvif_proxy.handle_soap(soap(action), config, camera)
                root = ET.fromstring(xml)
                self.assertIsNotNone(root.find('{%s}Body' % onvif_proxy.SOAP_NS))
            root = ET.fromstring(onvif_proxy.handle_soap(soap('GetStreamUri'), config, camera))
            self.assertEqual(bridge_config.stream_uri(config, camera),
                             root.find('.//{%s}Uri' % onvif_proxy.TT_NS).text)
            xml = onvif_proxy.handle_soap(soap('GetNetworkInterfaces'), config, camera)
            self.assertIn(camera['ip'], xml)
            self.assertIn(camera['mac'], xml)
            root = ET.fromstring(onvif_proxy.discovery_response(camera, 'urn:test&escaped'))
            self.assertEqual(onvif_proxy.device_url(camera),
                             root.find('.//{http://schemas.xmlsoap.org/ws/2005/04/discovery}XAddrs').text)
        first, second = config['cameras'][:2]
        for camera in (first, second):
            xml = onvif_proxy.handle_soap(soap('GetProfiles'), config, camera)
            root = ET.fromstring(xml)
            self.assertEqual(camera['name'], root.find('.//{%s}Name' % onvif_proxy.TT_NS).text)
            self.assertIn('<tt:Width>{}</tt:Width>'.format(camera['video']['width']), xml)


class NetworkTests(unittest.TestCase):
    def test_boot_waits_until_parent_routes_appear(self):
        raw = fixture()
        raw['network']['gateway'] = '192.0.2.1'
        config = bridge_config.normalize(raw)
        with patch.object(bridge_network, 'parent_ready', side_effect=[False, False, True]), \
                patch.object(bridge_network.time, 'sleep') as sleep:
            bridge_network.wait_for_parent(config)
            self.assertEqual(2, sleep.call_count)

    def test_boot_wait_has_finite_deadline(self):
        config = bridge_config.normalize(fixture())
        with patch.object(bridge_network, 'parent_ready', return_value=False), \
                patch.object(bridge_network.time, 'monotonic', side_effect=[0, 61]), \
                patch.object(bridge_network.time, 'sleep') as sleep:
            with self.assertRaises(RuntimeError):
                bridge_network.wait_for_parent(config)
            self.assertFalse(sleep.called)

    def test_parent_not_ready_without_configured_gateway_route(self):
        raw = fixture()
        raw['network']['gateway'] = '192.0.2.1'
        config = bridge_config.normalize(raw)
        responses = ['2: eth0 inet 192.0.2.10/24 scope global eth0\n',
                     '192.0.2.0/24 dev eth0 metric 202\n']
        with patch.object(bridge_network, 'command', side_effect=responses):
            self.assertFalse(bridge_network.parent_ready(config))

    def test_gateway_routes_have_distinct_metrics_and_follow_link_up(self):
        raw = fixture()
        raw['network']['gateway'] = '192.0.2.1'
        config = bridge_config.normalize(raw)
        metrics = set()
        for camera, group in bridge_network.plan(config):
            route = group[-1]
            self.assertEqual(['route', 'add', 'default', 'via', '192.0.2.1'], route[:5])
            self.assertLess(group.index(['link', 'set', 'dev', camera['interface_name'], 'up']), len(group) - 1)
            metrics.add(route[-1])
        self.assertEqual(9, len(metrics))
        raw['network']['gateway'] = '198.51.100.1'
        with self.assertRaises(ValueError):
            bridge_config.normalize(raw)

    def test_existing_manual_gateway_is_preserved(self):
        raw = fixture()
        raw['network']['gateway'] = '192.0.2.1'
        config = bridge_config.normalize(raw)
        camera = config['cameras'][0]
        output = 'default via 192.0.2.1 dev {} src {} metric 42700\n'.format(camera['interface_name'], camera['ip'])
        with patch.object(bridge_network, 'command', return_value=output) as command:
            bridge_network.ensure_gateway(config, camera)
            self.assertEqual(1, command.call_count)

    def test_parent_arp_preflight_is_read_only_and_rejects_default_policy(self):
        config = bridge_config.normalize(fixture())
        for value, accepted in (('0', False), ('1', True), ('2', True), ('8', False)):
            mocked = mock_open(read_data=value)
            with patch('builtins.open', mocked):
                if accepted:
                    bridge_network.check_parent_arp(config)
                else:
                    with self.assertRaises(RuntimeError):
                        bridge_network.check_parent_arp(config)
            self.assertEqual(2, mocked.call_count)
            for call in mocked.call_args_list:
                self.assertEqual(1, len(call[0]))
            self.assertFalse(mocked().write.called)

    def test_idempotent_up_preserves_matching_owned_interfaces(self):
        config = bridge_config.normalize(fixture())
        def fake_command(args):
            if args[:4] == ['-o', '-4', 'address', 'show']:
                return '2: eth0 inet 192.0.2.10/24 scope global eth0\n'
            if args[:3] == ['-4', 'route', 'show']:
                return '192.0.2.0/24 dev eth0 proto kernel scope link src 192.0.2.10 metric 202\n'
            return ''
        with patch.object(bridge_network.os, 'geteuid', return_value=0, create=True), \
                patch.object(bridge_network.os.path, 'exists', return_value=True), \
                patch.object(bridge_network, 'matches', return_value=True), \
                patch.object(bridge_network, 'check_parent_arp'), \
                patch.object(bridge_network, 'child_sysctls'), \
                patch.object(bridge_network, 'command', side_effect=fake_command) as command:
            bridge_network.apply(config, 'up')
            commands = [call[0][0] for call in command.call_args_list]
            self.assertFalse(any(args[:2] in (['link', 'add'], ['link', 'delete']) for args in commands))
            self.assertEqual(9, len([args for args in commands if args[-1] == 'up']))

    def test_plan_never_changes_parent_or_default_route(self):
        config = bridge_config.normalize(fixture())
        commands = [args for camera, group in bridge_network.plan(config) for args in group]
        self.assertEqual(45, len(commands))
        self.assertFalse(any('default' in args or 'flush' in args for args in commands))
        for args in commands:
            if args[:2] in (['link', 'set'], ['address', 'add'], ['route', 'add']):
                self.assertTrue(args[args.index('dev') + 1].startswith('ovb'))
            if args[:2] == ['address', 'add']:
                self.assertIn('noprefixroute', args)
        for camera, group in bridge_network.plan(config):
            up = ['link', 'set', 'dev', camera['interface_name'], 'up']
            route = next(args for args in group if args[:2] == ['route', 'add'])
            self.assertLess(group.index(up), group.index(route))

    def test_setup_failure_rolls_back_only_new_interfaces(self):
        config = bridge_config.normalize(fixture())
        config['cameras'] = config['cameras'][:2]
        calls = []
        def fake_command(args):
            calls.append(args)
            if args[:4] == ['-o', '-4', 'address', 'show']:
                return '2: eth0 inet 192.0.2.10/24 brd 192.0.2.255 scope global eth0\n'
            if args[:3] == ['-4', 'route', 'show']:
                return '192.0.2.0/24 dev eth0 proto kernel scope link src 192.0.2.10 metric 202\n'
            if args[:2] == ['address', 'add'] and config['cameras'][1]['ip'] in args[2]:
                raise OSError('Simulated address assignment failure')
            return ''
        with patch.object(bridge_network.os, 'geteuid', return_value=0, create=True), \
                patch.object(bridge_network.os.path, 'exists', return_value=False), \
                patch.object(bridge_network, 'check_parent_arp'), \
                patch.object(bridge_network, 'child_sysctls'), \
                patch.object(bridge_network, 'command', side_effect=fake_command):
            with self.assertRaises(OSError):
                bridge_network.apply(config, 'up')
        deletes = [args[-1] for args in calls if args[:2] == ['link', 'delete']]
        self.assertEqual([c['interface_name'] for c in reversed(config['cameras'])], deletes)

    def test_down_does_not_delete_unowned_interface(self):
        config = bridge_config.normalize(fixture())
        with patch.object(bridge_network.os, 'geteuid', return_value=0, create=True), \
                patch.object(bridge_network, 'owned', return_value=False), \
                patch.object(bridge_network, 'command', return_value='') as command:
            bridge_network.apply(config, 'down')
            self.assertEqual(1, command.call_count)

    def test_existing_mode_does_not_invoke_ip(self):
        config = bridge_config.normalize(fixture())
        config['network']['mode'] = 'existing'
        with patch.object(bridge_network, 'command') as command:
            bridge_network.apply(config, 'up')
            bridge_network.apply(config, 'down')
            self.assertFalse(command.called)


class SocketTests(unittest.TestCase):
    def test_concurrent_http_camera_identity(self):
        from http.client import HTTPConnection
        config = bridge_config.normalize(fixture())
        config['network']['mode'] = 'existing'
        servers, threads = [], []
        try:
            for camera in config['cameras'][:2]:
                camera['ip'], camera['onvif_port'] = '127.0.0.1', 0
                server = onvif_proxy.CameraHTTPServer(config, camera)
                camera['onvif_port'] = server.server_address[1]
                servers.append(server)
                thread = threading.Thread(target=server.serve_forever)
                thread.daemon = True
                thread.start()
                threads.append(thread)
            for server in servers:
                client = HTTPConnection(*server.server_address, timeout=5)
                try:
                    client.request('POST', '/onvif/device_service', soap('GetDeviceInformation'))
                    response = client.getresponse()
                    self.assertEqual(200, response.status)
                    self.assertIn(server.camera['serial_number'].encode('ascii'), response.read())
                finally:
                    client.close()
        finally:
            for server in servers:
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(5)

    def test_rtsp_digest_headers_and_binary_payload_unchanged(self):
        # Exercise the real socket relay, including data larger than BUFFER_SIZE.
        payload = (b'DESCRIBE rtsp://192.0.2.101:554/cam/realmonitor?channel=2&subtype=0 RTSP/1.0\r\n'
                   b'Authorization: Digest username="fake", uri="rtsp://192.0.2.101:554/cam/realmonitor?channel=2&subtype=0", response="fake"\r\n\r\n'
                   + bytes(bytearray(range(256))) * 1024)
        reply = b'RTSP/1.0 401 Unauthorized\r\nWWW-Authenticate: Digest realm="fixture", nonce="fake"\r\n\r\n' + payload[::-1]
        upstream = socket.socket()
        upstream.bind(('127.0.0.1', 0))
        upstream.listen(2)
        upstream.settimeout(5)
        errors, received, workers = [], [], []
        def exact(sock, length):
            data = b''
            while len(data) < length:
                chunk = sock.recv(min(65536, length - len(data)))
                if not chunk:
                    break
                data += chunk
            return data
        def recorder():
            try:
                for index in range(2):
                    sock, address = upstream.accept()
                    sock.settimeout(5)
                    try:
                        received.append(exact(sock, len(payload)))
                        sock.sendall(reply)
                    finally:
                        sock.close()
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=recorder)
        thread.daemon = True
        thread.start()
        config = bridge_config.normalize(fixture())
        config['network']['mode'] = 'existing'
        config['bridge']['rtsp_port'] = 0
        config['upstream']['host'], config['upstream']['rtsp_port'] = upstream.getsockname()
        servers = []
        try:
            for camera in config['cameras'][:2]:
                camera['ip'] = '127.0.0.1'
                server = rtsp_proxy.create_server(config, camera)
                servers.append(server)
                worker = threading.Thread(target=server.serve_forever)
                worker.daemon = True
                worker.start()
                workers.append(worker)
            for server in servers:
                client = socket.create_connection(server.server_address, timeout=5)
                try:
                    client.sendall(payload[:31])
                    client.sendall(payload[31:])
                    self.assertEqual(reply, exact(client, len(reply)))
                finally:
                    client.close()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual([], errors)
            self.assertEqual([payload, payload], received)
        finally:
            upstream.close()
            for server in servers:
                server.shutdown()
                server.server_close()
            for worker in workers:
                worker.join(5)


if __name__ == '__main__':
    unittest.main()
