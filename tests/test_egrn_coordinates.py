import pytest
from land.egrn_coordinates import extract

META='\nЛист № {sheet} раздела 3.2 Всего листов раздела 3.2: {total}\nКадастровый номер: {cad}\n'
TITLE='Сведения о характерных точках границы земельного участка\nСистема координат СК-63, зона 4\n'
HEADER='Номер точки Координаты, м Описание закрепления Средняя квадратичная погрешность, мX Y\n1 2 3 4 5\n'
ROWS='1 5000000.00 4300000.00 закрепление отсутствует 0.1\n2 5000020.00 4300000.00 закрепление отсутствует 0.1\n3 5000020.00 4300020.00 закрепление отсутствует 0.1\n'
END='4 5000000.00 4300020.00 закрепление отсутствует 0.1\n5 5000000.00 4300000.00 закрепление отсутствует 0.1\n'


def pages():
    return [(8,TITLE+HEADER+ROWS+META.format(sheet=1,total=2,cad='90:11:110501:2843')),
            (9,END+META.format(sheet=2,total=2,cad='90:11:110501:2843'))]


def test_two_page_egrn_preserves_axes_crs_sources_and_explicit_closure():
    result,consumed=extract(pages());t=result[0]
    assert t['state']=='review_required' and t['pages']==[8,9]
    assert t['cadastral_number']=='90:11:110501:2843'
    assert t['crs_label']=='СК-63, зона 4' and t['crs_status']=='parameters_missing'
    assert t['points'][0]['x']==5000000 and t['points'][0]['y']==4300000
    assert t['local_area_m2']==400 and len(t['points'])==5 and len(consumed)==5
    assert t['closure']=='explicit_coordinate_repeat' and not t['geometry_confirmed'] and not t['georeferenced']


@pytest.mark.parametrize('change',[
    lambda p:p[:1],
    lambda p:[p[0],(10,p[1][1])],
    lambda p:[p[0],(9,p[1][1].replace('90:11:110501:2843','90:11:110501:999'))],
    lambda p:[p[0],(9,p[1][1].replace('Лист № 2','Лист № 3'))],
    lambda p:[p[0],(9,p[1][1].replace('4300000.00','4300001.00'))],
    lambda p:[p[0],(9,p[1][1].replace('4 5000000.00','7 5000000.00'))],
    lambda p:[(8,p[0][1].replace('X Y','Y X')),p[1]],
    lambda p:[(8,p[0][1].replace('Координаты, м ','Координаты, см ')),p[1]],
    lambda p:[(8,p[0][1].replace('4300000.00 закрепление','43О0000.00 закрепление')),p[1]],
    lambda p:[(8,p[0][1].replace('закрепление отсутствует','неизвестное описание')),p[1]],
])
def test_incomplete_ambiguous_unsupported_and_broken_rows_rejected(change):
    t=extract(change(pages()))[0][0]
    assert t['state']=='rejected' and t['outline_xy'] is None and t['local_area_m2'] is None


def test_no_designation_from_neighbour_number_and_no_auto_combination():
    for title in ('Номер соседа:','Соседний кадастровый номер:'):
        p=pages();p[0]=(8,p[0][1].replace('Кадастровый номер:',title))
        assert extract(p)[0][0]['state']=='rejected'
    duplicate=pages()+[(n+2,text) for n,text in pages()]
    assert all(t['state']=='rejected' for t in extract(duplicate)[0])


def test_self_intersection_and_inner_coordinate_repeat_rejected():
    p=pages();p[0]=(8,p[0][1].replace('2 5000020.00 4300000.00','2 5000020.00 4300020.00')
                 .replace('3 5000020.00 4300020.00','3 5000020.00 4300000.00'))
    assert extract(p)[0][0]['outline_xy'] is None
    p=pages();p[1]=(9,p[1][1].replace('4 5000000.00 4300020.00','4 5000020.00 4300020.00'))
    assert extract(p)[0][0]['outline_xy'] is None


