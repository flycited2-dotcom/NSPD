"""Bounded, read-only text evidence from Word binary pieces and RTF groups."""
import codecs
import io
import re
import struct
import olefile
from striprtf.striprtf import PATTERN, destinations, rtf_to_text

ALGORITHM = 'torgi-legacy-text-v1'
MAX_BYTES = 8 * 1024 * 1024
MAX_TEXT = 500000
OLE_MAGIC = bytes.fromhex('d0cf11e0a1b11ae1')
# Word uses these case-sensitive table/math formatting controls. Destinations
# such as mmathPr are skipped by striprtf; do not lowercase arbitrary controls.
RTF_MIXED_FORMATTING = frozenset(('trftsWidthB','mmathFont','mbrkBin','mbrkBinSub',
    'msmallFrac','mdispDef','mlMargin','mrMargin','mdefJc','mwrapIndent','mintLim','mnaryLim'))
# MS-DOC 2.9.73: eight-bit Unicode with the following explicit exceptions.
# https://learn.microsoft.com/en-us/openspecs/office_file_formats/ms-doc/aa2e55a2-f4f2-4795-bab5-6d9d7a0ed249
COMPRESSED_MAP = dict(zip(
    [0x82,0x83,0x84,0x85,0x86,0x87,0x88,0x89,0x8a,0x8b,0x8c,0x91,0x92,0x93,0x94,0x95,0x96,0x97,0x98,0x99,0x9a,0x9b,0x9c,0x9f],
    [0x201a,0x192,0x201e,0x2026,0x2020,0x2021,0x2c6,0x2030,0x160,0x2039,0x152,0x2018,0x2019,0x201c,0x201d,0x2022,0x2013,0x2014,0x2dc,0x2122,0x161,0x203a,0x153,0x178]))


def integer(raw, offset, fmt='<H'):
    if offset < 0 or offset + struct.calcsize(fmt) > len(raw):
        raise ValueError('Неполная структура DOC')
    return struct.unpack_from(fmt, raw, offset)[0]


def visible_field_text(text):
    frames, out, skipped = [], [], 0
    for char in text:
        if char == '\x13':
            frames.append(False)
            if len(frames) > 64:raise ValueError('Слишком глубокие поля DOC')
        elif char == '\x14':
            if not frames or frames[-1]:raise ValueError('Некорректное поле DOC')
            frames[-1] = True
        elif char == '\x15':
            if not frames:raise ValueError('Некорректное поле DOC')
            frames.pop()
        elif all(frames):
            out.append(char)
        else:
            skipped += 1
    if frames:raise ValueError('Незавершённое поле DOC')
    return ''.join(out), skipped


def doc_stream_text(word, table):
    """MS-DOC FIB / Clx / PlcPcd: only CP [0, ccpText) from the main story."""
    if len(word) < 34 or integer(word, 0) != 0xa5ec:
        raise ValueError('Не найден заголовок Word Binary DOC')
    version, flags = integer(word,2), integer(word,10)
    if flags & 0x100:raise ValueError('Зашифрованный DOC не читается')
    csw = integer(word,32)
    if not 14 <= csw <= 256:raise ValueError('Неподдержанная структура FIB DOC')
    offset = 34 + csw * 2
    cslw = integer(word,offset); lw = offset + 2
    if not 22 <= cslw <= 256:raise ValueError('Неподдержанная структура FIB DOC')
    cbmac, body = integer(word,lw,'<I'), integer(word,lw+12,'<i')
    stories = [integer(word,lw+i*4,'<i') for i in (4,5,7,8,9,10)]
    if not 0 <= body <= MAX_TEXT or any(n < 0 or n > MAX_TEXT for n in stories):
        raise ValueError('Объём текста DOC превышает лимит')
    offset = lw + cslw * 4
    count = integer(word,offset); pairs = offset + 2
    if not 34 <= count <= 512:raise ValueError('Неподдержанная таблица FIB DOC')
    end = pairs + count * 8
    new_count = integer(word,end)
    if new_count > 64:raise ValueError('Неподдержанное продолжение FIB DOC')
    fib_end = end + 2 + new_count * 2
    if fib_end > len(word):raise ValueError('Неполный FIB DOC')
    if new_count:version = integer(word,end+2)
    if version not in (0xc1,0xd9,0x101,0x10c,0x112):
        raise ValueError('Версия бинарного DOC не поддержана')
    if not fib_end <= cbmac <= len(word):raise ValueError('Некорректный размер WordDocument')
    fc, size = struct.unpack_from('<II',word,pairs+33*8)
    if not 5 <= size <= 1024*1024 or fc + size > len(table):
        raise ValueError('Таблица частей текста DOC отсутствует/повреждена')
    clx = table[fc:fc+size]; pos = 0
    while pos < len(clx) and clx[pos] == 1:
        grp_size = integer(clx,pos+1)
        if grp_size > 0x3fa2:raise ValueError('Некорректная группа свойств DOC')
        pos += 3 + grp_size
    if pos >= len(clx) or clx[pos] != 2:raise ValueError('Не найден Pcdt DOC')
    size = integer(clx,pos+1,'<I'); pos += 5
    if size < 4 or (size-4) % 12 or pos+size != len(clx):
        raise ValueError('Некорректный размер PlcPcd DOC')
    n = (size-4)//12
    if not 1 <= n <= 10000:raise ValueError('Число частей DOC превышает лимит')
    cps = [integer(clx,pos+i*4,'<I') for i in range(n+1)]
    expected = body + sum(stories) + (1 if any(stories) else 0)
    if cps[0] != 0 or cps[-1] != expected or any(a >= b for a,b in zip(cps,cps[1:])):
        raise ValueError('Диапазоны символов DOC неполны/неупорядочены')
    descriptors = pos + (n+1)*4; chunks = []
    compressed_parts = 0
    for i in range(n):
        packed = integer(clx,descriptors+i*8+2,'<I')
        if packed & 0x80000000:raise ValueError('Некорректное смещение части DOC')
        compressed = bool(packed & 0x40000000); start = packed & 0x3fffffff
        if compressed and start % 2:raise ValueError('Нечётное сжатое смещение DOC')
        start = start//2 if compressed else start
        length = cps[i+1]-cps[i]; byte_count = length if compressed else length*2
        if start < fib_end or start+byte_count > cbmac:
            raise ValueError('Текст части DOC за пределами WordDocument')
        # Check every piece, but never read footnotes/headers/comments/textboxes into body evidence.
        wanted = min(length,max(0,body-cps[i]))
        if wanted:
            raw = word[start:start+wanted*(1 if compressed else 2)]
            if compressed:
                compressed_parts += 1
                raw = ''.join(chr(COMPRESSED_MAP.get(b,b)) for b in raw).encode('utf-16-le')
            chunks.append(raw)
    text = b''.join(chunks).decode('utf-16-le',errors='strict')
    text, skipped = visible_field_text(text)
    translation = {7:'\t',11:'\n',12:'\n',13:'\n',0x1e:'-',0x1f:'\u00ad'}
    text = ''.join(translation.get(ord(c),c if ord(c)>=32 or c in '\t\n' else ' ') for c in text)
    return text, {'fib_version':version,'piece_count':n,'compressed_body_pieces':compressed_parts,
                  'body_characters_declared':body,'other_story_characters':sum(stories),
                  'field_instruction_characters_skipped':skipped,'body_text_characters':len(text)}


