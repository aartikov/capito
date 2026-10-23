"""view(): paths, depth, limits, inline scalars and JSON projection, independent of any API."""
import json

import pytest

from capito.results import Result, Secrets
from capito.view import MISSING, parse, select
from helpers import LIMITED_VIEW


EVENT = {
    'id': 'e-1', 'title': 'Evening walk', 'status': 'RECRUITING', 'endedAt': None,
    'location': {'coordinates': {'latitude': 54.8, 'longitude': 83.1}, 'precision': 'EXACT'},
    'participants': [{'user': {'id': 'u-1', 'name': 'Luna', 'age': 30, 'gender': 'FEMALE'}, 'status': 'APPROVED'}],
    'tags': ['a', 'b', 'c', 'd', 'e', 'f', 'g'],
}


def result(body=EVENT, **fields):
    return Result(policy=Secrets.http(), limits=LIMITED_VIEW['result'], success=True, status=200, method='GET',
                  route='/events/{id}', duration=0.2, body=body, **fields)


def test_paths_select_keys_list_items_and_report_absence():
    assert parse('body.participants[].user.name') == ['body', 'participants', [], 'user', 'name']
    tree = {'body': EVENT}
    assert select(tree, 'body.participants[].user.name') == ['Luna']
    assert select(tree, 'body.participants[0].status') == 'APPROVED'
    assert select(tree, 'body.participants[-1].user.id') == 'u-1'
    assert select(tree, 'body.endedAt') is None
    assert select(tree, 'body.nothing') is MISSING and select(tree, 'body.title.deeper') is MISSING
    with pytest.raises(ValueError, match='Bad path'):
        parse('body..x')


def test_print_is_one_header_line():
    r = result()
    assert str(r) == r.view() == '200 GET /events/{id} · 0.20s · body {id, title, status, +4} ↓3'


def test_depth_expands_levels_and_collapses_the_boundary():
    r = result()
    assert r.view('body.location', depth=0).splitlines()[1:] == ['body.location: {coordinates, precision} ↓2']
    assert r.view('body.location').splitlines()[1:] == [
        'body.location:', '  coordinates: {latitude, longitude}', '  precision: EXACT']
    assert r.view('body.location', depth=2).splitlines()[1:] == [
        'body.location:', '  coordinates:', '    latitude: 54.8', '    longitude: 83.1', '  precision: EXACT']
    assert r.view('body.location', depth='all') == r.view('body.location', depth=2)


def test_lists_do_not_consume_a_level_and_scalar_lists_stay_inline():
    r = result()
    assert r.view('body.participants').splitlines()[1:] == [
        'body.participants: [1]', '  - user: {id, name, age, +1}', '    status: APPROVED']
    assert r.view('body.participants', depth=0).splitlines()[1:] == ['body.participants: [1] {user, status} ↓2']
    body = r.view('body').splitlines()
    assert '  endedAt: null' in body and '  tags: [a, b, c, d, e, +2 (items=5)]' in body
    assert '  participants: [1] {user, status} ↓2' in body


def test_collapsed_nodes_tell_how_many_levels_they_hide():
    r = result()
    # ↓N = levels below the node to its deepest leaf; depth + N from the same path reaches every leaf.
    assert '  location: {coordinates, precision} ↓2' in r.view('body')
    assert '    coordinates: {latitude, longitude}' in r.view('body', depth=2)  # only values inside: no marker
    assert '↓' not in r.view('body', depth=3).split('\n', 1)[1]
    deep = result({'a': [{'b': 1}, {'b': {'c': {'d': 1}}}], 'e': {}})  # a list takes the deepest element
    assert deep.view('body').splitlines()[1:] == ['body:', '  a: [2] {b} ↓3', '  e: {}']


def test_scalar_paths_join_the_header_line_and_absence_differs_from_null():
    line = result().view('body.status', 'body.endedAt', 'body.nothing', 'body.participants[].user.name')
    assert line == ('200 GET /events/{id} · 0.20s · body {id, title, status, +4} ↓3 · '
                    'body.status=RECRUITING body.endedAt=null body.nothing=<missing> body.participants[].user.name=[Luna]')


def test_every_limit_names_itself_and_all_lifts_it():
    long = {'text': 'x' * 100, 'items': [{'n': i} for i in range(8)]}
    r = result(long)
    text = r.view('body')
    assert f"text: {'x' * 80}…(+20, width=80)" in text
    assert 'x' * 100 in r.view('body', width='all')
    listed = r.view('body.items')
    assert '  … +3 (items=5)' in listed and r.view('body.items', items='all').count('- n:') == 8
    capped = r.view('body.items', items='all', lines=4).splitlines()
    assert len(capped) == 5 and capped[-1] == '… 6 more lines (lines=4); narrow the paths or raise lines'


def test_json_keeps_nesting_ignores_limits_and_masks():
    r = Result(policy=Secrets.http().extend(key_words=('token',)), success=True, status=200,
               body={'accessToken': 'secret', 'items': [{'id': i, 'text': 'y' * 200} for i in range(7)]})
    projected = json.loads(r.view('body.items[].id', 'body.accessToken', 'body.absent', format='json'))
    assert projected == {'body': {'items': [{'id': i} for i in range(7)], 'accessToken': '<redacted>'}}
    full = json.loads(r.view(format='json'))
    assert len(full['body']['items']) == 7 and len(full['body']['items'][0]['text']) == 200
    assert 'secret' not in r.view('body', depth='all', items='all', width='all', lines='all')


def test_bytes_and_text_bodies_are_shown_by_shape():
    assert str(result(b'\x00' * 12)).endswith('body <12 bytes>')
    assert str(result('plain text')).endswith('body plain text')
    with pytest.raises(ValueError, match="format is 'text' or 'json'"):
        result().view('body', format='yaml')


def test_limits_come_from_the_kind_and_none_is_rejected():
    plain = Result(policy=Secrets.http(), success=True, status=200, body={'text': 'x' * 100})
    assert 'x' * 100 in plain.view('body')  # without configured limits nothing is cut
    with pytest.raises(TypeError, match='use depth="all"'):
        result().view('body', depth=None)
    with pytest.raises(ValueError, match='non-negative'):
        result().view('body', lines=-1)
