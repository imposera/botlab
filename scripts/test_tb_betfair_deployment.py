import contextlib
import importlib.util
import io
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

import tb_betfair_preflight as preflight
from test_betfair_t15_emit import load_emitter


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.secrets = self.base / 'secrets.env'
        self.secrets.write_text('DO_NOT_READ=private\n')

    def dependencies(self):
        for name, exports in preflight.CONTRACTS.items():
            source = '\n'.join(f'def {key}(*args): pass' for key in exports)
            # A checker must not execute the module.
            (self.base / f'{name}.py').write_text(source + '\nraise RuntimeError("must not import")\n')
        (self.base / 'requests.py').write_text('')
        (self.base / 'tb_liquidity.py').write_text('')

    def test_preflight_roles_and_no_execution(self):
        self.dependencies()
        reports, errors = preflight.check('emitter', self.secrets, [str(self.base)])
        self.assertFalse(errors)
        self.assertTrue(any('source contract' in report for report in reports))
        (self.base / 'manual_arm_request.py').unlink()
        self.assertFalse(preflight.check('observer', self.secrets, [str(self.base)])[1])
        self.assertTrue(any('manual_arm_request' in e for e in preflight.check('emitter', self.secrets, [str(self.base)])[1]))

    def test_missing_dependency_and_bad_gateway_contract(self):
        self.dependencies()
        (self.base / 'requests.py').unlink()
        (self.base / 'betfair_gateway.py').write_text('def wrong(): pass\n')
        errors = preflight.check('observer', self.base / 'absent', [str(self.base)])[1]
        self.assertTrue(any('requests' in error for error in errors))
        self.assertTrue(any('expected exports' in error for error in errors))
        self.assertTrue(any('Credentials' in error for error in errors))
        self.assertNotIn('private', str(errors))

    def test_emitter_custom_credentials_path(self):
        emitter = load_emitter()
        with patch.object(sys, 'argv', ['emit', '--dry-run', '--secrets-file', str(self.secrets)]), \
             patch.object(emitter, 'read_json', return_value=None), \
             patch.object(emitter, 'load_priority_config', return_value=emitter.default_priority_config()), \
             patch.object(emitter, 'load_track_liquidity', return_value={}), \
             patch.object(emitter, 'discover_markets', return_value=[]), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(emitter.main(), 0)
        emitter.load_secrets.assert_called_once_with(self.secrets)

    def test_real_gateway_contract_with_mocked_http(self):
        requests = types.ModuleType('requests')
        response = Mock(status_code=200)
        requests.post = Mock(return_value=response)
        spec = importlib.util.spec_from_file_location('gateway_integration_test', Path(__file__).with_name('betfair_gateway.py'))
        gateway = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, requests=requests, tb_names=None):
            spec.loader.exec_module(gateway)
        self.assertEqual(gateway.runner_summary({'runnerName': '3. Example'})['runner_name'], 'Example')
        with patch.dict(os.environ, BETFAIR_API_KEY='dummy-key', BETFAIR_USERNAME='dummy-user', BETFAIR_PASSWORD='dummy-password'), \
             patch.object(gateway, 'certificate_pair', return_value=('dummy.crt','dummy.key')), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            response.json.return_value = {'loginStatus': 'SUCCESS', 'sessionToken': 'dummy-session'}
            token = gateway.betfair_login()
            response.json.return_value = [{'marketId': '1.2', 'inplay': False}]
            books = gateway.betting_api('listMarketBook', {'marketIds': ['1.2']}, token)
        self.assertEqual(books[0]['marketId'], '1.2')
        self.assertEqual(output.getvalue(), '')
        self.assertEqual(requests.post.call_args.kwargs['headers']['X-Authentication'], 'dummy-session')
        self.assertEqual(requests.post.call_args.kwargs['timeout'], 20)

        # Exercise the actual emitter, supplied gateway and manual-arm helper;
        # mock only HTTP and certificate paths, with a temporary credentials file.
        emitter_spec = importlib.util.spec_from_file_location('real_emitter_deploy_test', Path(__file__).with_name('betfair_t15_emit.py'))
        emitter = importlib.util.module_from_spec(emitter_spec)
        with patch.dict(sys.modules, betfair_gateway=gateway):
            emitter_spec.loader.exec_module(emitter)
        response.json.side_effect = [
            {'loginStatus': 'SUCCESS', 'sessionToken': 'dummy-session'}, []]
        args = ['emit', '--dry-run', '--state-dir', str(self.base/'state'),
                '--config-dir', str(self.base/'config'), '--secrets-file', str(self.secrets)]
        with patch.object(sys, 'argv', args), \
             patch.dict(os.environ, BETFAIR_API_KEY='dummy-key', BETFAIR_USERNAME='dummy-user', BETFAIR_PASSWORD='dummy-password'), \
             patch.object(gateway, 'certificate_pair', return_value=('dummy.crt', 'dummy.key')), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(emitter.main(), 0)
        self.assertIn('status=idle', output.getvalue())
        self.assertNotIn('dummy-session', output.getvalue())
        self.assertFalse((self.base/'state'/'betfair_t15_target.json').exists())



if __name__ == '__main__':
    unittest.main()
