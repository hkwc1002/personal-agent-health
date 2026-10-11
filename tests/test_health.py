"""业务验收：重复、修订、缺失、单位、历史目标及恢复，不使用真实个人数据。"""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from health import Health, interval, validate_density, day


def mass(best, low=None, high=None):
    return {'low': best if low is None else low, 'best': best, 'high': best if high is None else high}


class HealthTest(unittest.TestCase):
    def setUp(self):
        self.test_root = Path(os.environ.get('HEALTH_TEST_ROOT', Path(__file__).resolve().parent / '_test-data'))
        self.test_root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=self.test_root)
        self.h = Health(self.temp.name, network=False)
        self.food = {'id':'fixture:rice','name':'测试食物，不是真实营养数据', 'aliases':['米饭'],
            'basis':'per_100_g','carb_definition':'available','source':{'kind':'synthetic fixture'},
            'nutrients':validate_density({'energy_kcal':100,'protein_g':5,'fat_g':2,
                'carbs_g':15,'fiber_g':1,'sugars_g':None,'added_sugars_g':None,'sodium_mg':10})}
        self.h.cache_food(self.food)

    def tearDown(self):
        assert Path(self.temp.name).resolve().is_relative_to(self.test_root.resolve())
        self.temp.cleanup()

    def item(self, grams=100, key='rice'):
        return {'name':'测试米饭','key':key,'mass_g':mass(grams),'food_id':self.food['id']}

    def record(self, source='m1', **extra):
        return self.h.record({'items':[self.item()], **extra},source)

    def profile(self, **extra):
        return self.h.update_profile({'age':35,'sex':'male','height_cm':175,'weight_kg':80,
            'goal_weight_kg':75,'activity':'light','strength_days':2,'screening_complete':True,
            'health_limits':[], **extra}, 'profile1')

    def test_sum_and_ranges(self):
        self.h.record({'items':[{**self.item(),'mass_g':mass(100,80,150)}]},'m1')
        self.h.record({'items':[self.item(50)]},'m2')
        v=self.h.summary()['totals']['energy_kcal']
        self.assertEqual((v['low'],v['best'],v['high']),(130,150,200))

    def test_cached_food_search_preserves_cooking_state(self):
        self.h.cache_food({**self.food,'id':'fixture:cooked','name':'Rice, white, long-grain, cooked','aliases':[]})
        self.h.cache_food({**self.food,'id':'fixture:raw','name':'Rice, white, long-grain, raw','aliases':[]})
        result = self.h.search_foods('rice cooked')
        self.assertTrue(result['cached'])
        self.assertEqual([f['id'] for f in result['foods']],['fixture:cooked'])
        self.assertEqual(self.h.search_foods('米饭')['foods'][0]['id'],self.food['id'])
        with self.assertRaises(ValueError): self.h.search_foods(', / ()')

    def test_same_event_hundred_replays(self):
        for _ in range(100): self.record()
        self.assertEqual(self.h.summary()['meal_count'],1)

    def test_concurrent_retries(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(lambda _:self.record(),range(40)))
        self.assertEqual(sum(not r['duplicate'] for r in results),1)

    def test_same_image_new_message(self):
        self.record(image_hashes=['a'*64])
        r=self.record('m2',image_hashes=['a'*64])
        self.assertTrue(r['duplicate'])
        self.assertEqual(self.h.summary()['meal_count'],1)

    def test_same_image_another_meal_explicit(self):
        self.record(image_hashes=['a'*64])
        self.record('m2',image_hashes=['a'*64],new_meal=True)
        self.assertEqual(self.h.summary()['meal_count'],2)

    def test_same_image_another_day(self):
        yesterday=(date.fromisoformat(day())-timedelta(days=1)).isoformat()
        self.record(day=yesterday,image_hashes=['a'*64])
        self.record('m2',image_hashes=['a'*64])
        self.assertEqual(self.h.summary()['meal_count'],1)

    def test_more_views_do_not_double(self):
        first=self.record()['record']
        self.record('m2',record_id=first['id'])
        self.assertEqual(self.h.summary()['totals']['energy_kcal']['best'],100)
        self.assertEqual(self.h.meal(first['id'])['version'],2)

    def test_multidish(self):
        self.h.record({'items':[self.item(),self.item(50,'second')]},'m1')
        self.assertEqual(self.h.summary()['totals']['energy_kcal']['best'],150)

    def test_amend_mass_preserves_source(self):
        r=self.record()['record']
        args={'record_id':r['id'],'expected_version':1,'item_changes':[{'key':'rice','mass_g':mass(50)}], 'reason':'只吃一半'}
        self.h.amend(args,'correct1')
        self.h.amend(args,'correct1')
        self.assertEqual(self.h.summary()['totals']['energy_kcal']['best'],50)
        self.assertEqual(self.h.meal(r['id'])['version'],2)
        with self.h.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM revisions').fetchone()[0],2)

    def test_optimistic_lock(self):
        r=self.record()['record']
        args={'record_id':r['id'],'expected_version':1,'items':[self.item(50)],'reason':'改份量'}
        self.h.amend(args,'c1')
        with self.assertRaisesRegex(ValueError,'已被更新'):
            self.h.amend(args,'c2')

    def test_delete_then_resend_not_restore(self):
        r=self.record()['record']
        self.h.amend({'record_id':r['id'],'expected_version':1,'delete':True,'reason':'取消'},'c1')
        self.assertEqual(self.h.summary()['meal_count'],0)
        self.assertTrue(self.record()['duplicate'])

    def test_unknown_food_saved_without_confirmation(self):
        r=self.h.record({'items':[{'name':'看不清的菜','mass_g':mass(200,100,400)}]},'m1')
        self.assertEqual(r['record']['status'],'incomplete')
        self.assertEqual(self.h.summary()['meal_count'],1)
        self.assertIsNone(r['daily']['totals']['energy_kcal']['best'])

    def test_missing_sugar_is_not_zero(self):
        self.record()
        v=self.h.summary()['totals']['sugars_g']
        self.assertIsNone(v['best']);self.assertEqual(v['missing_items'],1)

    def test_partial_energy_no_false_remaining(self):
        self.profile();self.record()
        self.h.record({'items':[{'name':'未知','mass_g':mass(100)}]},'m2')
        s=self.h.summary()
        self.assertIsNone(s['remaining']['energy_kcal'])
        self.assertEqual(s['totals']['energy_kcal']['known_best'],100)

    def test_kj_and_salt_conversion(self):
        n=validate_density({'energy_kj':418.4,'salt_g':1})
        self.assertAlmostEqual(n['energy_kcal'],100)
        self.assertEqual(n['sodium_mg'],400)

    def test_per_serving_not_whole_package(self):
        item={'name':'两份包装','mass_g':mass(100), 'carb_definition':'available',
              'label':{'basis':'per_serving_g','basis_amount':50,'nutrients':{'energy_kcal':200,'carbs_g':20}}}
        self.h.record({'items':[item]},'label1')
        self.assertEqual(self.h.summary()['totals']['energy_kcal']['best'],400)

    def test_ml_label(self):
        self.h.record({'items':[{'name':'饮料','volume_ml':mass(250),'carb_definition':'available',
            'label':{'basis':'per_100_ml','nutrients':{'energy_kcal':40,'carbs_g':10}}}]},'liquid1')
        self.assertEqual(self.h.summary()['totals']['energy_kcal']['best'],100)

    def test_ml_g_mismatch_rejected(self):
        with self.assertRaises(ValueError):
            self.h.record({'items':[{'name':'饮料','volume_ml':mass(250),
                'label':{'basis':'per_100_g','nutrients':{'energy_kcal':40}}}]},'l1')

    def test_fiber_conversion_declared(self):
        self.food['carb_definition']='by_difference';self.h.cache_food(self.food)
        r=self.record()['record']['data']['items'][0]
        self.assertEqual(r['density']['carbs_g'],14)
        self.assertIn('carb_conversion',r['source'])

    def test_unknown_carb_not_mixed_silently(self):
        self.h.record({'items':[self.item(), {'name':'标签','mass_g':mass(100),
            'label':{'basis':'per_100_g','nutrients':{'energy_kcal':100,'carbs_g':15}}}]},'mix')
        self.assertFalse(self.h.summary()['totals']['carbs_g']['complete'])

    def test_profile_in_stages_gets_target(self):
        self.h.update_profile({'age':35},'p1')
        r=self.profile()
        self.assertTrue(r['target_saved']);self.assertTrue(self.h.targets_for(day())['eligible'])

    def test_profile_updates_do_not_overwrite_target(self):
        self.profile(); old=self.h.targets_for(day())
        self.h.update_profile({'weight_kg':78},'p2')
        self.assertEqual(self.h.targets_for(day()),old)

    def test_risks_invalidate_ordinary_target(self):
        self.profile();self.h.update_profile({'health_limits':['妊娠']},'p2')
        self.assertFalse(self.h.targets_for(day())['eligible'])

    def test_minor_does_not_get_deficit(self):
        self.assertFalse(self.profile(age=16)['initial_target']['eligible'])

    def test_history_target_effective_date(self):
        self.profile()
        today=day();tomorrow=(date.fromisoformat(today)+timedelta(days=1)).isoformat()
        original=self.h.targets_for(today)
        values={k:original[k] for k in ('energy_kcal','protein_g','fat_g','carbs_g')}
        values['energy_kcal']+=100;values['carbs_g']+=25
        self.h.set_targets(values,tomorrow,'t1')
        self.assertEqual(self.h.targets_for(today),original)
        self.assertEqual(self.h.targets_for(tomorrow)['energy_kcal'],values['energy_kcal'])

    def test_extreme_target_rejected(self):
        self.profile()
        with self.assertRaises(ValueError):
            self.h.set_targets({'energy_kcal':500,'protein_g':30,'fat_g':20,'carbs_g':50},day(),'t1')

    def test_workout_not_added_to_budget(self):
        self.profile();self.record();before=self.h.summary()['remaining']
        self.h.workout({'activity':'run','minutes':30},'w1')
        self.assertEqual(self.h.summary()['remaining'],before)

    def test_measure_dedup(self):
        for _ in range(5): self.h.record_measure({'weight_kg':80},'weight1')
        self.assertEqual(len(self.h.trend()['measurements']),1)

    def test_trend_requires_enough_data(self):
        self.h.record_measure({'weight_kg':80},'w1')
        self.assertIsNone(self.h.trend()['weekly_change_kg'])

    def test_trend_7day_average(self):
        end=date.fromisoformat(day())
        for i in range(20):
            self.h.record_measure({'day':(end-timedelta(days=19-i)).isoformat(),'weight_kg':80-i*.05},str(i))
        trend=self.h.trend()
        self.assertIsNotNone(trend['weekly_change_kg'])
        self.assertEqual(trend['weight_series'][-1]['samples_7d'],7)
        self.assertFalse(trend['automatic_target_change'])

    def test_week_denominator_missing_days(self):
        self.profile();self.record()
        self.assertEqual(self.h.week()['complete_energy_days'],0)
        self.h.mark_complete(day())
        self.assertEqual(self.h.week()['complete_energy_days'],1)

    def test_new_food_invalidates_complete_day(self):
        self.record();self.h.mark_complete(day());self.record('m2')
        self.assertFalse(self.h.summary()['user_marked_complete'])

    def test_backup_restores_totals(self):
        self.record();backup=self.h.backup()
        restored=Health(Path(self.temp.name)/'restore',network=False)
        with sqlite3.connect(Path(self.temp.name)/'backups'/backup['backup']) as src, restored.connect() as dst:
            src.backup(dst)
        src.close()
        self.assertEqual(restored.summary()['totals'],self.h.summary()['totals'])

    def test_export_and_explicit_erase(self):
        self.profile();self.record();self.h.backup()
        self.assertEqual(len(self.h.export()['data']['meals']),1)
        with self.assertRaises(ValueError): self.h.erase('其他文字')
        self.h.erase('删除全部健康数据')
        self.assertEqual(self.h.summary()['meal_count'],0)
        self.assertEqual(self.h.profile(),{})

    def test_invalid_numeric_ranges(self):
        for value in [float('nan'),float('inf'),-1,True,'100']:
            with self.subTest(value=value),self.assertRaises(ValueError): interval({'low':value,'best':100,'high':200})
        with self.assertRaises(ValueError):interval({'low':200,'best':100,'high':300})

    def test_unknown_fields_and_duplicate_keys(self):
        with self.assertRaises(ValueError):self.h.record({'items':[self.item()],'command':'delete'},'bad')
        with self.assertRaises(ValueError):self.h.record({'items':[self.item(),self.item()]},'bad')


if __name__=='__main__':
    unittest.main(verbosity=2)
