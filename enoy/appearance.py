"""Durable UI preferences, independent of the local HTTP port used each launch."""
import json
import re

PALETTES = ('clay', 'sage', 'graphite', 'ocean', 'lavender', 'rose', 'amber', 'teal', 'forest', 'indigo', 'coffee', 'contrast')


def read(db):
    raw = db.kv_get('appearance_colours')
    return {'colours': json.loads(raw) if raw else {}, 'theme': db.kv_get('appearance_theme')}


def save(db, data):
    if not isinstance(data, dict):
        raise ValueError('Appearance settings must be an object.')
    if 'theme' in data:
        if data['theme'] not in ('light', 'dark', 'system'):
            raise ValueError('Choose light, dark or system theme.')
        db.kv_set('appearance_theme', data['theme'])
    else:
        mode, preset, custom = data.get('mode'), data.get('preset'), data.get('custom', {})
        if mode not in ('chat', 'code') or preset not in PALETTES or not isinstance(custom, dict):
            raise ValueError('Choose a workspace and colour preset.')
        if any(key not in ('accent', 'background', 'sidebar', 'panel') or not isinstance(value, str)
               or not re.fullmatch(r'#[0-9a-fA-F]{6}', value) for key, value in custom.items()):
            raise ValueError('Custom colours must be six-digit hex colours.')
        current = read(db)['colours']
        current[mode] = {'preset': preset, 'custom': custom}
        db.kv_set('appearance_colours', json.dumps(current))
    return read(db)
