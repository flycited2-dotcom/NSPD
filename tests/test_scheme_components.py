import copy
import json
import pytest
from land import schemes,scheme_review,georeference

ROWS='н21 4978000.00 5190000.00\nн22 4978010.00 5190000.00\nн23 4978010.00 5190010.00\nн24 4978000.00 5190010.00\nн21 4978000.00 5190000.00\n'


def heading(number=4,area='100',parent=':ЗУ1'):
    return (f'Номер контура многоконтурного земельного участка {parent}({number})\n'
            f'Площадь контура многоконтурного земельного участка {area} кв. м\n'
            'Обозначение характерных\nточек границ\nКоординаты, м\n'
            'Система координат условная 1963 г.\nX Y\n')


def test_component_metadata_axes_origin_area_and_partial_group_are_explicit():
    result=schemes.extract([(86,heading()+ROWS),(87,heading(5)+ROWS.replace('49780','49781'))])
    table=result['tables'][0]
    assert table['local_area_m2']==100 and table['stated_area_m2']==100 and table['area_difference_m2']==0
    assert table['points'][0]=={'label':'н21','x':4978000.0,'y':5190000.0,'page':86,'source_row':'н21 4978000.00 5190000.00'}
    assert table['parcel_label']==':ЗУ1' and table['component_number']==4 and table['closure']=='explicit'
    assert table['table_kind']=='parcel_component' and table['purpose_hint']=='multi_contour_context'
    assert not table['geometry_confirmed'] and not table['georeferenced'] and not table['crs_parameters_confirmed']
    group=result['component_groups'][0]
    assert group['observed_components']==[4,5] and group['missing_numbers_before_highest']==[1,2,3]
    assert group['accepted_area_sum_m2']==200 and not group['completeness_confirmed']
    assert result['unparsed_coordinate_pages']==[] and result['crs_status']=='parameters_missing'
    assert result['crs_mentions'][0]['label']=='Система координат условная 1963 г.'
    doc={**result,'state':'extracted','document_id':'test','title':'Test'}
    assert not georeference.choices({'documents':[doc]})
    assert 'EPSG' not in json.dumps(result) and not result['review']['area_comparisons']


@pytest.mark.parametrize('change',[
    lambda s:s.replace('X Y','Y X'),
    lambda s:s.replace('Координаты, м','Координаты, см'),
    lambda s:s.replace('кв. м','кв.мм'),
    lambda s:s.replace('Система координат условная 1963 г.','МСК-90'),
    lambda s:s.replace('(4)','(0)'),
    lambda s:s.replace('(4)','(201)'),
])
def test_unknown_header_units_or_component_identifier_is_rejected(change):
    result=schemes.extract([(1,change(heading())+ROWS)])
    assert result['tables'][0]['state']=='rejected' and not result['tables'][0]['outline_xy']
    assert result['unparsed_coordinate_pages']==[1]


@pytest.mark.parametrize('rows,reason',[
    (ROWS.replace('н24','н23'),'Повтор обозначения'),
    (ROWS.rsplit('н21',1)[0],'Нет явного замыкания'),
    (ROWS.replace('н24 4978000.00 5190010.00','н24 4978010.00 5190000.00'),'Повтор координат'),
    (ROWS.replace('н24','нО4'),'Строка точки'),
    (ROWS+'н25 4978020.00 5190020.00\n','после замыкания'),
    (ROWS+'нО5 4978020.00 5190020.00\n','после замыкания'),
    ('н1 0 0\nн2 10 10\nн3 0 10\nн4 10 0\nн1 0 0\n','Self-intersection'),
    ('н1 0 0\nн2 1 1\nн1 0 0\n','Меньше трёх'),
    ('н1 0 0\nн2 1 0\nн3 2 0\nн1 0 0\n','Self-intersection'),
])
def test_disputed_points_and_open_rings_never_produce_geometry(rows,reason):
    result=schemes.extract([(86,heading()+rows)])
    table=result['tables'][0]
    assert table['state']=='rejected' and table['local_area_m2'] is None and table['outline_xy'] is None
    assert any(reason in x['reason'] for x in table['issues'])
    assert result['component_groups'][0]['rejected_components']==1
    assert result['review']['accepted_count']==0


