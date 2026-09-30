from shapely.geometry import Polygon
from land import schemes, scheme_review


def table(label, coords, role='formed_label', state='review_required'):
    return {'label':label,'outline_xy':list(map(list,Polygon(coords).exterior.coords)),
            'state':state,'purpose_hint':role}


def context():
    return scheme_review.context([(7,'Общая площадь образуемых земельных участков в границах земельного участка '
        'с кадастровым номером 90:12:171301:1630 площадью 8477 кв.м. составляет 5838 кв.м. '
        'Расположенного в кадастров ых кварталах 90:12:170301.'),
        (19,'Путем раздела земельного участка с кадастровым номе-\nром 90:12:171301:1630 '
            'с сохранением исходного в измененных границах')])


def test_parent_area_is_scoped_source_statement_not_formed_area_or_legal_status():
    evidence=context()
    assert evidence['origin_statements'][0]['stated_area_m2']==8477
    assert evidence['origin_statements'][0]['page']==7
    assert evidence['division_statements'][0]['page']==19
    r=scheme_review.build([table(':ЗУ1',[(0,0),(20,0),(20,20),(0,20)])],evidence)
    assert r['area_comparisons'][0]['difference_m2']==-8077
    assert r['quarter_disagreement'] and not r['geometry_confirmed'] and not r['georeferenced']
    assert r['comparison_scope']=='all_accepted_tables_not_verified_as_parent'


def test_common_edges_not_overlap_and_nonadjacent_parts_preserved():
    ts=[table(':ЗУ1',[(0,0),(10,0),(10,10),(0,10)]),
        table('retained',[(10,0),(20,0),(20,10),(10,10)],'public_use_context'),
        table(':ЗУ2',[(100,100),(110,100),(110,110),(100,110)])]
    r=scheme_review.build(ts,scheme_review.context([]))
    assert r['union_area_m2']==r['sum_area_m2']==300
    assert r['overlap_area_m2']==r['overlap_pair_count']==0
    assert r['component_count']==len(r['union_rings_xy'])==2
    assert [r['table_indices'] for r in r['union_rings_xy']]==[[0,1],[2]]
    assert len(r['role_totals'])==2 and not r['area_comparisons']


def test_intersections_are_measured_and_rejected_tables_excluded():
    ts=[table('a',[(0,0),(10,0),(10,10),(0,10)]),
        table('b',[(5,0),(15,0),(15,10),(5,10)]),
        table('bad',[(0,0),(100,0),(100,100),(0,100)],state='rejected')]
    r=scheme_review.build(ts,scheme_review.context([]))
    assert r['union_area_m2']==150 and r['sum_area_m2']==200
    assert r['overlap_area_m2']==50 and r['excluded_count']==1
    assert r['overlap_pairs']==[{'left':'a','right':'b','area_m2':50}]


def test_union_holes_are_not_filled_or_reported_as_land():
    ts=[table('a',[(0,0),(30,0),(30,10),(0,10)]),table('b',[(0,20),(30,20),(30,30),(0,30)]),
        table('c',[(0,10),(10,10),(10,20),(0,20)]),table('d',[(20,10),(30,10),(30,20),(20,20)])]
    r=scheme_review.build(ts,scheme_review.context([]))
    assert r['union_area_m2']==800 and len(r['union_rings_xy'][0]['holes_xy'])==1


def test_conflicting_parent_statements_not_reconciled_with_one_union():
    e=context();e['origin_statements'].append({**e['origin_statements'][0],'stated_area_m2':9000})
    r=scheme_review.build([table('a',[(0,0),(10,0),(10,10),(0,10)])],e)
    assert r['origin_ambiguous'] and not r['area_comparisons']
    r=scheme_review.build([],e)
    assert r['union_area_m2'] is None and not r['area_comparisons']


def test_page_limit_rejection_removes_provisional_polygon_from_joint_review():
    r=schemes.extract([(40,'Обозначение земельного участка :ЗУ1\nКоординаты, м\nX Y\n1 2 3\n'
                           'н1 100 100\nн2 120 100\nн3 120 120\nн4 100 120\n')])
    assert r['review']['union_area_m2']==400
    schemes.apply_page_limit(r,40,2)
    assert r['review']['accepted_count']==0 and r['review']['union_area_m2'] is None


def test_previous_extraction_algorithm_is_queued_for_local_upgrade():
    catalog={'items':[{'id':'d','state':'read','format':'pdf','kind':'planning','sha256':'sha'}]}
    old={'documents':[{'document_id':'d','source_sha256':'sha','algorithm':'designation-xy-v1','state':'extracted'}]}
    assert schemes.pending(catalog,old)==catalog['items']


def test_unrecognized_area_unit_not_truncated_to_square_metres():
    text='в границах земельного участка с кадастровым номером 90:12:171301:1630 площадью 8477 кв.мм'
    assert scheme_review.context([(1,text)])['origin_statements']==[]
