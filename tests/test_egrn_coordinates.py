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
    monkeypatch.setattr(municipal,'pdf_text',lambda raw:(details,p))
    result,_=torgi_file_worker.extract(b'local','pdf')
    assert len(result['egrn_tables'])==1 and result['egrn_tables'][0]['state']=='review_required'
    assert result['unparsed_coordinate_pages']==[10] and not result['georeferenced']