def test_component_continues_across_pages_without_guessing_missing_pages():
    lines=ROWS.splitlines(keepends=True)
    result=schemes.extract([(86,heading()+''.join(lines[:2])),(87,'87\n'+''.join(lines[2:]))])
    assert result['tables'][0]['pages']==[86,87] and result['tables'][0]['local_area_m2']==100
    assert result['tables'][0]['points'][2]['page']==87
    limited=schemes.apply_page_limit(schemes.extract([(40,heading()+''.join(lines[:2]))]),40,1)
    assert limited['tables'][0]['state']=='rejected' and not limited['tables'][0]['outline_xy']
    assert limited['component_groups'][0]['accepted_components']==0


def test_duplicate_component_identity_is_rejected_and_parent_labels_stay_separate():
    result=schemes.extract([(1,heading()+ROWS),(2,heading()+ROWS),(3,heading(parent=':ЗУ2')+ROWS)])
    assert [t['state'] for t in result['tables']]==['rejected','rejected','review_required']
    assert result['component_groups'][0]['accepted_components']==0
    assert result['component_groups'][1]['accepted_components']==1


def test_missing_page_between_vertices_does_not_produce_partial_geometry():
    lines=ROWS.splitlines(keepends=True)
    result=schemes.extract([(86,heading()+''.join(lines[:2])),(87,''),(88,''.join(lines[2:]))])
    table=result['tables'][0]
    assert table['state']=='rejected' and table['outline_xy'] is None
    assert any('пропущена страница' in x['reason'] for x in table['issues'])


def test_stated_area_discrepancy_is_preserved_for_review_without_geometry_repair():
    table=schemes.extract([(1,heading(area='101')+ROWS)])['tables'][0]
    assert table['local_area_m2']==100 and table['stated_area_m2']==101
    assert table['stated_area_disagrees'] and table['area_rounding_tolerance_m2']==.5
    assert table['state']=='review_required' and not table['geometry_confirmed']


def test_next_section_number_heading_is_not_an_extra_point():
    result=schemes.extract([(89,heading(17)+ROWS),(90,'Номер кадастрового квартала: 90:12:000000\n')])
    assert result['tables'][0]['state']=='review_required'
    assert result['tables'][0]['pages']==[89]


def test_header_without_any_points_is_a_visible_rejection():
    result=schemes.extract([(86,heading())])
    assert result['tables'][0]['state']=='rejected' and result['tables'][0]['points']==[]
    result=schemes.extract([(22,boundary())])
    assert not result['project_boundaries'][0]['rings'] and result['project_boundaries'][0]['issues']


def boundary():
    return 'Координаты границ территории проектирования\nТаблица 3\n'


def test_unlabelled_project_boundary_keeps_two_rings_separate_from_parcel_areas():
    first=ROWS.replace('н','');second=first.replace('21','31').replace('22','32').replace('23','33').replace('24','34')
    result=schemes.extract([(22,boundary()+first+second),(23,'8. КРАСНЫЕ ЛИНИИ\n')])
    assert result['tables']==[] and not result['review']['accepted_count']
    assert result['unparsed_coordinate_pages']==[]
    b=result['project_boundaries'][0]
    assert b['axes_status']=='unlabelled_columns' and not b['units_confirmed'] and len(b['rings'])==2
    assert all(r['state']=='review_required' and r['closure']=='explicit' for r in b['rings'])
    assert b['rings'][0]['points'][0]['column_1']==4978000
    assert 'outline_xy' not in b['rings'][0] and 'local_area_m2' not in b['rings'][0]
    assert not b['geometry_confirmed'] and not b['georeferenced']
    doc={**result,'state':'extracted','document_id':'d','title':'test'}
    assert georeference.choices({'documents':[doc]})==[]


def test_boundary_bad_ring_and_bad_continuation_are_visible():
    result=schemes.extract([(22,boundary()+ROWS.replace('н','')+'5 49780О0 5190000\n')])
    b=result['project_boundaries'][0]
    assert len(b['rings'])==1 and b['issues'] and not b['units_confirmed']
    result=schemes.extract([(22,boundary()+ROWS.rsplit('н21',1)[0])])
    b=result['project_boundaries'][0]
    assert b['rings'][0]['state']=='rejected' and b['rings'][0]['outline_columns'] is None


def test_legacy_parcels_and_review_do_not_include_boundary_rings():
    from test_schemes import HEADER,ROWS as OLD_ROWS
    old=schemes.extract([(19,HEADER+OLD_ROWS)])
    result=schemes.extract([(19,HEADER+OLD_ROWS),(22,boundary()+ROWS.replace('н',''))])
    assert old['tables']==result['tables'] and old['review']==result['review']
    assert len(result['project_boundaries'])==1