def test_pdf_worker_reports_egrn_and_leaves_other_coordinate_pages_visible(monkeypatch):
    from land import torgi_file_worker,municipal
    p=pages()+[(10,'Другой формат координат\nн1 4990000.0 4330000.0\n')]
    details=dict(municipal.evidence(p),processed_pages=10,total_pages=10,unread_pages=0)
    monkeypatch.setattr(municipal,'pdf_text',lambda raw,**kwargs:(details,p))
    result,_=torgi_file_worker.extract(b'local','pdf')
    assert len(result['egrn_tables'])==1 and result['egrn_tables'][0]['state']=='review_required'
    assert result['unparsed_coordinate_pages']==[10] and not result['georeferenced']


@pytest.mark.parametrize('mark',['-', 'закрепление отсутствует'])
@pytest.mark.parametrize('closing_label',['1','5'])
def test_printed_mark_and_two_explicit_closure_numberings(mark, closing_label):
    p=[(n,text.replace('закрепление отсутствует',mark)
        .replace('5 5000000.00 4300000.00',closing_label+' 5000000.00 4300000.00')) for n,text in pages()]
    t=extract(p)[0][0]
    assert t['state']=='review_required' and t['local_area_m2']==400
    assert t['points'][-1]['label']==closing_label
    assert all(point['mark_description']==mark for point in t['points'])
    assert not t['georeferenced'] and not t['geometry_confirmed']


@pytest.mark.parametrize('last_rows',[
    END.replace('5 5000000.00 4300000.00','1 5000000.00 4300001.00'),
    END.replace('4 5000000.00 4300020.00','1 5000000.00 4300020.00'),
    END+'6 5000010.00 4300010.00 закрепление отсутствует 0.1\n',
    END.replace('5 5000000.00 4300000.00','1 5000000.00 4300000.00')+'2 5000020.00 4300000.00 закрепление отсутствует 0.1\n',
])
def test_restarted_numbering_or_points_after_closure_rejected(last_rows):
    p=pages();p[1]=(9,last_rows+META.format(sheet=2,total=2,cad='90:11:110501:2843'))
    t=extract(p)[0][0]
    assert t['state']=='rejected' and t['outline_xy'] is None


def test_observed_single_sheet_dash_table_retains_precision_and_first_label():
    rows='''1 4991177.93 5255458.54 - 2.5
2 4991167.61 5255757.96 - 2.5
3 4990755.44 5255756.88 - 2.5
4 4990761.48 5255419.48 - 2.5
5 4991152.98 5255431.86 - 2.5
1 4991177.93 5255458.54 - 2.5
'''
    t=extract([(8,TITLE.replace('зона 4','зона 5')+HEADER+rows+
                  META.format(sheet=1,total=1,cad='90:13:050601:2239'))])[0][0]
    assert t['state']=='review_required' and len(t['points'])==6
    assert t['points'][0]['source_row']==rows.splitlines()[0]
    assert t['cadastral_number']=='90:13:50601:2239' and t['crs_label']=='СК-63, зона 5'
    assert t['outline_xy'][0]==[4991177.93,5255458.54] and t['outline_xy'][-1]==t['outline_xy'][0]
    assert t['crs_status']=='parameters_missing' and not t['georeferenced']


@pytest.mark.parametrize('separator',['Продолжение таблицы\n', ''])
@pytest.mark.parametrize('label',['4', 'н4'])
def test_coordinates_after_separator_or_unsupported_label_cannot_hide_second_part(separator,label):
    rows=ROWS+'1 5000000.00 4300000.00 - 0.1\n'+separator+label+' 5000000.00 4300020.00 - 0.1\n1 5000000.00 4300000.00 - 0.1\n'
    t=extract([(8,TITLE+HEADER+rows+META.format(sheet=1,total=1,cad='90:11:110501:2843'))])[0][0]
    assert t['state']=='rejected' and t['outline_xy'] is None and t['issues']
