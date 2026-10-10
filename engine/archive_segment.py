"""Read hot originals or verified cold capsules using the SAME durable offsets.

Cold storage is lossless input retention, not a claim of normalized finality.
The native writer produces the capsule; this adapter is only a replay reader.
"""
import ctypes
import hashlib
import json
import os
import stat
import struct
from pathlib import Path

LIMIT = 256 * 1024**2
HEADER = struct.Struct('<4sIQQI')

def exists(path):
    p = Path(path)
    return p.is_file() or (p.parent/'capsule.json').is_file()

def _identity(path):
    s = Path(path).lstat()
    if not stat.S_ISREG(s.st_mode) or s.st_nlink != 1:
        raise ValueError('Capsule must be a private regular file')
    return [s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns]

def metadata(path):
    folder = Path(path).parent
    p = folder/'capsule.json'
    if p.stat().st_size > 16384:
        raise ValueError('Capsule manifest bound')
    envelope = json.loads(p.read_bytes()); v = envelope['value']
    encoded = json.dumps(v, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
    if (hashlib.sha256(encoded).hexdigest() != envelope['sha256'] or
        v['schema'] != 1 or v['codec'] != 'zstd-canonical-frames-v1' or
        v['segment'] != folder.name or v['capsuleIdentity'] != _identity(folder/'inputs.slc') or
        not 0 < v['canonicalBytes'] <= LIMIT or not 0 < v['sourceBytes'] <= 96*1024**2):
        raise ValueError('Capsule manifest/identity mismatch')
    return v

def size(path):
    try:
        return Path(path).stat().st_size
    except FileNotFoundError:
        return metadata(path)['sourceBytes']

def _cold(path):
    v = metadata(path); p = Path(path).parent/'inputs.slc'
    if not 0 < p.stat().st_size <= LIMIT:
        raise ValueError('Capsule compressed size bound')
    compressed = p.read_bytes()
    if hashlib.sha256(compressed).hexdigest() != v['capsuleSHA256']:
        raise ValueError('Capsule compressed hash mismatch')
    lib = ctypes.CDLL('libzstd.so.1')
    lib.ZSTD_decompress.argtypes = [ctypes.c_void_p,ctypes.c_size_t,ctypes.c_void_p,ctypes.c_size_t]
    lib.ZSTD_decompress.restype = ctypes.c_size_t
    raw = ctypes.create_string_buffer(v['canonicalBytes'])
    length = lib.ZSTD_decompress(raw, len(raw), compressed, len(compressed))
    if length != len(raw):
        raise ValueError('Capsule decompression failed')
    view = memoryview(raw).cast('B')
    if hashlib.sha256(view).hexdigest() != v['canonicalSHA256']:
        raise ValueError('Capsule canonical hash mismatch')
    return v, view

def read_batch(path, offset, limit, max_frames=1024, max_bytes=16*1024**2):
    from solana_stream import original_payload
    if not 0 <= offset <= limit or max_frames < 1:
        raise ValueError('Invalid archive cursor')
    result = []; used = 0
    try:
        source = Path(path).open('rb')
    except FileNotFoundError:
        source = None
    if source is not None:
        with source:
            source.seek(offset)
            while source.tell() < limit and len(result) < max_frames and used < max_bytes:
                head = source.read(HEADER.size)
                if len(head) != HEADER.size:
                    raise ValueError('Durable original header missing')
                magic,n,seq,stamp,crc = HEADER.unpack(head)
                if not 0 < n <= 8*1024**2 or source.tell()+n > limit:
                    raise ValueError('Durable original frame boundary')
                stored = source.read(n)
                if len(stored) != n:
                    raise ValueError('Durable original body missing')
                raw = original_payload(magic,stored,crc)
                result.append((raw,stamp,source.tell())); used += len(raw)
        return result
    v, raw = _cold(path)
    if limit > v['sourceBytes']:
        raise ValueError('Capsule cursor beyond original end')
    pos = logical = sequence = 0; found = offset == 0
    import zlib
    while pos < len(raw):
        if pos+32 > len(raw):raise ValueError('Capsule torn frame')
        magic,n,seq,stamp,crc = HEADER.unpack_from(raw,pos)
        length = struct.unpack_from('<I',raw,pos+28)[0]
        end = logical+HEADER.size+n
        if (magic not in (b'SLI1',b'SLZ1') or not 0<n<=8*1024**2 or
            not 0<length<=8*1024**2 or pos+32+length>len(raw) or seq!=sequence+1):
            raise ValueError('Capsule frame accounting')
        if logical == offset:found = True
        if logical >= offset and logical < limit:
            if not found or end > limit:raise ValueError('Capsule cursor splits frame')
            payload = bytes(raw[pos+32:pos+32+length])
            if zlib.crc32(payload) != crc:raise ValueError('Capsule CRC mismatch')
            result.append((payload,stamp,end)); used += length
            if len(result)>=max_frames or used>=max_bytes:return result
        logical=end;sequence=seq;pos+=32+length
        if logical>=limit:break
    if not found and offset!=v['sourceBytes']:raise ValueError('Capsule cursor not found')
    if logical==v['sourceBytes'] and (sequence!=v['frames'] or pos!=len(raw)):
        raise ValueError('Capsule final accounting')
    return result
