"""Import public USDA SR Legacy CSV data; never bundle a user's food cache."""
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import urllib.request
import zipfile

from health import Health, NUTRIENTS, FDC_IDS, now, dumps, validate_density

SOURCE = 'https://fdc.nal.usda.gov/fdc-datasets/FoodData_Central_sr_legacy_food_csv_2018-04.zip'
SUPPORT = 'https://fdc.nal.usda.gov/fdc-datasets/FoodData_Central_Supporting_Data_csv_2022-10-28.zip'


def download(url, limit):
    with urllib.request.urlopen(url, timeout=60) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError('USDA download exceeds size limit')
    return data


def rows(archive, filename):
    matches = [n for n in archive.namelist() if Path(n).name == filename]
    if len(matches) != 1:
        raise ValueError('Missing or ambiguous USDA table: ' + filename)
    info = archive.getinfo(matches[0])
    if info.file_size > 128 * 1024 * 1024:
        raise ValueError('USDA table exceeds size limit')
    with archive.open(info) as stream:
        yield from csv.DictReader(io.TextIOWrapper(stream, encoding='utf-8-sig'))


def import_archives(health, raw, support):
    ids = {**FDC_IDS, 1008: 'energy_kcal'}
    with zipfile.ZipFile(io.BytesIO(support)) as archive:
        units = {int(r['id']): r['unit_name'].lower() for r in rows(archive, 'nutrient.csv')}
    for nutrient, key in ids.items():
        expected = 'kcal' if key == 'energy_kcal' else 'mg' if key == 'sodium_mg' else 'g'
        if units.get(nutrient) != expected:
            raise ValueError('USDA nutrient unit changed')
    digest = hashlib.sha256(raw).hexdigest()
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        foods = {}
        for row in rows(archive, 'food.csv'):
            if not row['fdc_id'].isdigit() or row['fdc_id'] in foods:
                raise ValueError('Invalid or duplicate FDC ID')
            foods[row['fdc_id']] = {
                'id': 'fdc:' + row['fdc_id'], 'name': row['description'], 'aliases': [],
                'basis': 'per_100_g', 'carb_definition': 'by_difference',
                'nutrients': {k: None for k in NUTRIENTS},
                'source': {'kind': 'USDA FDC', 'data_type': 'SR Legacy', 'data_release': '2018-04',
                           'url': 'https://fdc.nal.usda.gov/food-details/' + row['fdc_id'],
                           'download_url': SOURCE, 'archive_sha256': digest, 'retrieved_at': now()}}
        for row in rows(archive, 'food_nutrient.csv'):
            key = ids.get(int(row['nutrient_id']))
            if key and row['fdc_id'] in foods and row['amount']:
                foods[row['fdc_id']]['nutrients'][key] = float(row['amount'])
    if not foods:
        raise ValueError('Empty USDA archive')
    for food in foods.values():
        food['nutrients'] = validate_density(food['nutrients'])
    with health.write() as connection:
        before = connection.total_changes
        for food in foods.values():
            connection.execute('INSERT OR IGNORE INTO foods VALUES(?,?,?)',
                               (food['id'], now(), dumps(food)))
        inserted = connection.total_changes - before
    return {'foods_in_archive': len(foods), 'inserted': inserted, 'archive_sha256': digest,
            'support_sha256': hashlib.sha256(support).hexdigest()}


if __name__ == '__main__':
    try:
        result = import_archives(Health(os.environ.get('HEALTH_DATA', '/data')),
                                 download(SOURCE, 12000000), download(SUPPORT, 3000000))
        print(json.dumps(result))
    except (OSError, ValueError, KeyError, zipfile.BadZipFile):
        raise SystemExit('USDA import failed. No meal was written; check network and official CSV schema.')
