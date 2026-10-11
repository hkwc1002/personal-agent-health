import argparse
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
import zipfile

import install
from health import Health, FDC_IDS
from import_foods import import_archives

ROOT = Path(__file__).resolve().parents[1]


def zip_bytes(files):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        for name, body in files.items():
            archive.writestr(name, body)
    return stream.getvalue()


class DistributionTest(unittest.TestCase):
    def test_public_food_import_is_repeatable_and_preserves_meals(self):
        ids = {**FDC_IDS, 1008: 'energy_kcal'}
        support = zip_bytes({'nutrient.csv': 'id,unit_name\n' + ''.join(
            f'{key},{"kcal" if value == "energy_kcal" else "mg" if value == "sodium_mg" else "g"}\n'
            for key, value in ids.items())})
        raw = zip_bytes({'food.csv': 'fdc_id,description\n123,Synthetic food\n',
                         'food_nutrient.csv': 'fdc_id,nutrient_id,amount\n123,1008,100\n123,1003,5\n'})
        with tempfile.TemporaryDirectory() as directory:
            health = Health(directory, network=False)
            self.assertEqual(import_archives(health, raw, support)['inserted'], 1)
            self.assertEqual(import_archives(health, raw, support)['inserted'], 0)
            food = health.food('fdc:123')
            self.assertEqual(food['nutrients']['energy_kcal'], 100)
            self.assertIsNone(food['nutrients']['sugars_g'])
            self.assertEqual(health.summary()['meal_count'], 0)

    def test_public_food_import_rejects_changed_units_before_write(self):
        support = zip_bytes({'nutrient.csv': 'id,unit_name\n1003,mg\n'})
        with tempfile.TemporaryDirectory() as directory:
            health = Health(directory, network=False)
            with self.assertRaises(ValueError):
                import_archives(health, b'invalid', support)
            self.assertEqual(health.search_foods('synthetic')['foods'], [])

    def test_plugin_registers_only_own_tools_and_skill(self):
        context = types.ModuleType('gateway.session_context')
        context.get_session_env = lambda name, default='': default
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.json'
            config.write_text(json.dumps({'socket': str(Path(directory) / 'health.sock'),
                'state_db': str(Path(directory) / 'state.db'),
                'image_roots': [str(Path(directory) / 'cache')], 'owner_id': ''}), encoding='utf-8')
            spec = importlib.util.spec_from_file_location('pah_test_plugin', ROOT / '__init__.py',
                                                         submodule_search_locations=[str(ROOT)])
            module = importlib.util.module_from_spec(spec)
            tools, skills = [], []
            ctx = types.SimpleNamespace(register_tool=lambda **kw: tools.append(kw),
                                        register_skill=lambda *args: skills.append(args))
            with patch.dict(sys.modules, {'gateway.session_context': context, 'pah_test_plugin': module}), \
                 patch.dict(os.environ, {'PERSONAL_AGENT_HEALTH_CONFIG': str(config)}):
                spec.loader.exec_module(module)
                module.register(ctx)
            self.assertEqual([tool['name'] for tool in tools], ['health_record','health_query','health_manage'])
            self.assertEqual(len(skills), 1)
            self.assertTrue(skills[0][1].is_file())
            self.assertFalse((Path(directory) / 'state.db').exists())

    def args(self, directory, **kwargs):
        root = Path(directory)
        home = root / 'hermes'
        home.mkdir(exist_ok=True)
        return argparse.Namespace(**{
            'mode': 'host', 'destination': str(root / 'health'), 'hermes_home': str(home),
            'hermes_uid': 1000, 'hermes_gid': 1000, 'service_uid': 21000,
            'owner_id': '', 'timezone': 'Asia/Shanghai', 'compose_file': [],
            'compose_project': None, 'hermes_service': None, 'hermes_container_home': None,
            **kwargs})

    def test_install_plan_has_no_public_port_or_data_mount_into_hermes(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = install.build_plan(self.args(directory))
            service = plan['compose']['services']['health']
            self.assertNotIn('ports', service)
            self.assertTrue(service['read_only'])
            self.assertNotEqual(service['user'].split(':')[0], str(plan['hermes_uid']))
            self.assertFalse(Path(plan['destination']).exists())
            self.assertEqual(plan['config']['owner_id'], '')

    def test_install_refuses_data_inside_checkout_and_shared_uid(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                install.build_plan(self.args(directory, destination=str(ROOT / 'data')))
            with self.assertRaises(ValueError):
                install.build_plan(self.args(directory, service_uid=1000))

    @unittest.skipUnless(sys.platform == 'linux', 'Linux compose bind-mount paths')
    def test_docker_override_only_touches_named_service(self):
        with tempfile.TemporaryDirectory() as directory:
            compose = Path(directory) / 'base.json'
            compose.write_text('{"services":{}}', encoding='utf-8')
            plan = install.build_plan(self.args(directory, mode='docker',
                compose_file=[str(compose)], compose_project='synthetic',
                hermes_service='agent', hermes_container_home='/profile'))
            service = plan['override']['services']['agent']
            self.assertEqual(list(plan['override']['services']), ['agent'])
            self.assertTrue(all(m['read_only'] for m in service['volumes']))
            self.assertFalse(any(m['target'] in ('/data', '/secrets') for m in service['volumes']))


if __name__ == '__main__':
    unittest.main()
