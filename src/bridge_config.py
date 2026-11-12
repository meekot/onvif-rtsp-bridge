"""Configuration shared by bridge processes (Python 3.5+)."""
import argparse
import hashlib
import ipaddress
import json
import os
import re
import uuid
from urllib.parse import quote


def load_config(network_action=False):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=os.path.join(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))), 'config', 'config.json'))
    parser.add_argument('--check-config', action='store_true')
    if network_action:
        parser.add_argument('action', choices=['up', 'down', 'plan'])
    args = parser.parse_args()
    try:
        with open(args.config, encoding='utf-8') as handle:
            config = normalize(json.load(handle))
    except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError):
        parser.exit(2, 'Invalid configuration; compare config.example.json. Values are not logged.\n')
    if args.check_config:
        print('Configuration valid: {} camera(s).'.format(len(config['cameras'])))
        parser.exit()
    return (config, args.action) if network_action else config


def integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError('Integer outside permitted range')


def text(value, allow_empty=False):
    if not isinstance(value, str) or (not value and not allow_empty):
        raise ValueError('Expected text')
    if any(ord(c) < 32 for c in value):
        raise ValueError('Control character')
    return value


def normalize(config):
    bridge, network, source = config['bridge'], config['network'], config['upstream']
    text(bridge['id'])
    bridge.setdefault('log_requests', False)
    if type(bridge['log_requests']) is not bool:
        raise ValueError('log_requests must be a boolean')
    if network['mode'] not in ('macvlan', 'existing'):
        raise ValueError('Unknown network mode')
    if not re.match(r'^[a-zA-Z0-9_.-]{1,15}$', network['parent_interface']):
        raise ValueError('Invalid parent interface')
    integer(network['prefix_length'], 1, 32)
    network.setdefault('route_metric', 42700)
    integer(network['route_metric'], 1, 4294967295)
    network.setdefault('gateway', None)
    if network['gateway'] is not None:
        network['gateway'] = str(ipaddress.IPv4Address(network['gateway']))
    integer(bridge['rtsp_port'], 1, 65535)
    if re.search(r'[/@\s:]', text(source['host'])):
        raise ValueError('Expected IPv4 or hostname')
    integer(source['rtsp_port'], 1, 65535)
    for key in ('username', 'password', 'snapshot_url'):
        text(source[key], allow_empty=True)
    if source['password'] and not source['username']:
        raise ValueError('Password requires username')
    if not text(source['rtsp_path']).startswith('/'):
        raise ValueError('Expected absolute RTSP path')
    defaults = config.get('camera_defaults', {})
    if not isinstance(config['cameras'], list) or not config['cameras']:
        raise ValueError('Expected nonempty cameras list')
    unique = {key: set() for key in ('id', 'ip', 'uuid', 'mac', 'serial_number', 'interface_name')}
    result = []
    for index, entry in enumerate(config['cameras']):
        camera = dict(defaults)
        camera.update(entry)
        camera['video'] = dict(defaults.get('video', {}))
        camera['video'].update(entry.get('video', {}))
        identity = '{}:{}'.format(bridge['id'], text(camera['id']))
        device_uuid = uuid.uuid5(uuid.NAMESPACE_DNS, 'onvif-rtsp-bridge:' + identity)
        camera.setdefault('uuid', str(device_uuid))
        camera['uuid'] = str(uuid.UUID(camera['uuid']))
        digest = hashlib.sha256(identity.encode('utf-8')).hexdigest()
        camera.setdefault('mac', '02:' + ':'.join(digest[i:i+2] for i in range(0, 10, 2)))
        camera['mac'] = camera['mac'].lower()
        if not re.match(r'^(?:[0-9a-f]{2}:){5}[0-9a-f]{2}$', camera['mac']):
            raise ValueError('Invalid MAC')
        if int(camera['mac'][:2], 16) & 1:
            raise ValueError('Multicast MAC')
        camera.setdefault('interface_name', 'ovb' + digest[:10])
        if not re.match(r'^ovb[a-zA-Z0-9_-]{1,12}$', camera['interface_name']):
            raise ValueError('Managed interface must start with ovb')
        camera.setdefault('serial_number', str(device_uuid))
        camera.setdefault('name', 'Recorder Channel {}'.format(camera['channel']))
        camera['route_metric'] = network['route_metric'] + index
        integer(camera['route_metric'], 1, 4294967295)
        for key, value in (('manufacturer', 'ONVIF RTSP Bridge'), ('model', 'Virtual Channel'),
                           ('firmware', '0.1.0'), ('hardware_id', 'VirtualONVIF'),
                           ('subtype', 0), ('onvif_port', 8000), ('prefix_length', network['prefix_length'])):
            camera.setdefault(key, value)
        camera['ip'] = str(ipaddress.IPv4Address(camera['ip']))
        ip = ipaddress.IPv4Address(camera['ip'])
        if ip.is_multicast or ip.is_unspecified or ip.is_loopback:
            raise ValueError('Expected LAN camera IP')
        integer(camera['channel'], 1, 2147483647)
        integer(camera['subtype'], 0, 65535)
        integer(camera['onvif_port'], 1, 65535)
        integer(camera['prefix_length'], 1, 32)
        subnet = ipaddress.IPv4Interface('{}/{}'.format(camera['ip'], camera['prefix_length'])).network
        if camera['prefix_length'] < 31 and ip in (subnet.network_address, subnet.broadcast_address):
            raise ValueError('Network or broadcast address')
        if network['gateway'] is not None:
            gateway = ipaddress.IPv4Address(network['gateway'])
            if gateway not in subnet or gateway == ip or gateway in (subnet.network_address, subnet.broadcast_address):
                raise ValueError('Gateway must be a different usable IP in the camera subnet')
        if camera['ip'] == source['host']:
            raise ValueError('Camera IP must differ from recorder IP')
        if camera['onvif_port'] == bridge['rtsp_port']:
            raise ValueError('Port conflict')
        for key in ('width', 'height', 'fps', 'bitrate_kbps', 'gov_length'):
            integer(camera['video'][key], 1, 1000000)
        for key in ('name', 'manufacturer', 'model', 'firmware', 'serial_number', 'hardware_id'):
            text(camera[key])
        for key, values in unique.items():
            if camera[key] in values:
                raise ValueError('Duplicate camera identity')
            values.add(camera[key])
        source['rtsp_path'].format(channel=camera['channel'], subtype=camera['subtype'])
        source['snapshot_url'].format(host=source['host'], channel=camera['channel'], subtype=camera['subtype'])
        result.append(camera)
    config['cameras'] = result
    return config


def stream_uri(config, camera):
    source = config['upstream']
    credentials = ''
    if source['username']:
        credentials = '{}:{}@'.format(quote(source['username'], safe=''), quote(source['password'], safe=''))
    return 'rtsp://{}{}:{}{}'.format(credentials, camera['ip'], config['bridge']['rtsp_port'],
        source['rtsp_path'].format(channel=camera['channel'], subtype=camera['subtype']))
