import json
import pytest
from land import planning_enquiry as p


def data(stale=False):
    candidate = {'id':'contour','area_m2':1000,'point':[34.203,44.993],
                 'geometry':{'type':'Polygon','coordinates':[[[34.2,44.99],[34.21,44.99],[34.21,45],[34.2,44.99]]]},
                 'rights_confirmed':False,'srzu_ready':False}
    return {'stale':stale,'result':{'id':'version','created_at':'original-calculation-date','candidates':[candidate],
            'parameters':{'purpose':'housing'},'source_inputs':{'planning_maps':'dated-maps'},
            'sources':{'planning':{'id':'dated-catalog'},'regulations':{'id':'dated-text'}}}}


def test_request_pins_geometry_source_versions_and_is_not_a_submission():
    d = data(); text = p.enquiry(d,'contour').decode()
    assert 'НЕ ОТПРАВЛЕН' in text and 'original-calculation-date' in text
    assert 'dated-catalog' in text and 'dated-text' in text and 'dated-maps' in text
    assert 'ИЖС' in text and 'не подтверждённый ВРИ' in text
    assert 'не заявление о предоставлении земли, ГПЗУ' in text
    assert 'систему координат' in text and 'всех действующих изменений' in text
    assert json.dumps(d['result']['candidates'][0]['geometry'],ensure_ascii=False) in text
    assert d['result']['candidates'][0]['rights_confirmed'] is False


def test_geometry_is_exact_and_unknown_even_for_historical_version():
    d = data(True); gj = p.geometry(d,'contour')
    assert gj['features'][0]['geometry'] == d['result']['candidates'][0]['geometry']
    assert gj['source_result_id'] == 'version' and gj['stale']
    assert not gj['territorial_zone_confirmed'] and not gj['regulation_current_confirmed']
    assert not gj['features'][0]['properties']['is_srzu']
    assert 'устарела' in p.enquiry(d,'contour').decode()


def test_unknown_candidate_is_rejected_and_dossier_link_is_dated():
    d = data()
    with pytest.raises(ValueError,match='не найден'):
        p.enquiry(d,'other')
    html = p.dossier_section(d['result'],d['result']['candidates'][0],True)
    assert '/api/recon/planning-request?result_id=version&amp;id=contour' in html
    assert 'Расчёт устарел' in html and 'Проект не отправлен' in html
