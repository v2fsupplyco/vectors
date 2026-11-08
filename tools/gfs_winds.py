"""Winds aloft for Vectors, from NOAA's GFS model (public domain).

Downloads only the needed fields from NOAA's GFS 0.5-degree files on AWS (byte ranges from the
.idx files), and writes one small binary file per 10x10-degree box per level:

  out/winds/latest.json                        run time, valid times, levels, layout
  out/winds/<run>/<level>/<latBand>_<lonBand>.bin

Each .bin holds 17 times (0-48 h, every 3 h) x 21 x 21 points (0.5 deg, edges included),
row-major from the box's south-west corner. Per point: speed (kt, uint8), direction
(deg true the wind blows FROM, uint8 = deg * 256/360), temperature (deg C, int8); the surface
level adds gust (kt, uint8). latBand = floor((lat + 90) / 10) (0-17), lonBand = floor((lon + 180) / 10) (0-35).
"""
import json, math, os, sys, time, urllib.request, datetime
import numpy as np
import eccodes

BUCKET = 'https://noaa-gfs-bdp-pds.s3.amazonaws.com'
HOURS = list(range(0, 49, 3))
LEVELS = [  # (app level id, GRIB level text)
    ('surface', None), ('fl050', '850 mb'), ('fl100', '700 mb'), ('fl180', '500 mb'),
    ('fl240', '400 mb'), ('fl300', '300 mb'), ('fl340', '250 mb'), ('fl390', '200 mb'),
    ('fl450', '150 mb'), ('fl530', '100 mb'), ('fl600', '70 mb'),
]
UA = {'User-Agent': 'Vectors winds aloft (support@v2fsupply.com)'}
KT = 1.943844

def get(url, headers=None, tries=4):
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={**UA, **(headers or {})})
            return urllib.request.urlopen(req, timeout=60).read()
        except Exception as e:
            if attempt == tries - 1: raise
            time.sleep(2 * (attempt + 1))

def latest_run():
    """Newest run whose 48-hour file is published (so every time step exists)."""
    now = datetime.datetime.now(datetime.timezone.utc)
    for back in range(0, 48, 6):
        t = now - datetime.timedelta(hours=back)
        run = t.replace(hour=t.hour - t.hour % 6, minute=0, second=0, microsecond=0)
        base = f"{BUCKET}/gfs.{run:%Y%m%d}/{run:%H}/atmos/gfs.t{run:%H}z.pgrb2.0p50"
        try:
            get(f"{base}.f048.idx", tries=1)
            return run, base
        except Exception:
            continue
    raise SystemExit('no complete GFS run found')

_idx = {}
def field(base, hour, var, level):
    """One GRIB field as a (361, 720) array, lat 90..-90, lon 0..359.5."""
    key = (base, hour)
    if key not in _idx:
        lines = get(f"{base}.f{hour:03d}.idx").decode().strip().split('\n')
        _idx[key] = [l.split(':') for l in lines]
    rows = _idx[key]
    for i, r in enumerate(rows):
        if r[3] == var and r[4] == level:
            start = int(r[1])
            end = int(rows[i + 1][1]) - 1 if i + 1 < len(rows) else ''
            data = get(f"{base}.f{hour:03d}", {'Range': f'bytes={start}-{end}'})
            gid = eccodes.codes_new_from_message(data)
            try:
                ni = eccodes.codes_get(gid, 'Ni'); nj = eccodes.codes_get(gid, 'Nj')
                values = eccodes.codes_get_values(gid).reshape(nj, ni).astype(np.float32)
            finally:
                eccodes.codes_release(gid)
            return values
    raise KeyError(f'{var} {level} f{hour}')

def to_boxes(channels):
    """channels: list of (17, 361, 720) uint8/int8 arrays -> dict (latBand, lonBand) -> bytes."""
    # Re-grid to lat -90..90 ascending and lon -180..180 (adds the 180 seam column once).
    stacked = np.stack(channels, axis=-1)            # (T, 361, 720, C)
    stacked = stacked[:, ::-1, :, :]                 # lat ascending -90..90
    stacked = np.concatenate([stacked[:, :, 360:], stacked[:, :, :360], stacked[:, :, 360:361]], axis=2)  # lon -180..180 (721)
    out = {}
    for lb in range(18):
        for ob in range(36):
            r0, c0 = lb * 20, ob * 20
            box = stacked[:, r0:r0 + 21, c0:c0 + 21, :]
            if box.shape[1] < 21:  # north pole band ends at row 360
                pad = np.repeat(box[:, -1:], 21 - box.shape[1], axis=1)
                box = np.concatenate([box, pad], axis=1)
            out[(lb, ob)] = box.astype(np.uint8 if box.dtype == np.uint8 else box.dtype).tobytes()
    return out

def main(out_dir, levels=None, hours=None):
    run, base = latest_run()
    hours = hours or HOURS
    run_id = f"{run:%Y%m%d%H}"
    print('run', run_id, flush=True)
    for level_id, grib in LEVELS:
        if levels and level_id not in levels: continue
        t0 = time.time()
        speeds, dirs, temps, gusts = [], [], [], []
        for h in hours:
            if grib is None:
                u = field(base, h, 'UGRD', '10 m above ground'); v = field(base, h, 'VGRD', '10 m above ground')
                t = field(base, h, 'TMP', '2 m above ground'); g = field(base, h, 'GUST', 'surface')
                gusts.append(np.clip(np.rint(g * KT), 0, 255).astype(np.uint8))
            else:
                u = field(base, h, 'UGRD', grib); v = field(base, h, 'VGRD', grib); t = field(base, h, 'TMP', grib)
            speed = np.hypot(u, v) * KT
            direction = (np.degrees(np.arctan2(-u, -v)) + 360) % 360   # where it blows FROM
            speeds.append(np.clip(np.rint(speed), 0, 255).astype(np.uint8))
            dirs.append((np.rint(direction * 256 / 360).astype(np.int32) % 256).astype(np.uint8))
            temps.append(np.clip(np.rint(t - 273.15), -128, 127).astype(np.int8).view(np.uint8))
        channels = [np.stack(speeds), np.stack(dirs), np.stack(temps)] + ([np.stack(gusts)] if gusts else [])
        boxes = to_boxes(channels)
        folder = os.path.join(out_dir, 'winds', run_id, level_id)
        os.makedirs(folder, exist_ok=True)
        for (lb, ob), blob in boxes.items():
            with open(os.path.join(folder, f'{lb}_{ob}.bin'), 'wb') as f: f.write(blob)
        print(level_id, 'done in', round(time.time() - t0, 1), 's', flush=True)
    times = [int((run + datetime.timedelta(hours=h)).timestamp()) for h in hours]
    index = {
        'source': 'NOAA GFS 0.5 deg', 'run': run_id, 'generated': int(time.time()),
        'times': times, 'levels': [l for l, _ in LEVELS if not levels or l in levels],
        'box': 10, 'step': 0.5, 'n': 21,
        'channels': {'default': ['speed_kt_u8', 'dir_u8_256', 'temp_c_i8'], 'surface': ['speed_kt_u8', 'dir_u8_256', 'temp_c_i8', 'gust_kt_u8']},
    }
    with open(os.path.join(out_dir, 'winds', 'latest.json'), 'w') as f: json.dump(index, f)
    print('index written', flush=True)

if __name__ == '__main__':
    out = sys.argv[1]
    levels = sys.argv[2].split(',') if len(sys.argv) > 2 and sys.argv[2] else None
    hours = [int(x) for x in sys.argv[3].split(',')] if len(sys.argv) > 3 else None
    main(out, levels, hours)
