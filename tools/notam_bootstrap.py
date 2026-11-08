"""Initial (and occasional) full load: reads the NMS-API Initial Load file (AIXM, gzipped)
and writes SQL that fills loc_notams. Usage: bootstrap.py il.gz out.sql"""
import gzip, json, re, sys, datetime, xml.etree.ElementTree as ET
from collections import defaultdict

NS = {
    'event': 'http://www.aixm.aero/schema/5.1/event',
    'fnse': 'http://www.aixm.aero/schema/5.1/extensions/FAA/FNSE',
    'gml': 'http://www.opengis.net/gml/3.2',
    'msg': 'http://www.aixm.aero/schema/5.1/message',
}
GML_ID = '{http://www.opengis.net/gml/3.2}id'

def iso(stamp):
    """202611071100 -> 2026-11-07T11:00:00Z; PERM and others pass through."""
    if stamp and re.fullmatch(r'\d{12}', stamp):
        return f'{stamp[0:4]}-{stamp[4:6]}-{stamp[6:8]}T{stamp[8:10]}:{stamp[10:12]}:00Z'
    if stamp and re.fullmatch(r'\d{12}EST', stamp):
        return iso(stamp[:12]) + 'EST'
    return stamp

def text(el, path):
    found = el.find(path, NS)
    return found.text.strip() if found is not None and found.text else None

def normalize(member):
    message = member.find('msg:AIXMBasicMessage', NS)
    notam = member.find('.//event:NOTAM', NS)
    if message is None or notam is None:
        return None
    ext = member.find('.//fnse:EventExtension', NS)
    series, number, year = text(notam, 'event:series'), text(notam, 'event:number'), text(notam, 'event:year')
    translations = []
    for tr in notam.findall('event:translation/event:NOTAMTranslation', NS):
        body = text(tr, 'event:simpleText')
        if body is None:
            div = tr.find('event:formattedText', NS)
            body = ''.join(div.itertext()).strip() if div is not None else None
        if body:
            translations.append({'type': text(tr, 'event:type'), 'text': body})
    # Notam numbers the way the API's GeoJSON writes them: A0753/26, or 11/172 domestic.
    shown = number
    if series and number and year and number.isdigit():
        shown = f'{series}{int(number):04d}/{year[2:]}'
    elif translations and translations[0]['text'].startswith('!'):
        m = re.match(r'!\S+\s+(\d+/\d+)', translations[0]['text'])
        if m: shown = m.group(1)
    n = {
        'id': message.get(GML_ID),
        'number': shown,
        'series': series,
        'year': year,
        'type': text(notam, 'event:type'),
        'issued': text(notam, 'event:issued'),
        'location': text(notam, 'event:location'),
        'icaoLocation': text(ext, 'fnse:icaoLocation') if ext is not None else None,
        'effectiveStart': iso(text(notam, 'event:effectiveStart')),
        'effectiveEnd': iso(text(notam, 'event:effectiveEnd')),
        'text': text(notam, 'event:text'),
        'classification': text(ext, 'fnse:classification') if ext is not None else None,
        'accountId': text(ext, 'fnse:accountId') if ext is not None else None,
        'lastUpdated': text(ext, 'fnse:lastUpdated') if ext is not None else None,
        'selectionCode': text(notam, 'event:selectionCode'),
        'schedule': text(notam, 'event:schedule'),
        'coordinates': text(notam, 'event:coordinates'),
        'radius': text(notam, 'event:radius'),
        'translations': translations,
    }
    return {k: v for k, v in n.items() if v not in (None, '', [])}

LIMIT = 90_000  # D1 rejects SQL statements over 100 KB

def chunks(items):
    """Splits a location's NOTAMs into JSON arrays that each stay under the limit."""
    out, current, size = [], [], 2
    for item in items:
        piece = json.dumps(item, separators=(',', ':'), ensure_ascii=False)
        if len(piece) > LIMIT - 10:
            piece = json.dumps({**item, 'translations': [], 'text': item.get('text', '')[:LIMIT // 2]}, separators=(',', ':'), ensure_ascii=False)
        if current and size + len(piece) + 1 > LIMIT:
            out.append('[' + ','.join(current) + ']'); current, size = [], 2
        current.append(piece); size += len(piece) + 1
    if current: out.append('[' + ','.join(current) + ']')
    return out

def main(src, out):
    by_loc = defaultdict(list)
    stamp = None
    count = 0
    with gzip.open(src, 'rb') as f:
        for event, el in ET.iterparse(f, events=('start', 'end')):
            if event == 'start':
                if stamp is None and el.tag.endswith('FeatureCollection'):
                    stamp = el.get('timeStamp')
                continue
            if el.tag == '{http://www.opengis.net/wfs/2.0}member':
                n = normalize(el)
                el.clear()
                if n and n.get('type') != 'C' and n.get('location'):
                    by_loc[n['location']].append(n)
                    count += 1
    now = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    def q(s): return "'" + s.replace("'", "''") + "'"
    biggest = 0
    with open(out, 'w') as o:
        o.write('DELETE FROM loc_notams;\n')
        for loc, items in by_loc.items():
            for part, blob in enumerate(chunks(items)):
                biggest = max(biggest, len(blob))
                o.write(f'INSERT INTO loc_notams (loc, part, data, updated) VALUES ({q(loc)}, {part}, {q(blob)}, {q(now)});\n')
        o.write(f"INSERT OR REPLACE INTO meta (k, v) VALUES ('last_sync', {q(stamp or now)});\n")
        o.write(f"INSERT OR REPLACE INTO meta (k, v) VALUES ('last_full_load', {q(now)});\n")
    print(json.dumps({'notams': count, 'locations': len(by_loc), 'largestRowBytes': biggest, 'fileTimeStamp': stamp}))

if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
