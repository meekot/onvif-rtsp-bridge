#!/usr/bin/env python3
"""Extend a single-camera configuration, preserving the original camera."""
import argparse
import copy
import datetime
import ipaddress
import json
import os
import stat
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), 'src'))
from bridge_config import normalize


def extend(config, first_ip, first_channel, count):
    if len(config['cameras']) != 1 or first_channel < 1 or count < 1:
        raise ValueError('Expected one camera and positive channel/count')
    result = copy.deepcopy(config)
    original = result['cameras'][0]
    address = ipaddress.IPv4Address(first_ip)
    for offset in range(count):
        channel = first_channel + offset
        if channel == original['channel']:
            raise ValueError('Channel already exists')
        camera = copy.deepcopy(original)
        for key in ('uuid', 'mac', 'serial_number', 'interface_name', 'route_metric'):
            camera.pop(key, None)
        camera.update(id='channel-{}'.format(channel), ip=str(address + offset),
                      channel=channel, name='Recorder Channel {}'.format(channel))
        result['cameras'].append(camera)
    normalize(copy.deepcopy(result))
    return result


def write_config(path, config):
    metadata = os.stat(path)
    directory = os.path.dirname(os.path.abspath(path))
    with open(path, 'rb') as handle:
        previous = handle.read()
    backup = path + '.backup-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as handle:
        handle.write(previous)
        handle.flush()
        os.fsync(handle.fileno())
    descriptor, temporary = tempfile.mkstemp(prefix='.bridge-config-', dir=directory)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            json.dump(config, handle, indent=2, ensure_ascii=True)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.chown(temporary, metadata.st_uid, metadata.st_gid)
        os.chmod(temporary, stat.S_IMODE(metadata.st_mode))
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return backup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--first-ip', required=True)
    parser.add_argument('--first-channel', type=int, default=2)
    parser.add_argument('--count', type=int, default=8)
    parser.add_argument('--check', action='store_true', help='Preview without writing')
    args = parser.parse_args()
    try:
        with open(args.config, encoding='utf-8') as handle:
            config = extend(json.load(handle), args.first_ip, args.first_channel, args.count)
        if args.check:
            print('Valid proposed configuration: {} cameras; no files changed.'.format(len(config['cameras'])))
        else:
            if os.geteuid() != 0:
                raise ValueError('Root required')
            import subprocess
            if subprocess.call(['systemctl', 'is-active', '--quiet', 'bridge-network.service']) == 0:
                raise ValueError('Stop the bridge target before modifying its configuration')
            print('Configuration updated. Private backup:', write_config(args.config, config))
        for camera in config['cameras']:
            print('Channel {}: {}'.format(camera['channel'], camera['ip']))
    except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError):
        parser.exit(1, 'Could not update configuration. Check schema, sudo privileges, '
                    'single-camera input and that bridge-network.service is stopped. '
                    'Configuration values are not logged.\n')


if __name__ == '__main__':
    main()
