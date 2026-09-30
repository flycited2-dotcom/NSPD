from land import publications, store
import pytest


def test_article_links_ignore_nav_scripts_external_and_unsafe():
    raw='''<nav><a href="/nav">Извещение о земле</a></nav><article>
    <a href="/notice"> Извещение о предоставлении земли </a>
    <a href="/notice">Извещение о предоставлении земли</a>
    <a href="javascript:alert(1)">Земля</a><a href="https://evil.invalid/a">Земля</a>
    <script><a href="/script">Извещение</a></script>
    <a href="/tokens?token=abc">Земля</a></article>'''.encode()
    rows=publications.parse_links(raw,'https://trudovskoe-rk.ru/news/')
    assert rows==[{'title':'Извещение о предоставлении земли','url':'https://trudovskoe-rk.ru/notice'}]
    with pytest.raises(ValueError,match='раздел'):
        publications.parse_links(b'<html>challenge</html>','https://trudovskoe-rk.ru/')


def test_source_failure_stays_error_preserves_previous_date(tmp_path,monkeypatch):
    monkeypatch.setattr(store,'DATA',tmp_path);store.init()
    store.set_setting('publications_trudovoe',{'sources':[{'id':'planning','last_success_at':'old','links':[{'title':'old','url':'https://trudovskoe-rk.ru/old'}]}]})
    def fetch(url):
        if url==publications.SOURCES[0]['url']:raise ValueError('HTTP 403')
        if url.endswith('.pdf'):return b'<!doctype html>blocked','text/html',200
        return '<article><a href="/notice">Извещение о земле</a></article>'.encode(),'text/html',200
    monkeypatch.setattr(publications,'fetch',fetch)
    assert publications.run('trudovoe')=={'received':1,'total':3}
    r=store.get_setting('publications_trudovoe')
    assert r['complete'] is False and not r['candidate_matches_confirmed']
    assert r['sources'][0]['state']=='error' and r['sources'][0]['last_success_at']=='old'
    assert not r['sources'][0]['links'] and r['sources'][0]['previous_links']
    assert r['sources'][2]['state']=='error'
    assert len(list((tmp_path/'publications').iterdir()))==1
