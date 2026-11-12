"""Exercise installer selection using temporary paths and harmless command shims."""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@unittest.skipUnless(os.name == 'posix', 'Installer execution requires a POSIX shell')
class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.mkdtemp(prefix='bridge-install-test-')
        self.addCleanup(shutil.rmtree, self.temporary)
        self.project = os.path.join(self.temporary, 'project')
        os.makedirs(self.project)
        for directory in ('src', 'deploy', 'config'):
            shutil.copytree(os.path.join(ROOT, directory), os.path.join(self.project, directory),
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        self.config_dir = os.path.join(self.temporary, 'etc')
        os.makedirs(self.config_dir)
        self.installed = os.path.join(self.config_dir, 'config.json')
        self.units = os.path.join(self.temporary, 'units')
        os.makedirs(self.units)
        self.backups = os.path.join(self.temporary, 'backups')
        self.bin = os.path.join(self.temporary, 'bin')
        os.makedirs(self.bin)
        with open(os.path.join(ROOT, 'config', 'config.example.json')) as handle:
            self.nine = json.load(handle)
        self.one = dict(self.nine)
        self.one['cameras'] = self.nine['cameras'][:1]
        with open(os.path.join(self.project, 'config', 'config.json'), 'w') as handle:
            json.dump(self.one, handle)
        script_path = os.path.join(self.project, 'deploy', 'install.sh')
        with open(script_path) as handle:
            script = handle.read()
        substitutions = {'/opt/onvif-rtsp-bridge': os.path.join(self.temporary, 'app'),
                         '/etc/onvif-rtsp-bridge': self.config_dir,
                         '/etc/systemd/system': self.units,
                         '/var/backups/onvif-rtsp-bridge': self.backups,
                         'PYTHON=/usr/bin/python3': 'PYTHON="{}"'.format(sys.executable)}
        for old, new in substitutions.items():
            script = script.replace(old, new)
        self.assertNotIn('/etc/onvif-rtsp-bridge', script)
        self.assertNotIn('/var/backups/onvif-rtsp-bridge', script)
        with open(script_path, 'w') as handle:
            handle.write(script)
        self.script = script_path
        self.shim('id', 'echo 0')
        self.shim('getent', 'exit 0')
        self.shim('chown', 'exit 0')
        self.shim('systemctl', 'if [ "$1" = is-active ]; then exit "${BRIDGE_TEST_ACTIVE:-1}"; fi\nexit 0')
        # Retain real install/cp semantics while avoiding root ownership changes.
        real_install = shutil.which('install')
        self.shim('install', 'set -- "$@"\n'
                  'args=""\n'
                  'while [ "$#" -gt 0 ]; do\n'
                  '  case "$1" in -o|-g) shift 2;; *) args="$args \'$1\'"; shift;; esac\n'
                  'done\n'
                  'eval "exec {} $args"'.format(real_install))
        self.env = dict(os.environ)
        self.env['PATH'] = self.bin + os.pathsep + self.env['PATH']

    def shim(self, name, body):
        path = os.path.join(self.bin, name)
        with open(path, 'w') as handle:
            handle.write('#!/bin/sh\n' + body + '\n')
        os.chmod(path, 0o755)

    def run_installer(self, extra=()):
        return subprocess.check_output(['sh', self.script] + list(extra), env=self.env,
                                       stderr=subprocess.STDOUT).decode('utf-8')

    def test_default_reinstall_preserves_installed_nine_camera_config(self):
        with open(self.installed, 'w') as handle:
            json.dump(self.nine, handle)
        with open(self.installed, 'rb') as handle:
            previous = handle.read()
        output = self.run_installer()
        self.assertIn('Keeping the installed configuration.', output)
        with open(self.installed, 'rb') as handle:
            self.assertEqual(previous, handle.read())
        self.assertEqual(0o640, stat.S_IMODE(os.stat(self.installed).st_mode))
        backup = os.path.join(self.backups, os.listdir(self.backups)[0], 'config.json')
        with open(backup, 'rb') as handle:
            self.assertEqual(previous, handle.read())

    def test_explicit_config_replaces_installed_config(self):
        with open(self.installed, 'w') as handle:
            json.dump(self.nine, handle)
        self.run_installer((os.path.join(self.project, 'config', 'config.json'),))
        with open(self.installed) as handle:
            self.assertEqual(1, len(json.load(handle)['cameras']))

    def test_active_service_prevents_any_file_installation(self):
        self.env['BRIDGE_TEST_ACTIVE'] = '0'
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_installer()
        self.assertFalse(os.path.exists(self.backups))
        self.assertFalse(os.path.exists(self.installed))


if __name__ == '__main__':
    unittest.main()
