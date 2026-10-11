"""Exact Microsoft table formatting must not alter literal cell text or scope."""
import hashlib

import pytest

from land import legacy_text as reader
from land.torgi_file_worker import extract


def rtf(body):
    return ('{\\rtf1\\ansi\\ansicpg1251 ' + body + '}').encode('cp1251')


def table_row(cells, units=3):
    definitions = ''.join('\\clftsWidth' + str(units) + '\\clwWidth' + str(1200 * (index + 1))
                          + '\\cellx' + str(2400 * (index + 1)) for index in range(len(cells)))
    return ('\\trowd\\trftsWidth' + str(units) + definitions + '\\intbl '
            + ''.join(cell + '\\cell ' for cell in cells) + '\\row ')


@pytest.mark.parametrize('units', [0, 1, 2, 3])
def test_width_formatting_preserves_cell_order_empty_cells_and_literal_numbers(units):
    raw = rtf(table_row(['90:12:172101:420', '1000,12', '4606870.25'], units)
              + table_row(['90:12:172101:421', '', '500001.00'], units))
    text, info = reader.rtf_text(raw)
    assert text == '90:12:172101:420|1000,12|4606870.25|\n90:12:172101:421||500001.00|\n'
    assert info['encoding'] == 'cp1251' and info['body_text_characters'] == len(text)
    evidence, units_read = extract(raw, 'rtf')
    assert [mention['cadastral_number'] for mention in evidence['mentions']] == ['90:12:172101:420', '90:12:172101:421']
    assert units_read == [(1, text)] and evidence['unit'] == 'rtf_body' and evidence['tables'] == []
    assert not evidence['geometry_confirmed'] and not evidence['georeferenced'] and not evidence['text_layer_complete']


def test_table_widths_do_not_change_unicode_fallback_cyrillic_or_visible_literals():
    body = table_row(['Площадь \\u1046?', '12\\tab 34', r'Literal \\ \{42\}'])
    raw = rtf(body)
    original = hashlib.sha256(raw).hexdigest()
    text, _ = reader.rtf_text(raw)
    assert text == 'Площадь Ж|12\t34|Literal \\ {42}|\n'
    assert hashlib.sha256(raw).hexdigest() == original


def test_new_controls_never_expose_hidden_destination_or_field_instructions():
    body = (r'{\header\clftsWidth3\clwWidth2400 90:12:172101:888}'
            r'{\*\unknown\trftsWidth3 90:12:172101:999}'
            r'{\pict\clftsWidth3 90:12:172101:777}'
            r'{\field{\*\fldinst HYPERLINK "90:12:172101:666"}'
            r'{\fldrslt Link}}\par ' + table_row(['90:12:172101:420', '400']))
    evidence, units = extract(rtf(body), 'rtf')
    assert [mention['cadastral_number'] for mention in evidence['mentions']] == ['90:12:172101:420']
    assert units[0][1] == 'Link\n90:12:172101:420|400|\n'


@pytest.mark.parametrize('control', ['ClftsWidth', 'clftsWIDTH', 'CLWWIDTH', 'clwwidthX',
                                     'TrftsWidth', 'trftsWidthA', 'WidthUnknown', 'Bin', 'Pict', 'MmathPr'])
def test_only_the_three_verified_exact_names_expand_supported_mixed_case_controls(control):
    with pytest.raises(ValueError, match='Регистр'):
        reader.rtf_text(rtf('\\' + control + '3 90:12:172101:999'))


@pytest.mark.parametrize('body', [r'\clftsWidth3\bin4 data', r"\clwWidth2400\'zz",
                                  r'\trftsWidth3\uc99 text', r'\clftsWidth3\u999999?',
                                  r'\clwWidth2400\u-10240?', r'\trftsWidth3\ansicpg999999 text',
                                  '{' * 129 + r'\clftsWidth3 text' + '}' * 129])
def test_existing_binary_unicode_encoding_hex_and_depth_guards_remain(body):
    with pytest.raises((ValueError, LookupError, UnicodeError)):
        reader.rtf_text(rtf(body))


def test_incomplete_groups_trailing_payload_byte_and_text_caps_are_unchanged():
    for raw in (rtf(table_row(['1', '2']))[:-1], rtf(table_row(['1', '2'])) + b'payload',
                b'{\\rtf1 ' + b'x' * reader.MAX_BYTES + b'}'):
        with pytest.raises(ValueError):
            reader.rtf_text(raw)
    with pytest.raises(ValueError, match='Текст'):
        reader.rtf_text(rtf('\\clftsWidth3 ' + 'x' * (reader.MAX_TEXT + 1)))


def test_new_algorithm_and_verified_set_are_explicit():
    assert reader.ALGORITHM == 'torgi-legacy-text-v2'
    assert reader.RTF_TABLE_WIDTH_FORMATTING == {'clftsWidth', 'clwWidth', 'trftsWidth'}
