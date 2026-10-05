import pytest
from land import scheme_catalogs as catalogs, georeference, scheme_review, schemes


def page(number, rows):
    """Synthetic PDF line positions, no actual documents or personal data."""
    raw='';lines=[]
    for text,x,y in rows:
        body=text+'\r\n'
        lines.append({'page':number,'offset':len(raw),'end_offset':len(raw)+len(body),'text':text,
                      'pdf_bbox':[x,y,x+100,y+8]})
        raw+=body
    return {'page':number,'width':600,'height':840,'raw':raw,'lines':lines}


def title(section):
    return section+' '+catalogs.PROFILES[section][0]


def rows(x=60,top=700,label=':ЗУ0',closed=True):
    values=[label,'1 4978000.00 5190000.00','2 4978010.00 5190000.00',
            '3 4978010.00 5190010.00','4 4978000.00 5190010.00']
    if closed:values.append('1 4978000.00 5190000.00')
    return [(v,x,top-i*12) for i,v in enumerate(values)]


def test_page_footer_does_not_supply_the_section_heading_box():
    p=page(37,[('25',570,20),(title('2.2'),60,790),('п/п X, м Y, м',60,750),*rows()])
    result=catalogs.extract_layout([p])
    c=result['coordinate_catalogs'][0]
    assert c['heading_source']['text']==title('2.2') and c['heading_source']['pdf_bbox'][1]==790
    assert c['axes_status']=='explicit_xy_m' and c['phase']==1 and c['column_count']==1
    r=c['parts'][0]['rings'][0]
    assert r['state']=='review_required' and r['local_area_m2']==100
    assert r['points'][0]['column']==1 and r['points'][0]['page']==37
    assert r['points'][0]['source_row']=='1 4978000.00 5190000.00' and r['points'][0]['pdf_bbox']
    assert not c['geometry_confirmed'] and not c['completeness_confirmed']
    assert result['catalogue_covered_coordinate_pages']==[37]


def test_column_continuation_precedes_neighbouring_column_labels():
    r=rows();left=r[:3]
    right=rows(x=350,label=':ЗУ2')
    p1=page(40,[(title('2.3'),60,790),*left,*right])
    p2=page(41,[(text,x,780-i*12) for i,(text,x,_) in enumerate(r[3:])])
    c=catalogs.extract_layout([p1,p2])['coordinate_catalogs'][0]
    assert [p['label'] for p in c['parts']]==[':ЗУ0',':ЗУ2']
    assert all(p['state']=='review_required' for p in c['parts'])
    assert [p['page'] for p in c['parts'][0]['rings'][0]['points']]==[40,40,41,41,41]
    assert c['axes_status']=='unlabelled_columns' and all(p['rings'][0]['local_area_m2'] is None for p in c['parts'])


def test_two_sections_on_one_page_are_cut_by_their_physical_headings():
    p=page(46,[(title('2.6'),60,790),*rows(top=750,label=':ЗУ20'),
               (title('2.7'),60,600),*rows(top=560,label=':ЗУ20:ЗУ1')])
    c=catalogs.extract_layout([p])['coordinate_catalogs']
    assert [x['phase'] for x in c]==[3,4]
    assert [x['parts'][0]['label'] for x in c]==[':ЗУ20',':ЗУ20:ЗУ1']
    assert all(len(x['parts'])==1 for x in c)
    assert not georeference.choices({'documents':[{'state':'extracted','title':'x','coordinate_catalogs':c,'tables':[]}]})
    assert scheme_review.build([],{'origin_statements':[],'division_statements':[],'quarter_statements':[]})['accepted_count']==0


@pytest.mark.parametrize('header',['Y, м X, м','X, см Y, см',''])
def test_unknown_axes_and_units_are_not_inferred_from_other_catalogues(header):
    p=page(40,[(title('2.3'),60,790),(header,60,750),*rows()])
    c=catalogs.extract_layout([p])['coordinate_catalogs'][0]
    assert c['axes_status']=='unlabelled_columns' and c['parts'][0]['rings'][0]['local_area_m2'] is None


def test_thousands_spaces_and_source_area_are_preserved():
    r=[(t.replace('4978000.00','4 978 000,00').replace('5190000.00','5 190 000,00'),x,y) for t,x,y in rows()]
    p=page(37,[(title('2.2'),60,790),('X, м Y, м',60,750),*r,('Площадь: 1 000 кв.м.',60,580)])
    part=catalogs.extract_layout([p])['coordinate_catalogs'][0]['parts'][0]
    assert part['rings'][0]['points'][0]['column_1']==4978000
    assert part['source_areas'][0]['stated_area_m2']==1000
    assert '4 978 000,00' in part['rings'][0]['points'][0]['source_row']