def doc_text(raw):
    if len(raw) > MAX_BYTES or not raw.startswith(OLE_MAGIC):
        raise ValueError('Ожидался бинарный DOC до 8 МБ')
    with olefile.OleFileIO(io.BytesIO(raw),raise_defects=olefile.DEFECT_INCORRECT) as ole:
        paths = ole.listdir(streams=True,storages=True)
        if len(paths)>500 or any(part.lower() in ('macros','vba','_vba_project') for path in paths for part in path):
            raise ValueError('DOC с макросами или слишком большим числом потоков отклонён')
        if not ole.exists('WordDocument'):raise ValueError('В контейнере нет WordDocument')
        def stream(name):
            if not ole.exists(name) or ole.get_size(name)>MAX_BYTES:
                raise ValueError('Поток DOC отсутствует или превышает лимит')
            value = ole.openstream(name).read()
            if len(value)!=ole.get_size(name):raise ValueError('Неполный поток DOC')
            return value
        word = stream('WordDocument')
        if integer(word,10)&0x100:raise ValueError('Зашифрованный DOC не читается')
        table_name = '1Table' if integer(word,10)&0x200 else '0Table'
        text,details = doc_stream_text(word,stream(table_name))
        return text,{**details,'table_stream':table_name,'container_entries':len(paths)}


def rtf_text(raw):
    if len(raw)>MAX_BYTES or not re.match(br'{\\rtf1(?=[\\{}\s])',raw):
        raise ValueError('Ожидался RTF 1 до 8 МБ')
    ascii_text = raw.decode('latin1'); depth = tokens = 0; codepages = set(); max_depth = 0
    for match in PATTERN.finditer(ascii_text):
        tokens += 1
        if tokens>1000000:raise ValueError('Число элементов RTF превышает лимит')
        word,arg,hex_value,char,brace,text = match.groups()
        if char=="'":raise ValueError('Некорректная шестнадцатеричная запись RTF')
        if word and word!=word.lower() and word not in destinations and word not in RTF_MIXED_FORMATTING:
            raise ValueError('Регистр управляющего слова RTF не поддержан')
        if brace:
            depth += 1 if brace=='{' else -1
            max_depth = max(max_depth,depth)
            if depth<0 or depth>128:raise ValueError('Некорректная глубина RTF')
            if depth==0 and ascii_text[match.end():].strip():raise ValueError('Данные после окончания RTF')
        if word=='bin':raise ValueError('Бинарные вставки RTF требуют отдельного разбора')
        if word=='ansicpg':
            if not arg or not arg.isdigit():raise ValueError('Не задана кодировка RTF')
            codecs.lookup('cp'+arg);codepages.add('cp'+arg)
        if word=='uc' and (arg is None or not 0<=int(arg)<=16):raise ValueError('Некорректный Unicode fallback RTF')
        if word=='u' and (arg is None or not -32768<=int(arg)<=65535):raise ValueError('Некорректный символ Unicode RTF')
    if depth!=0:raise ValueError('Неполный RTF')
    if len(codepages)>1:raise ValueError('Несколько кодировок RTF требуют сверки')
    encoding = next(iter(codepages),'cp1252')
    text = rtf_to_text(raw.decode(encoding,errors='strict'),encoding=encoding,errors='strict')
    # Validate paired Unicode surrogates emitted by RTF \u controls; do not replace damaged characters.
    text = text.encode('utf-16-le',errors='surrogatepass').decode('utf-16-le',errors='strict')
    if len(text)>MAX_TEXT:raise ValueError('Текст RTF превышает лимит')
    return text,{'encoding':encoding,'rtf_tokens':tokens,'max_group_depth':max_depth,'body_text_characters':len(text)}