def test_open_sequence_number_gap_and_crossing_rings_have_no_outline():
    for selected in (rows(closed=False),[(t.replace('4 4978000','6 4978000'),x,y) for t,x,y in rows()],
                     [(t.replace('2 4978010.00 5190000.00','2 4978010.00 5190010.00').replace('3 4978010.00 5190010.00','3 4978010.00 5190000.00'),x,y) for t,x,y in rows()]):
        part=catalogs.extract_layout([page(40,[(title('2.3'),60,790),*selected])])['coordinate_catalogs'][0]['parts'][0]
        assert part['state']=='rejected' and part['rings'][0]['outline_columns'] is None


def test_extra_ring_has_unknown_role_and_common_area_belongs_to_catalogue():
    data=[(title('2.4'),60,790),('X, м Y, м',60,765),(':ЗУ0',60,740),('Контур 1',60,720),*rows(top=700)[1:]]
    extra=rows(top=600,label='unused')[1:]
    extra=[(t.replace('1 ','371 ',1).replace('2 ','372 ',1).replace('3 ','373 ',1).replace('4 ','374 ',1),x,y) for t,x,y in extra]
    data+=extra+[('Площадь: 6349 кв.м.',60,490)]
    c=catalogs.extract_layout([page(42,data)])['coordinate_catalogs'][0]
    p=c['parts'][0]
    assert len(p['rings'])==2 and p['state']=='rejected' and p['issues']
    assert all(r['outline_columns'] is None for r in p['rings'])
    assert p['source_areas']==[] and c['source_areas'][0]['scope']=='all_catalogue_contours'


def test_duplicate_labels_in_one_section_are_rejected():
    c=catalogs.extract_layout([page(40,[(title('2.3'),60,790),*rows(),*rows(top=580)])])['coordinate_catalogs'][0]
    assert all(p['state']=='rejected' for p in c['parts']) and len(c['parts'])==2


def test_scan_or_missing_page_does_not_join_vertices():
    r=rows();p1=page(40,[(title('2.3'),60,790),*r[:3]])
    p3=page(42,[(t,x,780-i*12) for i,(t,x,_) in enumerate(r[3:])])
    part=catalogs.extract_layout([p1,page(41,[]),p3])['coordinate_catalogs'][0]['parts'][0]
    assert part['state']=='rejected' and any('пропущена страница' in x['reason'] for x in part['rings'][0]['issues'])


def test_malformed_and_orphan_rows_remain_visible_and_not_covered():
    for body in ([('1 4978000.00 5190000.00',60,700)],[(t.replace('3 4978010.00','3 49780О0.00'),x,y) for t,x,y in rows()]):
        c=catalogs.extract_layout([page(40,[(title('2.3'),60,790),*body])])['coordinate_catalogs'][0]
        assert c['issues']
        if c['parts']:assert c['parts'][0]['state']=='rejected'


def test_layout_with_four_columns_is_rejected():
    data=[(title('2.3'),20,790)]
    for index in range(4):data+=rows(x=20+index*140,label=f':ЗУ{index}')
    with pytest.raises(ValueError,match='трёх столбцов'):catalogs.extract_layout([page(40,data)])


def test_unknown_line_inside_ring_is_not_silently_discarded():
    data=rows();data.insert(3,('непрочитанная строка',60,674))
    c=catalogs.extract_layout([page(40,[(title('2.3'),60,790),*data])])['coordinate_catalogs'][0]
    p=c['parts'][0]
    assert p['state']=='rejected' and p['rings'][0]['outline_columns'] is None
    assert any('Неподдержанная строка' in x['reason'] for x in p['issues'])


def test_catalogue_error_can_be_retried_explicitly_without_a_loop():
    row={'id':'d','state':'read','format':'pdf','kind':'planning','sha256':'a'*64}
    previous={'documents':[{'document_id':'d','state':'extracted','source_sha256':row['sha256'],
                            'algorithm':schemes.ALGORITHM,'coordinate_catalog_error':'layout error'}]}
    assert schemes.pending({'items':[row]},previous)==[]
    assert schemes.pending({'items':[row]},previous,retry=True)==[row]
